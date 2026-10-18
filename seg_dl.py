#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seg_dl — параллельная качка HLS-сегментов с последующей склейкой через ffmpeg.

Зачем: штатный download_hls (ffmpeg -c copy одним коннектом) тянет серию
последовательно, сегмент за сегментом, с TTFB 0.4-0.9с на каждый. При трубе
~95 КБ/с это даёт ~0.6x realtime. Пул из 8-16 потоков качает сегменты
параллельно, затем ffmpeg склеивает их БЕЗ перекодирования
(-f concat -c copy): битрейт и качество untouched.

Особенности сети (из замеров):
  - cloud.solodcdn.com отдаёт манифест, сами сегменты 302-redirect на
    nova.cloud.solodcdn.com; urllib падает на этом 302 (SSLEOF), curl — нет.
    Поэтому: сначала urllib (уважает прокси из env), при неудаче — curl
    через subprocess (тоже уважает env-прокси).
  - DIRECT-маршрут до nova дохнет — всё ходит через прокси из env.

HTTP-бэкенды (1.0.8):
  Старая цепочка urllib -> curl остаётся рабочей по умолчанию и не тронута.
  Добавлен альтернативный бэкенд на httpx — он сам ходит по 302 (follow_redirects)
  и мультиплексирует h2, поэтому на практике curl.exe не нужен вовсе.
    'auto'   — httpx, если пакет импортируется, иначе urllib->curl (по умолчанию)
    'httpx'  — только httpx; если пакета нет — понятная ошибка, НЕ молчаливый откат
    'urllib' — только старая цепочка
  Выбор: core.CFG['http_backend'] (по умолчанию 'auto') либо set_backend() из CLI
  (флаг --backend). httpx опционален: без него работает auto -> urllib -> curl.
  На машине, где системный curl.exe сломан (CRYPT_E_REVOCATION_OFFLINE), httpx —
  единственный живой путь до 302-redirect'ов.

Публичное API:
  resolve_media(m3u8_url) -> (media_url, media_text)   # master -> media
  parse_segments(text, base_url) -> [url]              # EXTINF -> URL
  download_hls_segmented(m3u8_url, out_path, seg_workers=8,
                         progress_cb=None, is_stopped=None) -> size
    progress_cb(done_count, total_count, bytes_done)
    is_stopped: threading.Event или callable. Остановка -> Stopped.
  set_backend('auto'|'httpx'|'urllib'), effective_backend(), httpx_available()
  Ошибка склейки/качки -> SegError (вызывающий код делает fallback).
"""

import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed

import certifi
import imageio_ffmpeg

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/120.0"
REF = "https://kodikplayer.com/"
CTX = ssl.create_default_context(cafile=certifi.where())
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

_TIMEOUT = 30
_RETRIES = 3


class Stopped(Exception):
    """Попросили остановиться (is_stopped). Частичные файлы уже убраны."""


class SegError(RuntimeError):
    """Сегментный путь не вышел — вызывающий код должен пойти fallback'ом."""


def _stopped(is_stopped):
    try:
        if is_stopped is None:
            return False
        if hasattr(is_stopped, "is_set"):
            return bool(is_stopped.is_set())
        return bool(is_stopped())
    except Exception:
        return False


def _curl_bin():
    return shutil.which("curl") or shutil.which("curl.exe")


# ------------------------------------------------------- выбор HTTP-бэкенда
BACKENDS = ("auto", "httpx", "urllib")

_BACKEND_OVERRIDE = None          # задано set_backend() из CLI, иначе None
_BACKEND_LOCK = threading.Lock()

#: httpx-клиент создаётся лениво и переиспользуется всеми потоками пула.
_HTTPX_CLIENT = None
_HTTPX_MAX_CONN = 0
_HTTPX_HTTP2 = None               # True/False — реально ли включился h2
_HTTPX_LOCK = threading.Lock()

_HTTPX_MIN_CONN = 16              # не меньше числа потоков сегментов
_HTTPX_CHUNK = 1 << 16            # размер чанка при стриминге на диск


def _import_httpx():
    """Модуль httpx или None. Отсутствие пакета НЕ должно быть ImportError'ом
    где-то в неожиданном месте — поэтому импорт ловим здесь."""
    try:
        import httpx
        return httpx
    except Exception:
        return None


def httpx_available():
    """True, если пакет httpx установлен (проверка без сети)."""
    return _import_httpx() is not None


def _cfg_backend():
    """Значение из core.CFG['http_backend']; core не импортируем жёстко —
    он и так лежит рядом, но seg_dl должен работать и в одиночку."""
    try:
        import core
        val = (getattr(core, "CFG", None) or {}).get("http_backend")
    except Exception:
        val = None
    val = str(val or "auto").strip().lower()
    return val if val in BACKENDS else "auto"


def set_backend(name):
    """Явно выбрать бэкенд (CLI --backend). None/'auto' — вернуть к конфигу."""
    global _BACKEND_OVERRIDE
    val = (name or "").strip().lower()
    if val and val not in BACKENDS:
        raise SegError("неизвестный HTTP-бэкенд: %r (допустимо %s)"
                       % (name, "|".join(BACKENDS)))
    with _BACKEND_LOCK:
        _BACKEND_OVERRIDE = val or None
    return effective_backend()


def get_backend_override():
    """Что задал set_backend(), либо None (решает конфиг). Для тестов."""
    return _BACKEND_OVERRIDE


def effective_backend():
    """Что реально будет использовано: 'httpx' | 'urllib'.

    Явный 'httpx' без пакета — ошибка, а не тихий откат: молчаливый откат
    скрыл бы от пользователя, что он поставил не то."""
    val = (_BACKEND_OVERRIDE or _cfg_backend()).strip().lower()
    if val not in BACKENDS:
        val = "auto"
    if val == "urllib":
        return "urllib"
    if val == "httpx":
        if not httpx_available():
            raise SegError(
                "бэкенд httpx запрошен, но пакет не установлен: "
                'py -m pip install "httpx[http2]"')
        return "httpx"
    return "httpx" if httpx_available() else "urllib"     # auto


def _build_httpx_client(httpx, max_conn):
    """Клиент с теми же UA/Referer, что и urllib-путь, но с redirect'ами и h2."""
    common = dict(follow_redirects=True,
                  timeout=_TIMEOUT,
                  headers={"User-Agent": UA, "Referer": REF},
                  limits=httpx.Limits(max_connections=max_conn,
                                      max_keepalive_connections=max_conn))
    try:
        # http2 требует пакет h2 (идёт в httpx[http2]); без него — обычный HTTP/1.1
        return httpx.Client(http2=True, **common), True
    except ImportError:
        return httpx.Client(http2=False, **common), False


def get_httpx_client(max_conn=None):
    """Ленивый общий httpx.Client. Потокобезопасен (сам httpx.Client такой)."""
    global _HTTPX_CLIENT, _HTTPX_MAX_CONN, _HTTPX_HTTP2
    httpx = _import_httpx()
    if httpx is None:
        raise SegError('httpx не установлен: py -m pip install "httpx[http2]"')
    want = max(int(max_conn or 0), _HTTPX_MIN_CONN, _HTTPX_MAX_CONN)
    with _HTTPX_LOCK:
        if _HTTPX_CLIENT is not None and _HTTPX_MAX_CONN >= want:
            return _HTTPX_CLIENT
        client, http2 = _build_httpx_client(httpx, want)
        _HTTPX_CLIENT = client
        _HTTPX_MAX_CONN = want
        _HTTPX_HTTP2 = http2
        return _HTTPX_CLIENT


def httpx_http2_enabled():
    """Реально ли поднялся h2 (None — клиент ещё не создан)."""
    return _HTTPX_HTTP2


def _reset_httpx_client():
    """Сбросить клиент (только для тестов: изоляция между кейсами)."""
    global _HTTPX_CLIENT, _HTTPX_MAX_CONN, _HTTPX_HTTP2
    with _HTTPX_LOCK:
        client, _HTTPX_CLIENT = _HTTPX_CLIENT, None
        _HTTPX_MAX_CONN = 0
        _HTTPX_HTTP2 = None
    try:
        if client is not None:
            client.close()
    except Exception:
        pass


def _fetch_bytes_httpx(url, ref=None, timeout=_TIMEOUT):
    """Байты по URL одним httpx-запросом (redirect'ы следуются клиентом)."""
    client = get_httpx_client()
    r = client.get(url, headers={"User-Agent": UA, "Referer": ref or REF},
                   timeout=timeout)
    r.raise_for_status()
    return r.content


def _stream_to_file_httpx(url, out_path, ref=None, timeout=_TIMEOUT):
    """Сегмент стримится прямо на диск — сегменты бывают по несколько МБ,
    буферизовать их в памяти в пуле из 8 потоков незачем."""
    client = get_httpx_client()
    written = 0
    with client.stream("GET", url,
                       headers={"User-Agent": UA, "Referer": ref or REF},
                       timeout=timeout) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for part in r.iter_bytes(_HTTPX_CHUNK):
                f.write(part)
                written += len(part)
    return written


# ------------------------------------------------------------------ fetch
def fetch_bytes(url, ref=None, timeout=_TIMEOUT):
    """Байты по URL. Бэкенд выбирает effective_backend(); старая цепочка
    urllib -> curl остаётся как есть. Иначе исключение."""
    ref = ref or REF
    if effective_backend() == "httpx":
        try:
            return _fetch_bytes_httpx(url, ref, timeout)
        except (Stopped, SegError):
            raise
        except Exception as e:
            raise SegError("httpx: %s" % str(e)[:120])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        try:
            return r.read()
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception as ue:
        curl = _curl_bin()
        if not curl:
            raise SegError("urllib: %s (curl нет)" % str(ue)[:120])
        try:
            p = subprocess.run(
                [curl, "-sL", "--max-time", str(timeout),
                 "-A", UA, "-e", ref, url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 5)
        except Exception as ce:
            raise SegError("curl: %s" % str(ce)[:120])
        if p.returncode != 0 or not p.stdout:
            raise SegError("curl rc=%s err=%s" % (
                p.returncode, (p.stderr or b"")[-120:].decode("utf-8", "replace")))
        return p.stdout


def fetch_text(url, ref=None, timeout=_TIMEOUT):
    return fetch_bytes(url, ref, timeout).decode("utf-8", "replace")


# ------------------------------------------------------------------ parse
_MASTER_RE = re.compile(r"#EXT-X-STREAM-INF:([^\r\n]*)\r?\n\s*([^\r\n\s]+)")
_BW_RE = re.compile(r"BANDWIDTH=(\d+)")


def resolve_media(m3u8_url, ref=None, timeout=_TIMEOUT):
    """(media_url, media_text). Мастер-плейлист -> вариант с макс. битрейтом."""
    text = fetch_text(m3u8_url, ref, timeout)
    if "#EXT-X-STREAM-INF" not in text:
        return m3u8_url, text
    best, best_bw = None, -1
    for m in _MASTER_RE.finditer(text):
        bw = _BW_RE.search(m.group(1))
        bw = int(bw.group(1)) if bw else 0
        if bw >= best_bw:
            best_bw, best = bw, m.group(2).strip()
    if not best:
        raise SegError("мастер-плейлист без вариантов")
    media_url = urllib.parse.urljoin(m3u8_url, best)
    return media_url, fetch_text(media_url, ref, timeout)


def parse_segments(text, base_url):
    """Медиа-плейлист -> [абсолютный URL] по порядку."""
    segs = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            segs.append(urllib.parse.urljoin(base_url, s))
    return segs


_KEY_RE = re.compile(r"#EXT-X-KEY:[^\r\n]*METHOD=([^,\s]+)", re.I)


def playlist_needs_fallback(media_text):
    """Плейлисты, которые сегментным путем не тянем.

    Возвращает строку-причину (и вызывающий код должен уйти в fallback
    на прямой ffmpeg с нативным HLS-демуксером), либо None."""
    txt = media_text or ""
    m = _KEY_RE.search(txt)
    if m and m.group(1).strip().upper() not in ("NONE",):
        return "encrypted HLS (%s)" % m.group(1).strip()
    if "#EXT-X-MAP" in txt:
        return "fmp4 init segment (EXT-X-MAP)"
    if "#EXT-X-DISCONTINUITY" in txt or "#EXT-X-GAP" in txt:
        return "discontinuity/gap (рвутся PTS, нужен нативный HLS-демуксер)"
    return None


def _is_mpegts(path):
    """Грубая проверка: .ts должен начинаться с sync-byte 0x47."""
    try:
        with open(path, "rb") as f:
            return f.read(1) == b"\x47"
    except Exception:
        return False


# ------------------------------------------------------------------ download
def download_one(url, ref=None, timeout=_TIMEOUT, retries=_RETRIES, out_path=None):
    """Один сегмент, ретраи + backoff. Иначе SegError.

    out_path задан и бэкенд httpx -> сегмент пишется на диск потоком
    (client.stream + чанки), возвращается размер. Иначе (urllib->curl, или httpx
    без out_path) поведение прежнее: возвращаются байты."""
    ref = ref or REF
    backend = effective_backend()
    streaming = backend == "httpx" and out_path is not None
    last = "?"
    for attempt in range(1, max(1, retries) + 1):
        if backend == "httpx":
            try:
                if streaming:
                    n = _stream_to_file_httpx(url, out_path, ref, timeout)
                    if n:
                        return n
                else:
                    data = _fetch_bytes_httpx(url, ref, timeout)
                    if data:
                        return data
                last = "пусто"
            except Exception as e:
                last = "httpx: %s" % str(e)[:100]
        else:
            # попытка A: urllib
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
                r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
                try:
                    data = r.read()
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass
                if data:
                    return data
                last = "пусто"
            except Exception as e:
                last = "urllib: %s" % str(e)[:100]
            # попытка B: curl
            curl = _curl_bin()
            if curl:
                try:
                    p = subprocess.run(
                        [curl, "-sL", "--max-time", str(timeout),
                         "-A", UA, "-e", ref, "-o", "-", url],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=timeout + 5)
                    if p.returncode == 0 and p.stdout:
                        return p.stdout
                    last = "curl rc=%s" % p.returncode
                except Exception as e:
                    last = "curl: %s" % str(e)[:100]
        if attempt < retries:
            time.sleep(attempt)  # backoff 1с, 2с, ...
    raise SegError("сегмент не скачался: %s" % last)


def download_segments(seg_urls, tmpdir, ref=None, seg_workers=8,
                      retries=_RETRIES, progress_cb=None, is_stopped=None):
    """Пул потоков -> [путь к сегменту] строго по порядку. Иначе Stopped/SegError."""
    ref = ref or REF
    n = len(seg_urls)
    if not n:
        raise SegError("пустой плейлист")
    workers = max(1, min(int(seg_workers or 8), n))
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except Exception as e:
        raise SegError("нет доступа к tmp: %s" % e)
    paths = [os.path.join(tmpdir, "seg-%05d.ts" % i) for i in range(n)]
    lock = threading.Lock()
    state = {"done": 0, "bytes": 0}
    backend = effective_backend()
    # лимит соединений клиента не должен быть меньше числа потоков
    if backend == "httpx":
        get_httpx_client(max_conn=workers)

    def job(i, url):
        if _stopped(is_stopped):
            raise Stopped()
        if backend == "httpx":
            # стрим прямо на диск: сегменты в памяти не копим
            got = download_one(url, ref, _TIMEOUT, retries, out_path=paths[i])
            size = got
        else:
            data = download_one(url, ref, _TIMEOUT, retries)
            with open(paths[i], "wb") as f:
                f.write(data)
            size = len(data)
        with lock:
            state["done"] += 1
            state["bytes"] += size
            if progress_cb:
                try:
                    progress_cb(state["done"], n, state["bytes"])
                except Exception:
                    pass
        return i

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="seg") as ex:
        futs = {ex.submit(job, i, u): i for i, u in enumerate(seg_urls)}
        try:
            for f in as_completed(futs):
                f.result()  # исключение -> сразу наружу
                if _stopped(is_stopped):
                    raise Stopped()
        except BaseException:
            for f in futs:
                try:
                    f.cancel()
                except Exception:
                    pass
            raise
    for p in paths:
        try:
            if os.path.getsize(p) <= 0:
                raise SegError("битый сегмент после качки")
        except OSError as e:
            raise SegError("нет сегмента: %s" % e)
    return paths


def _run(cmd, cwd=None):
    """Запустить ffmpeg, вернуть (rc, stderr_tail)."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", timeout=600, cwd=cwd)
        err = (r.stderr or "").strip().splitlines()
        tail = err[-1][:200] if err else ""
        return r.returncode, tail, os.path.exists(cmd[-1]) if cmd else False
    except Exception as e:
        return 1, str(e)[:200], False


def concat_copy(part_files, out_tmp):
    """Склейка без перекодирования. True/False (False -> вызывающий код fallback'ит).

    v2: concat-демуксер даже с +genpts оставляет микрофризы на стыках .ts.
    Поэтому основной путь — бинарный кат всех .ts в один combined.ts
    (HLS-сегменты для этого и спроектированы) + один ремукс
    `ffmpeg -i combined.ts -c copy`. Это ближе к нативному HLS-демуксеру,
    чем склейка по файлам. Не вышло — пробуем старый concat-демуксер,
    не вышло и он — False (caller уйдет на прямой ffmpeg с m3u8).
    Не-TS файлы (нет sync-byte 0x47) сразу в fallback."""
    if not part_files:
        return False
    partdir = os.path.dirname(os.path.abspath(part_files[0]))
    lst = os.path.join(partdir, "concat.txt")
    combined = os.path.join(partdir, "combined.ts")
    out_abs = os.path.abspath(out_tmp)
    try:
        for p in part_files:
            try:
                if os.path.getsize(p) <= 0 or not _is_mpegts(p):
                    return False
            except OSError:
                return False
        # 1) бинарный кат -> combined.ts
        try:
            with open(combined, "wb") as out:
                for p in part_files:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, out, length=1024 * 256)
        except Exception:
            return False
        # 2) один ремукс combined.ts -> mp4
        cmd = [FFMPEG, "-y", "-loglevel", "error",
               "-fflags", "+genpts+discardcorrupt",
               "-i", combined,
               "-c", "copy", "-bsf:a", "aac_adtstoasc", out_abs]
        rc, tail, _ = _run(cmd)
        if rc == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) >= 10000:
            return True
        last_tail = tail
        # 3) запасной: concat-демуксер с пересчетом меток
        try:
            with open(lst, "w", encoding="utf-8") as f:
                for p in part_files:
                    name = os.path.basename(p).replace("'", "'\\''")
                    f.write("file '%s'\n" % name)
            cmd2 = [FFMPEG, "-y", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", os.path.basename(lst),
                    "-fflags", "+genpts+igndts", "-avoid_negative_ts", "make_zero",
                    "-c", "copy", "-bsf:a", "aac_adtstoasc", out_abs]
            rc2, tail2, _ = _run(cmd2, cwd=partdir)
            if rc2 == 0 and os.path.exists(out_abs) and os.path.getsize(out_abs) >= 10000:
                return True
            last_tail = tail2 or last_tail
        except Exception:
            pass
        try:
            if os.path.exists(out_abs):
                os.remove(out_abs)
        except Exception:
            pass
        # причину оставляем в файле-марке для диагностики caller'а через исключение
        concat_copy.last_error = last_tail or "ffmpeg remux failed"
        return False
    except Exception:
        return False
    finally:
        for tmp in (lst, combined):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


concat_copy.last_error = ""


def download_hls_segmented(m3u8_url, out_path, seg_workers=8, ref=None,
                           retries=_RETRIES, progress_cb=None, is_stopped=None):
    """Весь pipeline: плейлист -> сегменты пулом -> concat -> out_path.
    Возвращает размер файла. Stopped при остановке, SegError иначе."""
    ref = ref or REF
    if _stopped(is_stopped):
        raise Stopped()
    try:
        media_url, media = resolve_media(m3u8_url, ref)
    except Stopped:
        raise
    except Exception as e:
        raise SegError("плейлист: %s" % str(e)[:150])
    reason = playlist_needs_fallback(media)
    if reason:
        raise SegError("fallback нужен: %s" % reason)
    segs = parse_segments(media, media_url)
    if not segs:
        raise SegError("в плейлисте нет сегментов")
    tmpdir = out_path + ".segs"
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    tmp = out_path + ".part.mp4"
    try:
        parts = download_segments(segs, tmpdir, ref, seg_workers,
                                  retries, progress_cb, is_stopped)
        if _stopped(is_stopped):
            raise Stopped()
        if not concat_copy(parts, tmp):
            detail = getattr(concat_copy, "last_error", "") or ""
            raise SegError("ffmpeg concat не смог склеить%s" % (
                (": " + detail[:120]) if detail else ""))
        os.replace(tmp, out_path)
        return os.path.getsize(out_path)
    except (Stopped, SegError):
        raise
    except Exception as e:
        raise SegError(str(e)[:150])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            if os.path.exists(tmp) and not os.path.exists(out_path):
                pass  # part-файл оставляем решению вызывающего кода
        except Exception:
            pass
