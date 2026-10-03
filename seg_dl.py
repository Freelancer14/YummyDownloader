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

Публичное API:
  resolve_media(m3u8_url) -> (media_url, media_text)   # master -> media
  parse_segments(text, base_url) -> [url]              # EXTINF -> URL
  download_hls_segmented(m3u8_url, out_path, seg_workers=8,
                         progress_cb=None, is_stopped=None) -> size
    progress_cb(done_count, total_count, bytes_done)
    is_stopped: threading.Event или callable. Остановка -> Stopped.
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


# ------------------------------------------------------------------ fetch
def fetch_bytes(url, ref=None, timeout=_TIMEOUT):
    """Байты по URL: сначала urllib, при неудаче — curl. Иначе исключение."""
    ref = ref or REF
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


# ------------------------------------------------------------------ download
def download_one(url, ref=None, timeout=_TIMEOUT, retries=_RETRIES):
    """Один сегмент в память, ретраи + backoff. Иначе SegError."""
    ref = ref or REF
    last = "?"
    for attempt in range(1, max(1, retries) + 1):
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

    def job(i, url):
        if _stopped(is_stopped):
            raise Stopped()
        data = download_one(url, ref, _TIMEOUT, retries)
        with open(paths[i], "wb") as f:
            f.write(data)
        with lock:
            state["done"] += 1
            state["bytes"] += len(data)
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


def concat_copy(part_files, out_tmp):
    """Склейка без перекодирования. True/False (False -> вызывающий код fallback'ит)."""
    if not part_files:
        return False
    partdir = os.path.dirname(os.path.abspath(part_files[0]))
    lst = os.path.join(partdir, "concat.txt")
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in part_files:
                name = os.path.basename(p).replace("'", "'\\''")
                f.write("file '%s'\n" % name)
        # относительные имена в списке -> работаем в cwd=tmpdir
        cmd = [FFMPEG, "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", os.path.basename(lst),
               "-c", "copy", os.path.abspath(out_tmp)]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=600, cwd=partdir)
        ok = (r.returncode == 0 and os.path.exists(out_tmp)
              and os.path.getsize(out_tmp) >= 10000)
        return ok
    except Exception:
        return False
    finally:
        try:
            if os.path.exists(lst):
                os.remove(lst)
        except Exception:
            pass


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
            raise SegError("ffmpeg concat не смог склеить")
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
