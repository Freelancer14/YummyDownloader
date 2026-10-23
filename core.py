#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core — site-независимое ядро YummyDownloader.

Сюда переехало из yummy_dl.py всё, чему не нужно знать про конкретный сайт:
  * конфиг и HTTP-слой (api/http_get/http_post — их дёргает GUI),
  * Kodik: декод ссылок и общий разбор POST /ftor для обоих путей
    (type='season' для обычного iframe и type='seria' для серий внутри
    kodik-serial), единая точка входа — kodik_resolve(EpisodeRef),
  * скачивание: download_hls (сегментный путь + fallback на прямой ffmpeg)
    и дешёвая проверка итога hls_file_ok,
  * политика сабов (is_sub/choose_dubbing), sanitize/parse_ep_range,
  * оркестрация: выбор группы, папка, файл-маркер через adapter.marker(),
    resume-проверка, ThreadPool по сериям, отчёт.

Сайтов здесь нет: работаем с EpisodeRef из site_adapter и с адаптером,
переданным снаружи. Поведение (тексты, флаги ffmpeg, имена файлов) — 1:1.
"""

import os, re, sys, json, time, base64, ssl, subprocess, socket
import http.client
from concurrent.futures import ThreadPoolExecutor, as_completed

import certifi
import imageio_ffmpeg
import urllib.request, urllib.parse

# Консоль cmd на русской Windows — cp866; не падаем на редких символах вывода.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# ----------------------------------------------------------------------------- config
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = {
    "preferred_dubbings": ["AniLibria", "AniStar", "Dream Cast", "AnimeVost", "AniDUB"],
    "allow_subtitles": True,       # False — скрыть сабы из выбора (CLI: --no-subs)
    "preferred_subs": ["Crunchyroll"],  # сабы на случай, если озвучек нет
    "quality": "max",            # "max" | "720" | "480" | "360"
    "all_seasons": False,        # True — пройти все сезоны франшизы (viewing_order)
    "only_kodik": True,          # качаем только из плеера Kodik (самый надёжный)
    "workers": 4,                # параллельных серий (CLI: --workers N)
    "seg_workers": 8,            # параллельных сегментов внутри серии (CLI: --segs N)
    "output_dir": os.path.join(HERE, "downloads"),
    "site": "https://old.yummyani.me",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception as e:
            print(f"[!] config.json кривой, беру значения по умолчанию: {e}")
    # относительный output_dir считаем от папки скрипта (а не от cwd)
    od = cfg.get("output_dir") or "downloads"
    if not os.path.isabs(od):
        od = os.path.join(HERE, od)
    cfg["output_dir"] = od
    return cfg


CFG = load_config()
SITE = CFG["site"].rstrip("/")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/120.0"
CTX = ssl.create_default_context(cafile=certifi.where())
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# ----------------------------------------------------------------------------- http
# Сетевой слой с повтором. Одиночный RST/таймаут на TLS-handshake (WinError 10054)
# — не ошибка пользователя, а обычная шероховатость канала: остаточный
# Sangfor-драйвер (SangforTcpDrv.sys) в TCP/WFP-пути рвёт первое соединение
# после простоя, а край сайта иногда дублит это по своему rate-limit.
# Поэтому РЕАЛЬНЫЙ обрыв повторяем, а HTTP-статусы (404/4xx/5xx) — нет:
# они воспроизводимы и повтором не чинятся.
HTTP_ATTEMPTS = 3                    # попыток всего, включая первую
HTTP_BACKOFF = (0.5, 1.5, 3.0)       # пауза перед попыткой 2 / 3 / 4...

#: Сетевой обрыв — единственное, что повторяем. HTTPError (подкласс URLError!)
#: сюда НЕ входит намеренно: 4xx/5xx — это ответ сервера, а не разрыв.
_RETRYABLE = (ConnectionResetError,     # WinError 10054 «удалённый хост разорвал»
              ConnectionAbortedError,
              BrokenPipeError,
              socket.timeout,
              TimeoutError,
              http.client.RemoteDisconnected)


def _retryable(exc):
    """True только для сетевого обрыва; HTTP-статусы и прочее — нет."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, urllib.error.URLError):
        return _retryable(getattr(exc, "reason", None))
    return False


def _retry_exhausted(attempts, last):
    """Тот же тип исключения, но с понятным текстом (свой класс НЕ заводим)."""
    msg = "%d попытки, последняя ошибка: %s" % (attempts, last)
    try:
        return type(last)(msg)
    except Exception:
        return last


def _urlopen_once(req, timeout, binary):
    """Одна попытка: открыть соединение и прочитать тело.

    Единственный шов для тестов и единственное место, где ловится обрыв:
    он прилетает и на urlopen (TLS-handshake), и на read (тело ответа)."""
    r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
    try:
        return r.read() if binary else r.read().decode("utf-8", "replace")
    finally:
        try:
            r.close()
        except Exception:
            pass


def _with_retry(fn):
    """Выполнить fn(), повторяя только сетевые обрывы (HTTP_ATTEMPTS раз)."""
    last = None
    for i in range(1, HTTP_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            if not _retryable(e):
                raise
            last = e
            if i >= HTTP_ATTEMPTS:
                break
            time.sleep(HTTP_BACKOFF[min(i - 1, len(HTTP_BACKOFF) - 1)])
    raise _retry_exhausted(HTTP_ATTEMPTS, last)


def http_get(url, ref=SITE + "/", extra=None, binary=False):
    hd = {"User-Agent": UA, "Referer": ref}
    if extra:
        hd.update(extra)
    req = urllib.request.Request(url, headers=hd)
    return _with_retry(lambda: _urlopen_once(req, 30, binary))


def http_post(url, data, ref=SITE + "/"):
    hd = {"User-Agent": UA, "Referer": ref, "Origin": "https://kodikplayer.com",
          "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=hd)
    return _with_retry(lambda: _urlopen_once(req, 30, False))


def api(path):
    raw = http_get(SITE + "/api" + path,
                   extra={"X-Application": "wawegr8j13it4rdw", "Vary": "json", "Lang": "ru",
                          "Accept": "application/json"})
    return json.loads(raw)["response"]


# ----------------------------------------------------------------------------- utils
#: Имена, которые Windows не даёт создать как файл/папку (регистр не важен).
_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {"COM%d" % i for i in range(1, 10)}
                 | {"LPT%d" % i for i in range(1, 10)})


def sanitize(name):
    if name is None:
        return "untitled"
    name = re.sub(r'[\\/:*?"<>|]', "", str(name)).strip()
    # пробелы схлопываем ДО финальной чистки: иначе "Foo ." -> "Foo " (хвостовой
    # пробел Windows молча обрежет, и папка окажется не там, где мы считали).
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if not name:
        return "untitled"
    # CON/NUL/COM1... Windows не создаст даже с расширением — уводим подчёркиванием.
    if name.split(".")[0].strip().upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def parse_ep_range(text, available):
    """'5-12', '3,7,9', '5-' -> множество номеров. Пусто/'all' -> все.

    Возвращает только реально доступные номера: если пользователь попросил
    несуществующую серию, результат пустой (вызывающий код покажет ошибку).
    Раньше здесь стоял `keep & set(available) or set(available)`, из-за чего
    любой промах молча превращался в «качать все серии»."""
    avail = set(available or ())
    avail_sorted = sorted(avail)
    text = (text or "").strip().lower()
    if not text or text in ("all", "все", "*"):
        return set(avail)
    keep = set()
    # Пробел — тоже разделитель: "1 2" это две серии, а не двенадцатая.
    # Пробелы ВОКРУГ дефиса убираем заранее, иначе "5 - 12" распадается на
    # "5", "-", "12" и превращается в две серии вместо диапазона.
    text = re.sub(r"\s*-\s*", "-", text)
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        if "-" in part:
            a_s, _, b_s = part.partition("-")
            if not a_s and not b_s:
                continue          # одинокий дефис — не диапазон
            try:
                a = int(a_s) if a_s else (avail_sorted[0] if avail_sorted else None)
                b = int(b_s) if b_s else (avail_sorted[-1] if avail_sorted else None)
            except ValueError:
                continue          # "1-3-5" — мусор, не роняем весь прогон
            if a is None or b is None or a > b:
                continue
            keep.update(n for n in range(a, b + 1) if n in avail)
        elif part.isdigit():
            n = int(part)
            if n in avail:
                keep.add(n)
    return keep


# --------------------------------------------------------------------------- subs
SUB_PREFIX = "[саб] "
SUB_SUFFIX = " (Субтитры)"


def is_sub(name):
    """True, если группа — субтитры, а не озвучка.

    Ядро маркирует сабы префиксом '[саб] ' (kodik_groups, tv_enumerate);
    суффикс ' (Субтитры)' принимается для совместимости со старыми именами.
    """
    n = str(name or "")
    return n.startswith(SUB_PREFIX) or SUB_SUFFIX in n


def choose_dubbing(groups, prefs, allow_subs=True, sub_prefs=None):
    """Выбор группы по умолчанию: озвучка в приоритете.

    Сабы — только если озвучек нет (сначала по sub_prefs, иначе самая
    полная группа) или выбраны явно (dub_override в grab_* обходит
    этот приоритет). При allow_subs=False сабы исключаются полностью
    (одни сабы -> None).
    """
    names = list(groups.keys())
    dubs = [n for n in names if not is_sub(n)]
    subs = [n for n in names if is_sub(n)]
    for p in prefs or []:
        for n in dubs:
            if p.lower() in n.lower():
                return n
    if dubs:
        # иначе — озвучка, где больше всего серий
        return max(dubs, key=lambda n: len(groups[n]))
    if allow_subs and subs:
        for p in sub_prefs or []:
            for n in subs:
                if p.lower() in n.lower():
                    return n
        return max(subs, key=lambda n: len(groups[n]))
    return None


# --------------------------------------------------------------------------- kodik
# Единственный разбор POST kodikplayer.com/ftor на оба пути:
#   обычный iframe плеера -> type='season'  (kodik_m3u8)
#   серия внутри serial    -> type='seria'   (tv_m3u8)
# Оба возвращают (m3u8, pick) — одинаково, поэтому ядро их не различает.

ROT_TABLES = [str.maketrans(
    {**{chr(c): chr((c - 65 + n) % 26 + 65) for c in range(65, 91)},
     **{chr(c): chr((c - 97 + n) % 26 + 97) for c in range(97, 123)}}) for n in range(26)]


def kodik_decode(src):
    """Ссылка от Kodik закодирована base64 со сдвигом букв. Сдвиг меняется — перебираем."""
    for tbl in ROT_TABLES:
        t = src.translate(tbl)
        try:
            d = base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")
            if "m3u8" in d:
                return ("https:" + d) if d.startswith("//") else d
        except Exception:
            pass
    raise RuntimeError("не смог декодировать ссылку Kodik")


def _ftor(sig, ktype, ident, khash, ref_url, quality):
    """POST /ftor -> (m3u8, качество). sig — разобранные sig-поля страницы."""
    payload = {"d": sig.get("domain", ""), "d_sign": sig.get("d_sign", ""),
               "pd": sig.get("pd", ""), "pd_sign": sig.get("pd_sign", ""),
               "ref": sig.get("ref", ""), "ref_sign": sig.get("ref_sign", ""),
               "bad_user": "false", "cdn_is_working": "true",
               "type": ktype, "hash": khash, "id": ident}
    j = json.loads(http_post("https://kodikplayer.com/ftor", payload, ref=ref_url))
    links = j.get("links") or {}
    if not links:
        raise RuntimeError("Kodik не отдал ссылки (links пуст)")

    def _src_of(q):
        """Список ссылок качества q, если в нём есть непустой src."""
        v = links.get(str(q))
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("src"):
            return v[0]["src"]
        return None

    # Считаем только качества, у которых реально есть ссылка: иначе
    # links["720"] == [] давал IndexError вместо понятной ошибки.
    avail = sorted(int(q) for q in links
                   if str(q).isdigit() and _src_of(q))
    if not avail:
        raise RuntimeError("Kodik не отдал ссылок с src (качества: %s)"
                           % sorted(links)[:6])
    # quality из config.json может прийти числом (720) — приводим к строке.
    qtxt = str("max" if quality is None else quality).strip().lower()
    if qtxt in ("", "max", "best") or not qtxt.isdigit():
        pick = avail[-1]
    else:
        want = int(qtxt)
        le = [q for q in avail if q <= want]
        pick = le[-1] if le else avail[-1]
    return kodik_decode(_src_of(pick)), pick


def _iframe_sig(html):
    """Sig-поля обычной iframe-страницы плеера (var domain = "..."; и т.п.)."""
    def var(n):
        m = re.search(rf'var {n} = "([^"]*)";', html)
        return m.group(1) if m else ""

    def one(pat, what):
        m = re.search(pat, html)
        if not m:
            raise RuntimeError("страница плеера Kodik не разобрана: нет %s" % what)
        return m.group(1)

    return ({"domain": var("domain"), "d_sign": var("d_sign"), "pd": var("pd"),
             "pd_sign": var("pd_sign"), "ref": var("ref"), "ref_sign": var("ref_sign")},
            one(r"\.type = '([^']+)'", ".type"),
            one(r"\.hash = '([^']+)'", ".hash"),
            one(r"\.id = '([^']+)'", ".id"))


def kodik_m3u8(iframe_url, quality="max"):
    """iframe_url -> прямой m3u8 нужного качества."""
    if iframe_url.startswith("//"):
        iframe_url = "https:" + iframe_url
    html = http_get(iframe_url, ref=SITE + "/")
    sig, typ, hsh, vid = _iframe_sig(html)
    return _ftor(sig, typ, vid, hsh, iframe_url, quality)


def tv_m3u8(sig, ep_id, ep_hash, ref_url, quality="max"):
    """Одна серия serial-iframe: POST /ftor type=seria -> (m3u8, pick).

    sig — parse_sig() страницы перевода, чьи (id, hash) используем."""
    return _ftor(sig, "seria", ep_id, ep_hash, ref_url, quality)


def _as_ref(ref):
    """Привести EpisodeRef | ('tv', sig, id, hash, ref) | строку к (kind, payload)."""
    kind = getattr(ref, "kind", None)
    if kind:
        payload = dict(getattr(ref, "payload", None) or {})
        return kind, payload
    if isinstance(ref, tuple) and ref and ref[0] == "tv":
        return "serial", {"sig": ref[1], "id": ref[2], "hash": ref[3], "ref": ref[4]}
    if isinstance(ref, str):
        return "iframe", {"iframe_url": ref}
    if isinstance(ref, dict):
        return (ref.get("kind") or "iframe"), dict(ref)
    return "iframe", {"iframe_url": ref}


def kodik_resolve(ref, quality="max"):
    """EpisodeRef (или старый 'tv'-tuple / строка-iframe) -> (m3u8, качество).

    kind='serial' -> POST /ftor type=seria (серия внутри kodik-serial), иначе
    обычный iframe-путь type='season'."""
    kind, payload = _as_ref(ref)
    if kind == "serial":
        return tv_m3u8(payload.get("sig") or {}, payload.get("id") or "",
                       payload.get("hash") or "", payload.get("ref") or "", quality)
    return kodik_m3u8(payload.get("iframe_url") or "", quality)


# --------------------------------------------------------------------------- ffmpeg
def download_hls(m3u8, out_path, attempts=3, progress_cb=None, is_stopped=None,
                 seg_workers=None):
    """Скачать серию. Сначала сегментный путь (пул потоков + concat без
    перекодирования), при неудаче — старый ffmpeg -c copy напрямую с m3u8.

    seg_workers=0 — форсировать прямой ffmpeg (для проверки: если микрофризы
    уходят на прямом пути, виновата склейка; если остаются — источник).

    progress_cb(done_count, total_count, bytes_done) — для GUI.
    is_stopped: threading.Event или callable — остановка пробрасывается наружу
    как исключение Stopped из seg_dl (CLI его не ловит специально)."""
    try:
        _sw = int(seg_workers) if seg_workers is not None else None
    except Exception:
        _sw = None
    if _sw is None:
        try:
            _sw = max(1, int(CFG.get("seg_workers", 8) or 8))
        except Exception:
            _sw = 8
    seg_workers = _sw
    if seg_workers <= 0:
        seg_dl = None  # прямой путь ниже
    else:
        try:
            import seg_dl
        except ImportError:
            seg_dl = None
    if seg_dl is not None:
        try:
            seg_dl.download_hls_segmented(m3u8, out_path, seg_workers=seg_workers,
                                          progress_cb=progress_cb, is_stopped=is_stopped)
            ok, reason = hls_file_ok(out_path)
            if ok:
                return
            # сегменты сошлись в неиграбельный файл — удаляем и пробуем
            # прямой ffmpeg ниже (там свой контроль итога)
            print(f"(итог неиграбелен: {reason}, пробую ffmpeg напрямую...)", flush=True)
            try:
                os.remove(out_path)
            except OSError:
                pass
        except Exception as e:
            if isinstance(e, seg_dl.Stopped):
                raise
            print(f"(сегменты не вышли: {str(e)[:100]}, пробую ffmpeg напрямую...)", flush=True)
    tmp = out_path + ".part.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-stats",
           "-referer", "https://kodikplayer.com/", "-user_agent", UA,
           # keep-alive + переподключение на обрыве сегмента: лечит "I/O error"
           # на нестабильном CDN Kodik (иначе ffmpeg падает на первом же сбое сети)
           "-multiple_requests", "1", "-reconnect", "1",
           "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
           "-reconnect_delay_max", "5", "-rw_timeout", "30000000",
           "-i", m3u8, "-c", "copy", "-bsf:a", "aac_adtstoasc", tmp]
    last_err = "?"
    for attempt in range(1, attempts + 1):
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        last_err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "?"
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) >= 10000:
            ok, reason = hls_file_ok(tmp)
            if ok:
                os.replace(tmp, out_path)
                return
            # файл-обрывок (битый mp4): в итог не годится, качаем заново
            last_err = "итог неиграбелен (%s)" % reason
        if os.path.exists(tmp):
            os.remove(tmp)
        if attempt < attempts:
            print(f"(попытка {attempt} не удалась, повтор...)", end=" ", flush=True)
            time.sleep(2 * attempt)
    raise RuntimeError("ffmpeg не справился: " + last_err)


def hls_file_ok(path, expected_dur=None):
    """Дешёвая проверка, что файл — играбельный mp4, а не обрывок качки.

    Открываем файл ffmpeg'ом без выдачи (сам ffmpeg быстро выйдет с ошибкой
    'At least one output file...', однако к тому моменту в stderr уже есть
    баннер входа). Нет строки 'Duration:' или есть 'Invalid data' -> файла
    нет как медиа: ffmpeg -c copy не дописывает moov при обрыве, поэтому
    битый обрывок почти всегда даже не открывается.
    Результат (ok, reason): при ok=False reason понятен человеку — он идёт
    в лог «повторно качаю» / ошибку серии. expected_dur (>0) — эталон по
    сумме EXTINF плейлиста: длительность файла должна попасть в 80..120%
    эталона. Сама проверка не должна блокировать качку, поэтому при ошибке
    запуска (ffmpeg отсутствует, timeout) возвращаем (True, '')."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 10000:
            return False, "нет файла или меньше 10 КБ"
    except OSError:
        return False, "файл недоступен"
    try:
        r = subprocess.run([FFMPEG, "-nostdin", "-i", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        return True, ""
    except Exception as e:
        print(f"(проверку файла не смог запустить: {str(e)[:80]} - считаю файл рабочим)")
        return True, ""
    err = r.stderr or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    if m is None or "invalid data" in err.lower():
        why = next((ln for ln in lines
                    if "invalid data" in ln.lower() or "moov" in ln.lower()),
                   lines[-1] if lines else "ffmpeg молчит")
        return False, "ffmpeg mp4 не прочитал (%s)" % why[:90]
    if expected_dur and expected_dur > 0:
        try:
            h, mn, sec = m.groups()
            dur = int(h) * 3600 + int(mn) * 60 + float(sec)
            k = dur / float(expected_dur)
            if not (0.8 <= k <= 1.2):
                return False, ("длительность %.0fс у файла против %.0fс по плейлисту"
                               % (dur, expected_dur))
        except Exception:
            pass
    return True, ""


# ----------------------------------------------------------------- orchestration
def pool_sizes(cfg):
    """(серий, сегментов) из конфига — с теми же дефолтами и клампами, что раньше."""
    try:
        workers = max(1, int(cfg.get("workers", 4) or 4))
    except Exception:
        workers = 4
    try:
        seg_workers = max(1, int(cfg.get("seg_workers", 8) or 8))
    except Exception:
        seg_workers = 8
    return workers, seg_workers


def episode_path(out_dir, num):
    """Имя серии — ровно как раньше: '<папка>/Серия NN.mp4'."""
    return os.path.join(out_dir, f"Серия {num:02d}.mp4")


def select_group(groups, cfg, dub_override=None):
    """Явный override -> приоритеты из конфига. None, если брать нечего.

    Если пользователь ЯВНО выбрал озвучку, а её нет — возвращаем None
    (тайтл пропускается с понятным сообщением), а не подсовываем другую:
    раньше несовпавший override молча заменялся озвучкой из приоритетов."""
    if not groups:
        return None
    if dub_override:
        for n in groups:
            if dub_override.lower() in n.lower():
                return n
        return None
    return choose_dubbing(groups, cfg.get("preferred_dubbings") or [],
                          allow_subs=cfg.get("allow_subtitles", True),
                          sub_prefs=cfg.get("preferred_subs"))


def plan_todo(out_dir, nums):
    """Resume: оставить целые файлы, битые удалить. Печатает ход решений."""
    todo = []
    for num in nums:
        out = episode_path(out_dir, num)
        if os.path.exists(out):
            ok, why = hls_file_ok(out)
            if ok:
                print(f"    [{num:02d}] уже есть - пропуск")
                continue
            print(f"    [{num:02d}] повторно качаю: {why}")
            try:
                os.remove(out)
            except OSError:
                pass
        todo.append(num)
    return todo


def _download_episode_ref(num, ref, quality, out_dir, seg_workers):
    """Одна серия: EpisodeRef -> m3u8 -> download_hls. -> (качество, размер)."""
    out = episode_path(out_dir, num)
    m3u8, q = kodik_resolve(ref, quality)
    download_hls(m3u8, out, seg_workers=seg_workers)
    return q, os.path.getsize(out)


def run_episodes(refs, todo, out_dir, cfg, quality, workers, seg_workers,
                 thread_prefix="ep"):
    """ThreadPool по сериям + отчёт. refs: {номер: EpisodeRef}.

    Возвращает {номер: ('ok', качество, размер) | ('err', текст)} —
    исключение одной серии не трогает остальные."""
    results = {}
    if not todo:
        return results
    if len(todo) == 1 or workers <= 1:
        for num in todo:
            try:
                q, size = _download_episode_ref(num, refs[num], quality,
                                                out_dir, seg_workers)
                results[num] = ("ok", q, size)
            except Exception as e:
                results[num] = ("err", str(e))
            time.sleep(0.5)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo)),
                                thread_name_prefix=thread_prefix) as ex:
            futs = {ex.submit(_download_episode_ref, n, refs[n], quality,
                              out_dir, seg_workers): n for n in todo}
            for f in as_completed(futs):
                n = futs[f]
                try:
                    q, size = f.result()
                    results[n] = ("ok", q, size)
                except Exception as e:
                    results[n] = ("err", str(e))
    for num in sorted(results):
        r = results[num]
        if r[0] == "ok":
            print(f"    [{num:02d}] {r[1]}p ... OK ({r[2]//1024//1024} МБ)")
        else:
            print(f"    [{num:02d}] ОШИБКА: {r[1]}")
    return results


def run_title(title, groups, cfg, adapter, info=None, dub_override=None,
              ep_filter=None, header_extra="", franchise_dir=None,
              empty_msg="", nodub_msg="", thread_prefix="ep"):
    """Общий путь тайтла для обоих сайтов.

    groups: {группа: {номер: EpisodeRef}}. adapter — только ради marker()/is_sub(),
    к сайту ядру отношения не имеет. Печатает ровно то, что печатали
    grab_anime/tv_grab_anime. None — тайтл пропущен."""
    if not groups:
        if empty_msg:
            print(empty_msg)
        return None
    dub = select_group(groups, cfg, dub_override)
    if not dub:
        if nodub_msg:
            print(nodub_msg)
        return None
    refs = groups[dub]
    quality = cfg["quality"]
    base = franchise_dir or cfg["output_dir"]
    out_dir = os.path.join(base, title)
    os.makedirs(out_dir, exist_ok=True)
    nums = sorted(refs)
    if ep_filter:
        nums = [n for n in nums if ep_filter(n)]
    fname, word = adapter.marker(dub)
    kind = "субтитры" if adapter.is_sub(dub) else "озвучка"
    print(f"\n=== {title} ===")
    print(f"    {kind}: {dub}   серий к скачиванию: {len(nums)}   качество: {quality}")
    if header_extra:
        print(header_extra(dub) if callable(header_extra) else header_extra)
    lines = [title, f"{word}: {dub}"] + [str(x) for x in adapter.marker_extra(info)]
    with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    workers, seg_workers = pool_sizes(cfg)
    todo = plan_todo(out_dir, nums)
    if todo:
        print(f"    потоки: серии={min(workers, len(todo))}, сегменты={seg_workers}")
    results = run_episodes(refs, todo, out_dir, cfg, quality, workers,
                           seg_workers, thread_prefix=thread_prefix)
    return {"dub": dub, "title": title, "nums": nums, "out_dir": out_dir,
            "results": results}