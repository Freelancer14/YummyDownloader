#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YummyDownloader — качалка аниме с YummyAnime (old.yummyani.me) без проигрывания плеера.

Как это работает (никаких спойлеров-превью):
  1. По ссылке на тайтл берём числовой page_id со страницы.
  2. Дёргаем внутренний API сайта  /api/anime/<id>/videos  — там список всех серий,
     названия озвучек и прямые ссылки на плеер Kodik.
  3. Для нужной озвучки по каждой серии разбираем плеер Kodik (запрос /ftor + декод
     ссылки перебором сдвига) и получаем m3u8.
  4. ffmpeg сам скачивает HLS-поток и складывает в .mp4. Раскладываем по папкам.

Запуск:
  py yummy_dl.py "https://old.yummyani.me/catalog/item/...."     — скачать один тайтл

Настройки — в config.json рядом со скриптом.
"""

import os, re, sys, json, time, base64, ssl, subprocess
import threading
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import certifi
import imageio_ffmpeg

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
def http_get(url, ref=SITE + "/", extra=None, binary=False):
    hd = {"User-Agent": UA, "Referer": ref}
    if extra:
        hd.update(extra)
    r = urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=30, context=CTX)
    return r.read() if binary else r.read().decode("utf-8", "replace")

def http_post(url, data, ref=SITE + "/"):
    hd = {"User-Agent": UA, "Referer": ref, "Origin": "https://kodikplayer.com",
          "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode(data).encode()
    r = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hd), timeout=30, context=CTX)
    return r.read().decode("utf-8", "replace")

def api(path):
    raw = http_get(SITE + "/api" + path,
                   extra={"X-Application": "wawegr8j13it4rdw", "Vary": "json", "Lang": "ru",
                          "Accept": "application/json"})
    return json.loads(raw)["response"]

# ----------------------------------------------------------------------------- utils
def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', "", str(name)).strip().rstrip(".")
    return re.sub(r"\s+", " ", name) or "untitled"

def resolve_page_id(url):
    """Из ссылки на тайтл вытащить числовой page_id."""
    html = http_get(url)
    m = re.search(r'page_id"\s*content="(\d+)"', html)
    if not m:
        raise RuntimeError("не нашёл page_id на странице - проверь ссылку")
    return int(m.group(1))

# --------------------------------------------------------------------------- kodik
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

def kodik_m3u8(iframe_url, quality="max"):
    """iframe_url -> прямой m3u8 нужного качества."""
    if iframe_url.startswith("//"):
        iframe_url = "https:" + iframe_url
    html = http_get(iframe_url, ref=SITE + "/")
    def var(n):
        m = re.search(rf'var {n} = "([^"]*)";', html)
        return m.group(1) if m else ""
    typ = re.search(r"\.type = '([^']+)'", html).group(1)
    hsh = re.search(r"\.hash = '([^']+)'", html).group(1)
    vid = re.search(r"\.id = '([^']+)'", html).group(1)
    payload = {"d": var("domain"), "d_sign": var("d_sign"), "pd": var("pd"),
               "pd_sign": var("pd_sign"), "ref": var("ref"), "ref_sign": var("ref_sign"),
               "bad_user": "false", "cdn_is_working": "true",
               "type": typ, "hash": hsh, "id": vid}
    j = json.loads(http_post("https://kodikplayer.com/ftor", payload, ref=iframe_url))
    links = j.get("links") or {}
    if not links:
        raise RuntimeError("Kodik не отдал ссылки (links пуст)")
    avail = sorted((int(q) for q in links if q.isdigit()))
    if quality == "max" or not quality.isdigit():
        pick = avail[-1]
    else:
        want = int(quality)
        le = [q for q in avail if q <= want]
        pick = le[-1] if le else avail[-1]
    return kodik_decode(links[str(pick)][0]["src"]), pick

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
            return
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
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) >= 10000:
            os.replace(tmp, out_path)
            return
        last_err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "?"
        if os.path.exists(tmp):
            os.remove(tmp)
        if attempt < attempts:
            print(f"(попытка {attempt} не удалась, повтор...)", end=" ", flush=True)
            time.sleep(2 * attempt)
    raise RuntimeError("ffmpeg не справился: " + last_err)

# --------------------------------------------------------------------------- core
def kodik_groups(items):
    """Сгруппировать серии по озвучке (только плеер Kodik)."""
    groups = {}
    for it in items:
        iframe = it.get("iframe_url", "")
        if "kodikplayer.com" not in iframe:
            continue
        d = it.get("data") or {}
        dub = (d.get("dubbing") or "Без названия").replace("Озвучка ", "").replace("Субтитры ", "[саб] ").strip()
        try:
            num = int(re.sub(r"\D", "", str(it.get("number"))) or 0)
        except Exception:
            num = 0
        groups.setdefault(dub, {})[num] = iframe
    return groups

def choose_dubbing(groups, prefs):
    names = list(groups.keys())
    for p in prefs:
        for n in names:
            if p.lower() in n.lower():
                return n
    # иначе — где больше всего серий
    return max(names, key=lambda n: len(groups[n])) if names else None

def _download_episode(num, iframe, quality, out_dir, seg_workers):
    """Одна серия: m3u8 -> download_hls. Возвращает (pick_quality, size_bytes).
    Исключение одной серии не трогает остальные (ловит вызывающий код)."""
    out = os.path.join(out_dir, f"Серия {num:02d}.mp4")
    m3u8, q = kodik_m3u8(iframe, quality)
    download_hls(m3u8, out, seg_workers=seg_workers)
    return q, os.path.getsize(out)


def grab_anime(anime_id, cfg, franchise_dir=None, dub_override=None, ep_filter=None):
    info = api(f"/anime/{anime_id}")
    title = sanitize(info.get("title") or f"anime_{anime_id}")
    items = api(f"/anime/{anime_id}/videos")
    groups = kodik_groups(items)
    if not groups:
        print(f"[!] {title}: озвучек в плеере Kodik не нашлось (есть только Alloha/CVH) - пропуск")
        return info
    # выбор озвучки: явный override (если есть в этом тайтле) -> приоритеты из конфига
    dub = None
    if dub_override:
        for n in groups:
            if dub_override.lower() in n.lower():
                dub = n
                break
    if not dub:
        dub = choose_dubbing(groups, cfg["preferred_dubbings"])
    eps = groups[dub]
    quality = cfg["quality"]
    base = franchise_dir or cfg["output_dir"]
    out_dir = os.path.join(base, title)
    os.makedirs(out_dir, exist_ok=True)
    nums = sorted(eps)
    if ep_filter:
        nums = [n for n in nums if ep_filter(n)]
    print(f"\n=== {title} ===")
    print(f"    озвучка: {dub}   серий к скачиванию: {len(nums)}   качество: {quality}")
    with open(os.path.join(out_dir, "_озвучка.txt"), "w", encoding="utf-8") as f:
        f.write(f"{title}\nОзвучка: {dub}\nИсточник: {SITE}\n")
    try:
        workers = max(1, int(cfg.get("workers", 4) or 4))
    except Exception:
        workers = 4
    try:
        seg_workers = max(1, int(cfg.get("seg_workers", 8) or 8))
    except Exception:
        seg_workers = 8
    todo = []
    for num in nums:
        out = os.path.join(out_dir, f"Серия {num:02d}.mp4")
        if os.path.exists(out) and os.path.getsize(out) > 10000:
            print(f"    [{num:02d}] уже есть - пропуск")
            continue
        todo.append(num)
    if todo:
        print(f"    потоки: серии={min(workers, len(todo))}, сегменты={seg_workers}")
    results = {}
    if len(todo) == 1 or workers <= 1:
        for num in todo:
            try:
                q, size = _download_episode(num, eps[num], quality, out_dir, seg_workers)
                results[num] = ("ok", q, size)
            except Exception as e:
                results[num] = ("err", str(e))
            time.sleep(0.5)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo)),
                                thread_name_prefix="ep") as ex:
            futs = {ex.submit(_download_episode, n, eps[n], quality,
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
    return info

def parse_ep_range(text, available):
    """'5-12', '3,7,9', '5-' -> множество номеров. Пусто/'all' -> все."""
    text = (text or "").strip().lower()
    if not text or text in ("all", "все", "*"):
        return set(available)
    keep = set()
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            a, _, b = part.partition("-")
            a = int(a) if a else min(available)
            b = int(b) if b else max(available)
            keep.update(range(a, b + 1))
        elif part.isdigit():
            keep.add(int(part))
    return keep & set(available) or set(available)

def ask_menu(info, items, cfg):
    """Интерактивный выбор: озвучка -> качество -> серии -> все сезоны. Возвращает dict override."""
    title = info.get("title", "?")
    groups = kodik_groups(items)
    print(f"\n========== {title} ==========")
    if not groups:
        print("В плеере Kodik озвучек нет (только Alloha/CVH) — этот тайтл не качается.")
        return None

    # --- 1. озвучка ---
    names = sorted(groups, key=lambda n: -len(groups[n]))
    default_dub = choose_dubbing(groups, cfg["preferred_dubbings"])
    print("\nДоступные озвучки:")
    for i, n in enumerate(names, 1):
        mark = "  <- по умолчанию" if n == default_dub else ""
        print(f"  {i}. {n}  ({len(groups[n])} сер.){mark}")
    sel = input("Выбери озвучку (номер, Enter = по умолчанию): ").strip()
    dub = names[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(names) else default_dub
    eps_avail = sorted(groups[dub])

    # --- 2. качество ---
    print("\nКачество:  1) макс (обычно 720)   2) 480   3) 360")
    qmap = {"1": "max", "2": "480", "3": "360", "": cfg["quality"]}
    q = qmap.get(input(f"Выбери качество (Enter = {cfg['quality']}): ").strip(), cfg["quality"])

    # --- 3. серии ---
    print(f"\nСерии в этой озвучке: {eps_avail[0]}–{eps_avail[-1]} (всего {len(eps_avail)})")
    rng = input("Какие качать? (Enter = все, или напр. 5-12 / 3,7,9): ").strip()
    keep = parse_ep_range(rng, eps_avail)

    # --- 4. все сезоны франшизы ---
    vo = info.get("viewing_order") or []
    other = [v for v in vo if v.get("anime_id") and v["anime_id"] != info.get("anime_id")]
    allseasons = False
    if other:
        a = input(f"\nКачать всю франшизу (ещё {len(other)} тайтл(ов))? (y/N): ").strip().lower()
        allseasons = a == "y"

    print(f"\n>> Итог: озвучка «{dub}», качество {q}, серий {len(keep)}"
          + (", вся франшиза" if allseasons else ""))
    if input("Начать? (Y/n): ").strip().lower() == "n":
        return None
    return {"dub": dub, "quality": q, "episodes": keep, "all_seasons": allseasons}


def grab(url_or_id, cfg=None, interactive=False):
    cfg = dict(cfg or CFG)
    if isinstance(url_or_id, str) and "yummyanime.tv" in url_or_id.lower():
        tv_grab(url_or_id, cfg, interactive=interactive)
        return
    if isinstance(url_or_id, str) and url_or_id.startswith("http"):
        page_id = resolve_page_id(url_or_id)
    else:
        page_id = int(url_or_id)

    dub_override = None
    ep_filter = None
    if interactive:
        info0 = api(f"/anime/{page_id}")
        items0 = api(f"/anime/{page_id}/videos")
        choice = ask_menu(info0, items0, cfg)
        if not choice:
            print("Отменено.")
            return
        cfg["quality"] = choice["quality"]
        cfg["all_seasons"] = choice["all_seasons"]
        dub_override = choice["dub"]
        keep = choice["episodes"]
        ep_filter = (lambda n: n in keep)

    info = grab_anime(page_id, cfg, dub_override=dub_override, ep_filter=ep_filter)
    if cfg.get("all_seasons") and info:
        vo = info.get("viewing_order") or []
        seasons = [v for v in vo if v.get("anime_id") and v["anime_id"] != page_id]
        if seasons:
            franchise = os.path.join(cfg["output_dir"], sanitize(vo[0].get("title") or info.get("title")))
            print(f"\n>>> Франшиза: ещё {len(seasons)} связанн(ых) тайтл(ов)")
            # остальные сезоны качаем целиком в выбранной озвучке (если она там есть)
            for v in seasons:
                try:
                    grab_anime(v["anime_id"], cfg, franchise_dir=franchise, dub_override=dub_override)
                except Exception as e:
                    print(f"[!] сезон {v.get('title')}: {e}")
    print("\nГотово.")

# --------------------------------------------------------------------------- .tv
# Ветка yummyanime.tv поверх того же Kodik-пайплайна (kodik_m3u8/decode,
# download_hls/seg_dl, ThreadPool серий). Alloha/CVH не тронуты.

def is_tv_url(url):
    return isinstance(url, str) and "yummyanime.tv" in url.lower()


def tv_m3u8(sig, ep_id, ep_hash, ref_url, quality="max"):
    """Одна серия serial-iframe: POST /ftor type=seria -> (m3u8, pick).

    sig — parse_sig() страницы перевода, чьи (id, hash) используем."""
    payload = {"d": sig.get("domain", ""), "d_sign": sig.get("d_sign", ""),
               "pd": sig.get("pd", ""), "pd_sign": sig.get("pd_sign", ""),
               "ref": sig.get("ref", ""), "ref_sign": sig.get("ref_sign", ""),
               "bad_user": "false", "cdn_is_working": "true",
               "type": "seria", "hash": ep_hash, "id": ep_id}
    j = json.loads(http_post("https://kodikplayer.com/ftor", payload, ref=ref_url))
    links = j.get("links") or {}
    if not links:
        raise RuntimeError("Kodik не отдал ссылки (links пуст)")
    avail = sorted((int(q) for q in links if q.isdigit()))
    if quality == "max" or not quality.isdigit():
        pick = avail[-1]
    else:
        want = int(quality)
        le = [q for q in avail if q <= want]
        pick = le[-1] if le else avail[-1]
    return kodik_decode(links[str(pick)][0]["src"]), pick


def tv_enumerate(card_url):
    """Карточка .tv -> {'id','title','card_url','slot','iframe','dubs'}.

    dubs: {dub_title: {'tid','season','eps':{ep:(epid,ephash)},
                       'sig':{...},'ref':page_url} | {'single':iframe_url}}.
    Сезон по умолчанию — selected на странице, иначе максимальный."""
    import tv_adapter as tva
    op = tva.TvOpener()
    meta = tva.tv_card_meta(card_url, op)
    slots = tva.tv_slots(card_url, op)
    kodik = [(s["title"], s["src"]) for s in slots
             if s["kind"] == "iframe" and "kodikplayer.com" in s["src"]]
    if not kodik:
        have = ", ".join("%s (%s)" % (s["title"], s["kind"]) for s in slots) or "пусто"
        raise RuntimeError("на .tv-карточке нет Kodik-слота, есть: " + have)
    slot_title, iframe = kodik[0]
    if iframe.startswith("//"):
        iframe = "https:" + iframe
    enum = {"id": meta["id"], "title": meta["title"] or "tv_%d" % meta["id"],
            "card_url": card_url, "slot": slot_title or "Kodik",
            "iframe": iframe, "dubs": {}}
    if "/serial/" not in iframe:
        # разовый несоставной iframe (фильм): одна "серия" штатным разбором
        enum["dubs"][slot_title or "Kodik"] = {"single": iframe}
        return enum
    sess = tva.SerialSession()
    first = sess.prime(iframe)
    trs = tva.parse_translations(first)
    if not trs:
        raise RuntimeError("serial-страница без переводов")
    done = set()

    def _store(tr, html, ref):
        seasons, sel = tva.parse_seasons(html)
        if not seasons:
            return False
        season = sel if sel in seasons else max(seasons)
        name = (tr["title"] or "").replace(".Subtitles", " (Субтитры)")
        enum["dubs"][name] = {
            "tid": tr["tid"], "season": season, "eps": seasons[season],
            "sig": tva.parse_sig(html), "ref": ref}
        return True

    default = next((t for t in trs if t["selected"]), trs[0])
    if _store(default, first, sess.last_url):
        done.add(default["title"])
    for tr in trs:
        if tr["title"] in done:
            continue
        try:
            html = sess.page(tr["media_id"], tr["media_hash"],
                             tr["media_type"] or "serial", expect_tid=tr["tid"])
        except Exception as e:
            print(f"[!] перевод «{tr['title']}»: пропуск ({str(e)[:100]})")
            continue
        if _store(tr, html, sess.last_url):
            done.add(tr["title"])
        time.sleep(0.5)
    if not enum["dubs"]:
        raise RuntimeError("не разобрали ни одного перевода")
    return enum


def _download_tv_episode(num, src, quality, out_dir, seg_workers):
    """Одна .tv-серия: src ('tv',sig,id,hash,ref) или iframe-строка (single)."""
    out = os.path.join(out_dir, f"Серия {num:02d}.mp4")
    if isinstance(src, tuple) and src and src[0] == "tv":
        _, sig, ep_id, ep_hash, ref = src
        m3u8, q = tv_m3u8(sig, ep_id, ep_hash, ref, quality)
    else:
        m3u8, q = kodik_m3u8(src, quality)
    download_hls(m3u8, out, seg_workers=seg_workers)
    return q, os.path.getsize(out)


def tv_grab_anime(card_url, cfg, dub_override=None, ep_filter=None, enum=None):
    """Скачать сезон .tv-карточки. Структура пула — как в grab_anime."""
    enum = enum or tv_enumerate(card_url)
    title = sanitize(enum.get("title") or "tv_%s" % enum.get("id", "?"))
    dubs = enum.get("dubs") or {}
    if not dubs:
        print(f"[!] {title}: озвучек в плеере Kodik не нашлось - пропуск")
        return enum
    groups = {}
    for name, di in dubs.items():
        groups[name] = {1: None} if di.get("single") else di.get("eps", {})
    dub = None
    if dub_override:
        for n in groups:
            if dub_override.lower() in n.lower():
                dub = n
                break
    if not dub:
        dub = choose_dubbing(groups, cfg["preferred_dubbings"])
    di = dubs[dub]
    if di.get("single"):
        eps = {1: di["single"]}
    else:
        eps = {n: ("tv", di["sig"], eid, ehash, di["ref"])
               for n, (eid, ehash) in di["eps"].items()}
    quality = cfg["quality"]
    out_dir = os.path.join(cfg["output_dir"], title)
    os.makedirs(out_dir, exist_ok=True)
    nums = sorted(eps)
    if ep_filter:
        nums = [n for n in nums if ep_filter(n)]
    extra = "" if di.get("single") else f"   сезон: {di.get('season')}"
    print(f"\n=== {title} ===")
    print(f"    озвучка: {dub}   серий к скачиванию: {len(nums)}   качество: {quality}")
    print(f"    плеер: {enum.get('slot')}{extra}")
    with open(os.path.join(out_dir, "_озвучка.txt"), "w", encoding="utf-8") as f:
        f.write(f"{title}\nОзвучка: {dub}\nПлеер: {enum.get('slot')} (yummyanime.tv)\n"
                f"Источник: {enum.get('card_url')}\n")
    try:
        workers = max(1, int(cfg.get("workers", 4) or 4))
    except Exception:
        workers = 4
    try:
        seg_workers = max(1, int(cfg.get("seg_workers", 8) or 8))
    except Exception:
        seg_workers = 8
    todo = []
    for num in nums:
        out = os.path.join(out_dir, f"Серия {num:02d}.mp4")
        if os.path.exists(out) and os.path.getsize(out) > 10000:
            print(f"    [{num:02d}] уже есть - пропуск")
            continue
        todo.append(num)
    if todo:
        print(f"    потоки: серии={min(workers, len(todo))}, сегменты={seg_workers}")
    results = {}
    if not todo:
        pass
    elif len(todo) == 1 or workers <= 1:
        for num in todo:
            try:
                q, size = _download_tv_episode(num, eps[num], quality, out_dir, seg_workers)
                results[num] = ("ok", q, size)
            except Exception as e:
                results[num] = ("err", str(e))
            time.sleep(0.5)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(todo)),
                                thread_name_prefix="tvep") as ex:
            futs = {ex.submit(_download_tv_episode, n, eps[n], quality,
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
    return enum


def tv_ask_menu(enum, cfg):
    """Интерактивный выбор для .tv: озвучка -> качество -> серии."""
    dubs = enum.get("dubs") or {}
    if not dubs:
        print("В плеере Kodik озвучек нет — этот тайтл не качается.")
        return None
    groups = {n: ({1: None} if d.get("single") else d.get("eps", {}))
              for n, d in dubs.items()}
    names = sorted(groups, key=lambda n: -len(groups[n]))
    default_dub = choose_dubbing(groups, cfg["preferred_dubbings"])
    print(f"\n========== {enum.get('title', '?')} (.tv, плеер {enum.get('slot')}) ==========")
    print("\nДоступные озвучки:")
    for i, n in enumerate(names, 1):
        mark = "  <- по умолчанию" if n == default_dub else ""
        print(f"  {i}. {n}  ({len(groups[n])} сер.){mark}")
    sel = input("Выбери озвучку (номер, Enter = по умолчанию): ").strip()
    dub = names[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(names) else default_dub
    eps_avail = sorted(groups[dub])
    print("\nКачество:  1) макс (обычно 720)   2) 480   3) 360")
    qmap = {"1": "max", "2": "480", "3": "360", "": cfg["quality"]}
    q = qmap.get(input(f"Выбери качество (Enter = {cfg['quality']}): ").strip(), cfg["quality"])
    print(f"\nСерии в этой озвучке: {eps_avail[0]}–{eps_avail[-1]} (всего {len(eps_avail)})")
    rng = input("Какие качать? (Enter = все, или напр. 5-12 / 3,7,9): ").strip()
    keep = parse_ep_range(rng, eps_avail)
    print(f"\n>> Итог: озвучка «{dub}», качество {q}, серий {len(keep)}")
    if input("Начать? (Y/n): ").strip().lower() == "n":
        return None
    return {"dub": dub, "quality": q, "episodes": keep}


def tv_grab(card_url, cfg=None, interactive=False):
    cfg = dict(cfg or CFG)
    enum = tv_enumerate(card_url)
    dub_override = None
    ep_filter = None
    if interactive:
        choice = tv_ask_menu(enum, cfg)
        if not choice:
            print("Отменено.")
            return
        cfg["quality"] = choice["quality"]
        dub_override = choice["dub"]
        keep = choice["episodes"]
        ep_filter = (lambda n: n in keep)
    tv_grab_anime(card_url, cfg, dub_override=dub_override,
                  ep_filter=ep_filter, enum=enum)
    print("\nГотово.")

# --------------------------------------------------------------------------- main
def _pop_int_flag(args, *names):
    """Вытащить из argv значение флага (--workers 4). Возвращает (value|None, остаток)."""
    out, val, i = [], None, 0
    while i < len(args):
        if args[i] in names and i + 1 < len(args) and args[i + 1].isdigit():
            val = int(args[i + 1])
            i += 2
            continue
        out.append(args[i])
        i += 1
    return val, out


if __name__ == "__main__":
    os.makedirs(CFG["output_dir"], exist_ok=True)
    args = [a for a in sys.argv[1:] if a]
    workers, args = _pop_int_flag(args, "--workers")
    segs, args = _pop_int_flag(args, "--segs", "--seg-workers")
    run_cfg = dict(CFG)
    if workers:
        run_cfg["workers"] = max(1, min(workers, 8))
    if segs is not None:
        # 0 = форсировать прямой ffmpeg без сегментов (диагностика микрофризов)
        run_cfg["seg_workers"] = max(0, min(segs, 32))
    if not args:
        url = input("Вставь ссылку на тайтл: ").strip()
        args = [url] if url else []
    if not args:
        print("Ссылка не указана.")
        sys.exit(1)
    # запуск с ссылкой -> интерактивное меню (озвучка/качество/серии/франшиза)
    grab(args[0], run_cfg, interactive=True)
