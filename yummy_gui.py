#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YummyDownloader — GUI-прототип (фаза 1).

Только GUI-слой поверх yummy_dl.py. Ядро не меняем, CLI работает как раньше.
Логика: ссылка -> Проверить -> настройки -> Скачать. Качка в отдельном
потоке, статусы в окно идут через queue.Queue + root.after(100).

Запуск:  py yummy_gui.py
Требуется только стандартная библиотека (tkinter входит в Python 3.9).
"""

import os
import sys
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import ttk, filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import yummy_dl

try:
    import seg_dl
except ImportError:
    seg_dl = None


# ---------------------------------------------------------------- helpers
# Чистые функции без сети и без окна — их дергает и интерфейс, и smoke-тест.

def validate_url(url):
    """Грубая проверка ссылки. Возвращает (ok, message)."""
    url = (url or "").strip()
    if not url:
        return False, "Вставь ссылку на тайтл."
    if not url.startswith("http"):
        return False, "Ссылка должна начинаться с http(s)."
    if "yummy" not in url.lower() and "anime" not in url.lower():
        return False, "Похоже, это не ссылка на YummyAnime. Проверь адрес."
    return True, ""


def parse_episodes_text(text, available):
    """Разобрать поле серий через ядро. Пусто = все.
    Возвращает (ok, keep_set, message). Сеть не нужна."""
    text = (text or "").strip()
    if not text:
        return True, set(available), ""
    allowed = set("0123456789,- \t*")
    low = text.lower()
    if low in ("all", "все"):
        return True, set(available), ""
    for ch in text:
        if ch not in allowed:
            return False, set(), "Можно только цифры, запятые и дефис. Пример: 5-12 или 3,7,9."
    try:
        keep = yummy_dl.parse_ep_range(text, available)
    except Exception:
        return False, set(), "Не понял диапазон. Пример: 5-12 или 3,7,9. Пусто = все."
    if not keep:
        return False, set(), "Таких серий нет в этой озвучке."
    return True, keep, ""


def build_plan(page_id, info, groups, dub, quality, ep_text, want_franchise, cfg):
    """Собрать очередь качки из уже проверенных данных. Сети нет.
    Возвращает (tasks, message), где tasks — список словарей на каждый тайтл."""
    eps_avail = sorted(groups[dub])
    ok, keep, msg = parse_episodes_text(ep_text, eps_avail)
    if not ok:
        return None, msg
    tasks = [{
        "anime_id": page_id,
        "title_hint": info.get("title", "?"),
        "dub": dub,
        "quality": quality,
        "keep": keep,
        "franchise_dir": None,
    }]
    if want_franchise:
        vo = info.get("viewing_order") or []
        seasons = [v for v in vo if v.get("anime_id") and v["anime_id"] != page_id]
        base_name = yummy_dl.sanitize((vo[0].get("title") if vo else None) or info.get("title") or "franchise")
        franchise_dir = os.path.join(cfg["output_dir"], base_name)
        for v in seasons:
            tasks.append({
                "anime_id": v["anime_id"],
                "title_hint": v.get("title", "?"),
                "dub": dub,
                "quality": quality,
                "keep": None,  # остальные сезоны — целиком
                "franchise_dir": franchise_dir,
            })
    return tasks, ""


# ------------------------------------------- размеры/прогресс/скорость
# Чистые функции без сети и окна: форматирование, разбор m3u8 и вывода
# ffmpeg, замер скорости. Ядро yummy_dl.py не трогаем.


def fmt_mb(n):
    """Байты -> '180 МБ' / '2.1 ГБ' / '512 КБ'."""
    try:
        n = max(0, float(n or 0))
    except Exception:
        return "0 МБ"
    if n >= 1024 ** 3:
        return "%.1f ГБ" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        mb = n / 1024 ** 2
        return "%d МБ" % int(round(mb)) if mb >= 100 else "%.1f МБ" % mb
    return "%d КБ" % int(round(n / 1024))


def fmt_pair(cur, total):
    """Пара '82/180 МБ' в одной единице (по итоговому размеру)."""
    try:
        total = max(1, float(total))
        cur = max(0, float(cur or 0))
    except Exception:
        return "?/?"
    if total >= 1024 ** 3:
        return "%.1f/%.1f ГБ" % (cur / 1024 ** 3, total / 1024 ** 3)
    return "%d/%d МБ" % (int(round(cur / 1024 ** 2)), int(round(total / 1024 ** 2)))


def fmt_speed(bps):
    """Байт/с -> '3.2 МБ/с'. Ноль и мусор -> прочерк."""
    try:
        bps = float(bps or 0)
    except Exception:
        return "—"
    if bps <= 0:
        return "—"
    m = bps / 1024 ** 2
    if m >= 100:
        return "%d МБ/с" % int(round(m))
    if m >= 1:
        return "%.1f МБ/с" % m
    return "%d КБ/с" % int(round(bps / 1024))


class SpeedTracker:
    """Скользящее окно: помним (время, всего байт), скорость — наклон за окно."""

    def __init__(self, window=3.0):
        self.window = float(window)
        self.samples = []

    def add(self, t, total):
        if self.samples and t - self.samples[-1][0] < 0.25:
            return  # слишком частые точки врут про скорость — пропускаем
        self.samples.append((t, total))
        cut = t - self.window
        self.samples = [s for s in self.samples if s[0] >= cut]

    def rate(self):
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0
        r = (b1 - b0) / dt
        return r if r > 0 else 0.0

    def reset(self):
        self.samples = []


def parse_progress_value(line):
    """Одна строка '-progress pipe:1' -> ('time_ms', N) / ('progress', s) / None."""
    s = (line or "").strip()
    if s.startswith("out_time_ms="):
        try:
            return ("time_ms", int(s.split("=", 1)[1]))
        except Exception:
            return None
    if s.startswith("progress="):
        return ("progress", s.split("=", 1)[1])
    return None


_MASTER_RE = re.compile(r"#EXT-X-STREAM-INF:([^\r\n]*)\r?\n\s*([^\r\n\s]+)")
_BW_RE = re.compile(r"BANDWIDTH=(\d+)")
_EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")


def parse_master_variants(text):
    """Мастер-плейлист -> [(bandwidth, uri)] по возрастанию."""
    out = []
    for m in _MASTER_RE.finditer(text or ""):
        bw = _BW_RE.search(m.group(1))
        out.append((int(bw.group(1)) if bw else 0, m.group(2).strip()))
    out.sort()
    return out


def parse_media_duration(text):
    """Медиа-плейлист -> (длительность_сек, число_сегментов)."""
    total, n = 0.0, 0
    for m in _EXTINF_RE.finditer(text or ""):
        try:
            total += float(m.group(1))
            n += 1
        except Exception:
            pass
    return total, n


def parse_media_segments(text, base_url):
    """Медиа-плейлист -> [(длительность|None, абсолютный_url)] по порядку."""
    segs, last_dur = [], None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("#EXTINF:"):
            try:
                last_dur = float(s[8:].split(",")[0])
            except Exception:
                last_dur = None
        elif s and not s.startswith("#"):
            segs.append((last_dur, urllib.parse.urljoin(base_url, s)))
            last_dur = None
    return segs


def estimate_total_bytes(segments):
    """Грубая оценка итога: размер первого сегмента x число сегментов.
    HEAD, запасной вариант — Range. Не вышло -> None (покажем только МБ)."""
    if not segments:
        return None
    url = segments[0][1]
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": yummy_dl.UA, "Referer": "https://kodikplayer.com/"})
        r = urllib.request.urlopen(req, timeout=10, context=yummy_dl.CTX)
        size = r.headers.get("Content-Length")
        try:
            r.close()
        except Exception:
            pass
        if size and int(size) > 0:
            return int(size) * len(segments)
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": yummy_dl.UA, "Referer": "https://kodikplayer.com/",
            "Range": "bytes=0-0"})
        r = urllib.request.urlopen(req, timeout=10, context=yummy_dl.CTX)
        cr = r.headers.get("Content-Range") or ""  # вид: bytes 0-0/12345
        try:
            r.close()
        except Exception:
            pass
        m = re.search(r"/(\d+)\s*$", cr)
        if m and int(m.group(1)) > 0:
            one = int(m.group(1))
            # Range 0-0 на сегмент не дает размер сегмента — эвристика:
            # средний сегмент ~2 МБ на 720p; честно возвращаем None,
            # чтобы не врать. (Ветка оставлена для будущих оценок.)
            _ = one
            return None
    except Exception:
        pass
    return None


def fetch_media_info(m3u8_url):
    """(длительность_сек|None, оценка_итога_байт|None). Любая ошибка -> (None, None)."""
    try:
        text = yummy_dl.http_get(m3u8_url, ref=yummy_dl.SITE + "/")
    except Exception:
        return None, None
    try:
        if "#EXT-X-STREAM-INF" in text:
            variants = parse_master_variants(text)
            if not variants:
                return None, None
            media_url = urllib.parse.urljoin(m3u8_url, variants[-1][1])
            try:
                media = yummy_dl.http_get(media_url, ref=yummy_dl.SITE + "/")
            except Exception:
                return None, None
            base = media_url
        else:
            media, base = text, m3u8_url
        dur, _n = parse_media_duration(media)
        est = estimate_total_bytes(parse_media_segments(media, base))
        return (dur or None), est
    except Exception:
        return None, None


class _StopDownload(Exception):
    pass


def gui_download_hls(m3u8_url, out_path, emit=None, stop_flag=None, attempts=3,
                     seg_workers=8):
    """Качка серии с прогрессом через emit(cur, total_est, pct, speed).

    Сначала сегментный путь (seg_dl: пул потоков + concat без перекодирования,
    прогресс — по готовым сегментам/total, МБ и скорость через SpeedTracker).
    При неудаче — старый путь: ffmpeg -c copy напрямую с -progress pipe:1.
    stop_flag: threading.Event — текущая дожимается, дальше стоп."""
    if seg_dl is not None and not (
            stop_flag is not None and stop_flag.is_set()):
        tracker = SpeedTracker(3.0)
        last_emit = [0.0]

        def _seg_emit(done_c, total_c, bytes_done):
            now = time.time()
            est = None
            if done_c > 0 and total_c > 0:
                try:
                    est = int(bytes_done / done_c * total_c)
                except Exception:
                    est = None
            pct = (done_c / total_c * 100.0) if total_c else None
            tracker.add(now, bytes_done)
            if emit and now - last_emit[0] >= 0.5:
                last_emit[0] = now
                try:
                    emit(bytes_done, est, pct, tracker.rate())
                except Exception:
                    pass

        try:
            size = seg_dl.download_hls_segmented(
                m3u8_url, out_path, seg_workers=max(1, int(seg_workers or 8)),
                progress_cb=_seg_emit, is_stopped=stop_flag)
            if emit:
                try:
                    emit(size, size, 100.0, 0.0)
                except Exception:
                    pass
            return size
        except seg_dl.Stopped:
            raise _StopDownload()
        except Exception:
            pass  # ниже fallback: старый ffmpeg-путь
    tmp = out_path + ".part.mp4"
    cmd = [yummy_dl.FFMPEG, "-y", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1",
           "-referer", "https://kodikplayer.com/", "-user_agent", yummy_dl.UA,
           "-multiple_requests", "1", "-reconnect", "1",
           "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
           "-reconnect_delay_max", "5", "-rw_timeout", "30000000",
           "-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", tmp]
    dur, total_est = fetch_media_info(m3u8_url)
    tracker = SpeedTracker(3.0)
    last_emit = [0.0]

    def send(pct):
        now = time.time()
        if now - last_emit[0] < 0.5:
            return
        last_emit[0] = now
        try:
            cur = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        except Exception:
            cur = 0
        tracker.add(now, cur)
        if emit:
            try:
                emit(cur, total_est, pct, tracker.rate())
            except Exception:
                pass

    last_err = "?"
    for attempt in range(1, attempts + 1):
        if stop_flag is not None and stop_flag.is_set():
            raise _StopDownload()
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        send(None)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                parsed = parse_progress_value(line)
                if parsed and parsed[0] == "time_ms":
                    pct = None
                    if dur:
                        try:
                            pct = min(99.0, (parsed[1] / 1000000.0) / dur * 100.0)
                        except Exception:
                            pct = None
                    elif total_est:
                        try:
                            cur = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                            pct = min(99.0, cur / float(total_est) * 100.0)
                        except Exception:
                            pct = None
                    send(pct)
                elif parsed == ("progress", "end"):
                    break
            err = proc.stderr.read() or ""
            rc = proc.wait()
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        ok = rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) >= 10000
        if ok:
            os.replace(tmp, out_path)
            try:
                size = os.path.getsize(out_path)
            except Exception:
                size = 0
            if emit:
                try:
                    emit(size, total_est, 100.0, 0.0)
                except Exception:
                    pass
            return size
        last_err = err.strip().splitlines()[-1] if err.strip() else "?"
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError("ffmpeg не справился: " + last_err)


# ------------------------------------------------- вставка в поля ввода
# Клавиша V/C/X/A при русской раскладке дает Tk другой keysym
# (Cyrillic_em вместо v и т.д.), поэтому штатный Ctrl+V в Entry молча
# не срабатывает. Маппинг ниже чинит вставку/копирование/вырезание
# без вмешательства в латинскую раскладку (там отдаем Tk по умолчанию).

# Физические клавиши V/C/X/A: на Windows event.keycode — это VK-код,
# одинаковый при любой раскладке (86/67/88/65). Keysym же зависит от языка
# (Cyrillic_em вместо v и т.д.), поэтому штатный Ctrl+V в Entry молча
# не срабатывает. Сначала смотрим keycode, keysym — запасной путь.

_WIN_CTRL_KEYCODES = {
    86: "paste",       # V — вставить
    67: "copy",        # C — копировать
    88: "cut",         # X — вырезать
    65: "select_all",  # A — выделить все
}

_RU_CTRL_ACTIONS = {
    "cyrillic_em": "paste",  # клавиша V — вставить
    "cyrillic_es": "copy",   # клавиша C — копировать
    "cyrillic_che": "cut",   # клавиша X — вырезать (позиция X = Ч, не Х)
    "cyrillic_ha": "cut",    # оставляем: вдруг Tk отдаст Х
    "cyrillic_ef": "select_all",  # клавиша A — выделить все
    # те же буквы прямым текстом — на всякий случай, если Tk отдаст символ
    "м": "paste", "М": "paste",
    "с": "copy", "С": "copy",
    "ч": "cut", "Ч": "cut",
    "х": "cut", "Х": "cut",
    "ф": "select_all", "Ф": "select_all",
}


def ctrl_key_action(keysym, keycode=None):
    """Что делать при Ctrl+клавиша в поле ввода.
    Возвращает 'paste'/'copy'/'cut'/'select_all' или None (пусть Tk сам)."""
    if keycode is not None and sys.platform.startswith("win"):
        try:
            hit = _WIN_CTRL_KEYCODES.get(int(keycode))
        except (TypeError, ValueError):
            hit = None
        if hit:
            return hit
    if not keysym:
        return None
    hit = _RU_CTRL_ACTIONS.get(keysym) or _RU_CTRL_ACTIONS.get(keysym.lower())
    return hit


# ------------------------------------------------------- зависимости (1 клик)
# Кнопка «Установить зависимости» для запуска в 1 клик: тихая проверка
# импорта при старте (без автоустановки) + pip install в отдельном потоке.

# pip-название -> import-модуль
_DEPS_IMPORT = (("certifi", "certifi"),
               ("imageio-ffmpeg", "imageio_ffmpeg"))


def requirements_path():
    """requirements.txt рядом с этим файлом."""
    return os.path.join(HERE, "requirements.txt")


def deps_status():
    """Тихая проверка: (ok, label). Сети нет, ничего не ставим."""
    missing = []
    for pip_name, mod in _DEPS_IMPORT:
        try:
            __import__(mod)
        except Exception:
            missing.append(pip_name)
    if not missing:
        return True, "зависимости: ok"
    return False, "зависимости: нет " + ", ".join(missing)


def pip_install_cmd():
    """Команда установки: [python, -m, pip, install, -r, requirements.txt]."""
    return [sys.executable, "-m", "pip", "install", "-r", requirements_path()]


def run_pip_install(runner=None):
    """Запустить pip install. Возвращает (ok, message). Сеть нужна.
    runner — для тестов (мок вместо subprocess.run)."""
    req = requirements_path()
    if not os.path.exists(req):
        return False, "ошибка: нет файла requirements.txt рядом с программой"
    run = runner or subprocess.run
    try:
        r = run(pip_install_cmd(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=600)
    except Exception as e:
        return False, "ошибка: %s" % e
    rc = getattr(r, "returncode", 1)
    if rc == 0:
        return True, "готово"
    out = (getattr(r, "stdout", "") or "").strip().splitlines()
    tail = out[-1][:160] if out else "?"
    return False, "ошибка: " + tail


def install_deps_worker(q):
    """Фоновая установка: pip в потоке, итог — событием в очередь окна."""
    try:
        ok, msg = run_pip_install()
    except Exception as e:
        ok, msg = False, "ошибка: %s" % e
    try:
        q.put(("deps_done", {"ok": ok, "msg": msg}))
    except Exception:
        pass


# ------------------------------------------------------- background workers
# Работают в отдельном потоке, с окном общаются только через очередь событий.

def check_worker(url, cfg, q):
    """Проверить ссылку: page_id -> info -> videos -> группы озвучек."""
    try:
        q.put(("status", "Проверяю ссылку..."))
        if yummy_dl.is_tv_url(url):
            enum = yummy_dl.tv_enumerate(url)
            groups = {}
            for name, di in (enum.get("dubs") or {}).items():
                eps = {1: None} if di.get("single") else (di.get("eps") or {})
                if eps:
                    groups[name] = sorted(eps)
            title = enum.get("title") or ("tv_%d" % enum.get("id", 0))
            if not groups:
                q.put(("check_empty", {"page_id": enum.get("id", 0),
                                       "info": {"title": title}, "title": title}))
                return
            names = sorted(groups, key=lambda n: -len(groups[n]))
            default_dub = yummy_dl.choose_dubbing(groups, cfg["preferred_dubbings"])
            q.put(("check_done", {
                "page_id": enum.get("id", 0), "info": {"title": title},
                "title": title, "names": names, "default_dub": default_dub,
                "groups": groups, "franchise_count": 0,
                "is_tv": True, "tv_enum": enum,
            }))
            return
        page_id = yummy_dl.resolve_page_id(url)
        info = yummy_dl.api("/anime/%d" % page_id)
        items = yummy_dl.api("/anime/%d/videos" % page_id)
        groups = yummy_dl.kodik_groups(items)
        title = info.get("title") or ("anime_%d" % page_id)
        if not groups:
            q.put(("check_empty", {"page_id": page_id, "info": info, "title": title}))
            return
        names = sorted(groups, key=lambda n: -len(groups[n]))
        default_dub = yummy_dl.choose_dubbing(groups, cfg["preferred_dubbings"])
        vo = info.get("viewing_order") or []
        others = [v for v in vo if v.get("anime_id") and v["anime_id"] != page_id]
        q.put(("check_done", {
            "page_id": page_id, "info": info, "title": title,
            "names": names, "default_dub": default_dub,
            "groups": {n: sorted(groups[n]) for n in names},
            "franchise_count": len(others),
        }))
    except Exception as e:
        q.put(("check_error", str(e)))


def _gui_one_ep(q, num, title, iframe, quality, out, seg_workers,
                stop_flag, lock, done_box):
    """Одна серия в пуле потоков: resume -> m3u8 -> gui_download_hls.
    Все статусы — событиями в очередь; ошибка серии очередь не стопает."""
    if stop_flag.is_set():
        return
    if os.path.exists(out):
        try:
            have = os.path.getsize(out)
        except Exception:
            have = 0
        if have > 10000:
            q.put(("ep_skip", {"num": num, "title": title, "size": have}))
            with lock:
                done_box[0] += 1
                q.put(("overall", {"done": done_box[0]}))
            return
    q.put(("ep_start", {"num": num, "title": title}))
    prog = {"cur": 0}

    def _emit(cur, total_est, pct, speed):
        prog["cur"] = max(prog["cur"], cur)
        q.put(("ep_prog", {"num": num, "title": title, "cur": cur,
                           "total_est": total_est, "pct": pct,
                           "speed": speed}))

    try:
        m3u8, qp = (yummy_dl.tv_m3u8(iframe[1], iframe[2], iframe[3],
                                     iframe[4], quality)
                    if isinstance(iframe, tuple) and iframe and iframe[0] == "tv"
                    else yummy_dl.kodik_m3u8(iframe, quality))
        size = gui_download_hls(m3u8, out, emit=_emit, stop_flag=stop_flag,
                                seg_workers=seg_workers)
        q.put(("ep_done", {"num": num, "title": title,
                           "size": size, "q": qp}))
    except _StopDownload:
        return  # outer по флагу положит ("stopped", ...) один раз
    except Exception as e:
        q.put(("ep_error", {"num": num, "title": title,
                            "error": str(e),
                            "downloaded": prog["cur"]}))
    with lock:
        done_box[0] += 1
        q.put(("overall", {"done": done_box[0]}))


def download_worker(tasks, cfg, q, stop_flag):
    """Очередь тайтлов: подготовка последовательно, серии — пулом потоков.
    Ошибка одной серии не стопает очередь. stop_flag: новые не стартуем,
    текущие дожимаются, дальше стоп."""
    workers = max(1, int(cfg.get("workers", 4) or 4))
    seg_workers = max(1, int(cfg.get("seg_workers", 8) or 8))
    lock = threading.Lock()
    done_box = [0]
    try:
        total = len(tasks[0]["keep"]) if tasks[0]["keep"] else 0
        main_nums = sorted(tasks[0]["keep"]) if tasks[0]["keep"] else []
        q.put(("plan", {"nums": main_nums, "total_hint": total}))
        for ti, t in enumerate(tasks):
            if stop_flag.is_set():
                q.put(("stopped", "Остановлено. Уже скачанное сохранено."))
                return
            if t.get("tv"):
                enum = t["tv_enum"]
                title = yummy_dl.sanitize(enum.get("title") or t["title_hint"])
                dubs = enum.get("dubs") or {}
                di = dubs.get(t["dub"])
                dub = t["dub"] if di is not None else yummy_dl.choose_dubbing(
                    {n: (d.get("eps") or {1: None}) for n, d in dubs.items()},
                    cfg["preferred_dubbings"])
                di = dubs.get(dub)
                if di is None:
                    q.put(("title_error", {"hint": title, "error": "нет озвучек"}))
                    continue
                if di.get("single"):
                    eps = {1: di["single"]}
                else:
                    eps = {n: ("tv", di["sig"], eid, ehash, di["ref"])
                           for n, (eid, ehash) in (di.get("eps") or {}).items()}
                nums = sorted(eps)
                if t["keep"]:
                    keep = set(t["keep"]) & set(nums)
                    nums = sorted(keep) if keep else []
                base = t["franchise_dir"] or cfg["output_dir"]
                out_dir = os.path.join(base, title)
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    with open(os.path.join(out_dir, "_озвучка.txt"), "w", encoding="utf-8") as f:
                        f.write("%s\nОзвучка: %s\nПлеер: %s (yummyanime.tv)\nИсточник: %s\n" % (
                            title, dub, enum.get("slot"), enum.get("card_url")))
                except Exception as e:
                    q.put(("title_error", {"hint": title, "error": "нет доступа к папке: %s" % e}))
                    continue
            else:
                try:
                    info = yummy_dl.api("/anime/%d" % t["anime_id"])
                    items = yummy_dl.api("/anime/%d/videos" % t["anime_id"])
                except Exception as e:
                    q.put(("title_error", {"hint": t["title_hint"], "error": str(e)}))
                    continue
                title = yummy_dl.sanitize(info.get("title") or t["title_hint"])
                groups = yummy_dl.kodik_groups(items)
                if not groups:
                    q.put(("title_error", {"hint": title, "error": "в Kodik нет озвучек"}))
                    continue
                dub = t["dub"] if t["dub"] in groups else yummy_dl.choose_dubbing(
                    groups, cfg["preferred_dubbings"])
                eps = groups[dub]
                nums = sorted(eps)
                if t["keep"]:
                    keep = set(t["keep"]) & set(nums)
                    nums = sorted(keep) if keep else []
                base = t["franchise_dir"] or cfg["output_dir"]
                out_dir = os.path.join(base, title)
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    with open(os.path.join(out_dir, "_озвучка.txt"), "w", encoding="utf-8") as f:
                        f.write("%s\nОзвучка: %s\nИсточник: %s\n" % (title, dub, yummy_dl.SITE))
                except Exception as e:
                    q.put(("title_error", {"hint": title, "error": "нет доступа к папке: %s" % e}))
                    continue
            if ti > 0:
                # сезоны франшизы показываем как продолжение очереди
                q.put(("season", {"title": title, "nums": nums, "dub": dub}))
            q.put(("title_start", {"title": title, "dub": dub,
                                   "count": len(nums), "quality": t["quality"]}))
            if not nums:
                continue
            w = min(workers, len(nums))
            if w <= 1:
                for num in nums:
                    if stop_flag.is_set():
                        break
                    _gui_one_ep(q, num, title, eps[num], t["quality"],
                                os.path.join(out_dir, "Серия %02d.mp4" % num),
                                seg_workers, stop_flag, lock, done_box)
                    time.sleep(0.5)
            else:
                with ThreadPoolExecutor(max_workers=w,
                                        thread_name_prefix="gep") as ex:
                    futs = [ex.submit(
                        _gui_one_ep, q, num, title, eps[num], t["quality"],
                        os.path.join(out_dir, "Серия %02d.mp4" % num),
                        seg_workers, stop_flag, lock, done_box)
                        for num in nums if not stop_flag.is_set()]
                    for f in as_completed(futs):
                        try:
                            f.result()
                        except Exception:
                            pass  # helper все ошибки уже положил в очередь
            if stop_flag.is_set():
                q.put(("stopped", "Остановлено. Уже скачанное сохранено."))
                return
        q.put(("finished", "Готово."))
    except Exception as e:
        q.put(("fatal", str(e)))


# ------------------------------------------------------------------- GUI

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("YummyDownloader")
        try:
            self.root.geometry("560x715")
            self.root.minsize(500, 560)
        except Exception:
            pass
        self.cfg = yummy_dl.load_config()
        self.q = queue.Queue()
        self.data = None          # результат проверки: dict из check_worker
        self.checked_url = ""
        self.state = "idle"
        self.stop_flag = threading.Event()
        self.worker = None
        self._installing = False  # идет ли фоновая установка зависимостей
        self.ep_total = 0
        self.ep_done = 0
        self.ep_widgets = {}      # (title, num) -> {"var", "bar", "head", "mode"}
        self.ep_info = {}         # (title, num) -> {"est": None|bytes, "final": None|bytes}
        self.main_title = None
        self.done_bytes = 0
        self._active_cur = 0
        self._active_key = None
        self.over_tracker = SpeedTracker(3.0)
        self._build_widgets()
        self.set_state("idle")
        self.refresh_deps_label()  # тихая проверка, без автоустановки
        self.root.after(100, self._poll)

    # -- layout --
    def _build_widgets(self):
        pad = {"padx": 10, "pady": 4}
        # 1. ссылка
        f1 = ttk.LabelFrame(self.root, text="1. Ссылка")
        f1.pack(fill="x", **pad)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(f1, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        self.check_btn = ttk.Button(f1, text="Проверить", command=self.on_check)
        self.check_btn.pack(side="left", padx=(0, 8), pady=8)
        self.url_hint = ttk.Label(self.root, text="Вставь ссылку на тайтл и нажми «Проверить».",
                                  foreground="gray")
        self.url_hint.pack(fill="x", padx=14, pady=(0, 2))
        # 2. настройки
        f2 = ttk.LabelFrame(self.root, text="2. Что качать")
        f2.pack(fill="x", **pad)
        self.title_var = tk.StringVar(value="Пока ничего не проверено")
        ttk.Label(f2, textvariable=self.title_var, font=("", 10, "bold")).pack(
            anchor="w", padx=8, pady=(6, 2))
        row = ttk.Frame(f2)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text="Озвучка:").pack(side="left")
        self.dub_var = tk.StringVar(value="—")
        self.dub_menu = ttk.OptionMenu(row, self.dub_var, "—")
        self.dub_menu.pack(side="left", fill="x", expand=True, padx=6)
        self.dub_var.trace_add("write", lambda *a: self.refresh_summary())
        row2 = ttk.Frame(f2)
        row2.pack(fill="x", padx=8, pady=2)
        ttk.Label(row2, text="Качество:").pack(side="left")
        self.q_var = tk.StringVar(value=self.cfg.get("quality") or "max")
        for val, label in (("max", "Макс"), ("480", "480"), ("360", "360")):
            ttk.Radiobutton(row2, text=label, value=val, variable=self.q_var,
                            command=self.refresh_summary).pack(side="left", padx=6)
        row3 = ttk.Frame(f2)
        row3.pack(fill="x", padx=8, pady=2)
        ttk.Label(row3, text="Серии:").pack(side="left")
        self.eps_var = tk.StringVar()
        self.eps_entry = ttk.Entry(row3, textvariable=self.eps_var, width=22)
        self.eps_entry.pack(side="left", padx=6)
        self.eps_var.trace_add("write", lambda *a: self.refresh_summary())
        self.eps_hint = ttk.Label(row3, text="пусто = все", foreground="gray")
        self.eps_hint.pack(side="left")
        row35 = ttk.Frame(f2)
        row35.pack(fill="x", padx=8, pady=2)
        ttk.Label(row35, text="Потоки серий:").pack(side="left")
        self.workers_var = tk.StringVar(
            value=str(self.cfg.get("workers", 4) or 4))
        self.workers_spin = ttk.Spinbox(row35, from_=1, to=8, width=3,
                                        textvariable=self.workers_var)
        self.workers_spin.pack(side="left", padx=6)
        ttk.Label(row35, text="1–8, сегменты качаются по 8",
                  foreground="gray").pack(side="left")
        self.fran_var = tk.BooleanVar(value=False)
        self.fran_check = ttk.Checkbutton(f2, text="Качать всю франшизу",
                                          variable=self.fran_var,
                                          command=self.refresh_summary)
        self.fran_check.pack(anchor="w", padx=8, pady=2)
        row4 = ttk.Frame(f2)
        row4.pack(fill="x", padx=8, pady=(2, 6))
        self.dir_var = tk.StringVar(value=self.cfg["output_dir"])
        ttk.Label(row4, text="Папка:").pack(side="left")
        ttk.Label(row4, textvariable=self.dir_var, foreground="gray").pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(row4, text="Открыть папку", command=self.on_open_folder).pack(side="left")
        # 3. прогресс
        f3 = ttk.LabelFrame(self.root, text="3. Прогресс")
        f3.pack(fill="both", expand=True, **pad)
        self.sum_var = tk.StringVar(value="Итог: —")
        ttk.Label(f3, textvariable=self.sum_var).pack(anchor="w", padx=8, pady=(6, 2))
        prow = ttk.Frame(f3)
        prow.pack(fill="x", padx=8, pady=2)
        self.bar = ttk.Progressbar(prow, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True)
        self.bar_label = ttk.Label(prow, text="0/0", width=9, anchor="e")
        self.bar_label.pack(side="left", padx=(6, 0))
        self.tot_var = tk.StringVar(value="")
        ttk.Label(f3, textvariable=self.tot_var, foreground="gray").pack(
            anchor="w", padx=8)
        lframe = ttk.Frame(f3)
        lframe.pack(fill="both", expand=True, padx=8, pady=4)
        self.ep_canvas = tk.Canvas(lframe, height=170, highlightthickness=0)
        self.ep_scroll = ttk.Scrollbar(lframe, command=self.ep_canvas.yview)
        self.ep_canvas.configure(yscrollcommand=self.ep_scroll.set)
        self.ep_canvas.pack(side="left", fill="both", expand=True)
        self.ep_scroll.pack(side="left", fill="y")
        self.ep_inner = ttk.Frame(self.ep_canvas)
        self._ep_win = self.ep_canvas.create_window((0, 0), window=self.ep_inner,
                                                    anchor="nw")
        self.ep_inner.bind("<Configure>",
                           lambda e: self.ep_canvas.configure(
                               scrollregion=self.ep_canvas.bbox("all")))
        self.ep_canvas.bind("<Configure>",
                            lambda e: self.ep_canvas.itemconfigure(
                                self._ep_win, width=e.width))
        # повтор серии из списка — двойной клик по строке с ошибкой
        self.ep_inner.bind("<Double-Button-1>", self.on_retry_one)
        self.ep_canvas.bind("<Enter>", self._wheel_on)
        self.ep_canvas.bind("<Leave>", self._wheel_off)
        # низ: статус + зависимости + кнопки
        self.status_var = tk.StringVar(value="Вставь ссылку и нажми «Проверить».")
        ttk.Label(self.root, textvariable=self.status_var, foreground="gray").pack(
            fill="x", padx=14, pady=2)
        # зависимости в 1 клик: кнопка + мелкий статус, работе не мешает
        drow = ttk.Frame(self.root)
        drow.pack(fill="x", padx=10, pady=(0, 2))
        self.deps_btn = ttk.Button(drow, text="Установить зависимости",
                                   command=self.on_install_deps)
        self.deps_btn.pack(side="left")
        self.deps_var = tk.StringVar(value="зависимости: проверка…")
        self.deps_label = ttk.Label(drow, textvariable=self.deps_var,
                                    foreground="gray", font=("", 8))
        self.deps_label.pack(side="left", padx=(8, 0))
        brow = ttk.Frame(self.root)
        brow.pack(fill="x", padx=10, pady=(2, 10))
        self.start_btn = ttk.Button(brow, text="Скачать", command=self.on_start)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.cancel_btn = ttk.Button(brow, text="Отмена", command=self.on_cancel)
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        # --- вставка в поля: контекстное меню + русские Ctrl + Shift+Insert ---
        self._entry_menu = tk.Menu(self.root, tearoff=0)
        self._entry_menu.add_command(label="Вырезать", command=self._menu_cut)
        self._entry_menu.add_command(label="Копировать", command=self._menu_copy)
        self._entry_menu.add_command(label="Вставить", command=self._menu_paste)
        self._menu_target = None
        self.url_var.trace_add("write", lambda *a: self._update_check_btn())
        for entry in (self.url_entry, self.eps_entry):
            entry.bind("<Control-KeyPress>", self._on_entry_ctrl)
            entry.bind("<Button-3>", self._show_entry_menu)
            entry.bind("<Shift-Insert>", self._on_shift_insert)
        try:
            self.url_entry.focus_set()
        except Exception:
            pass

    # -- states --
    def set_state(self, st, msg=""):
        self.state = st
        if st == "idle":
            self.check_btn.configure(state="normal")
            self.url_entry.configure(state="normal")
            self._settings_enabled(False)
            self.start_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
        elif st == "ready":
            self.check_btn.configure(state="normal")
            self.url_entry.configure(state="normal")
            self._settings_enabled(True)
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
        elif st == "checking":
            self.check_btn.configure(state="disabled")
            self._settings_enabled(False)
            self.start_btn.configure(state="disabled")
            self.cancel_btn.configure(state="disabled")
        elif st == "downloading":
            self.check_btn.configure(state="disabled")
            self.url_entry.configure(state="disabled")
            self._settings_enabled(False)
            self.start_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
        elif st in ("done", "error", "stopped"):
            self.check_btn.configure(state="normal")
            self.url_entry.configure(state="normal")
            self._settings_enabled(True)
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
        if msg:
            self.status_var.set(msg)
        if st not in ("checking", "downloading"):
            self._update_check_btn()

    def _update_check_btn(self):
        """Кнопка Проверить активна только при непустом поле ссылки."""
        if self.state in ("checking", "downloading"):
            return
        try:
            ok = bool(self.url_var.get().strip())
        except Exception:
            ok = False
        try:
            self.check_btn.configure(state="normal" if ok else "disabled")
        except Exception:
            pass

    # -- вставка в поля (Ctrl+V при RU-раскладке, Shift+Insert, меню по ПКМ) --
    def _on_entry_ctrl(self, event):
        action = ctrl_key_action(getattr(event, "keysym", ""),
                                 getattr(event, "keycode", None))
        if not action:
            return None  # латинская раскладка — штатная обработка Tk
        w = event.widget
        try:
            if action == "paste":
                w.event_generate("<<Paste>>")
            elif action == "copy":
                w.event_generate("<<Copy>>")
            elif action == "cut":
                w.event_generate("<<Cut>>")
            elif action == "select_all":
                w.selection("range", 0, "end")
        except Exception:
            pass
        return "break"

    def _on_shift_insert(self, event):
        try:
            event.widget.event_generate("<<Paste>>")
        except Exception:
            pass
        return "break"

    def _show_entry_menu(self, event):
        w = event.widget
        try:
            w.focus_set()
        except Exception:
            pass
        self._menu_target = w
        try:
            editable = str(w.cget("state")) == "normal"
        except Exception:
            editable = True
        try:
            self._entry_menu.entryconfigure(0, state="normal" if editable else "disabled")
            self._entry_menu.entryconfigure(2, state="normal" if editable else "disabled")
        except Exception:
            pass
        try:
            self._entry_menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        return "break"

    def _menu_cut(self):
        try:
            self._menu_target.event_generate("<<Cut>>")
        except Exception:
            pass

    def _menu_copy(self):
        try:
            self._menu_target.event_generate("<<Copy>>")
        except Exception:
            pass

    def _menu_paste(self):
        try:
            self._menu_target.event_generate("<<Paste>>")
        except Exception:
            pass

    def _settings_enabled(self, on):
        s = "normal" if on else "disabled"
        for w in (self.dub_menu, self.eps_entry, self.fran_check,
                  self.workers_spin):
            try:
                w.configure(state=s)
            except Exception:
                pass
        for w in self.root.pack_slaves():
            pass  # радиокнопки качества переключаем отдельно
        # радиокнопки: ищем по типу
        for w in self._all_children(self.root):
            if isinstance(w, ttk.Radiobutton):
                try:
                    w.configure(state=s)
                except Exception:
                    pass

    def _all_children(self, w):
        for c in w.winfo_children():
            yield c
            for cc in self._all_children(c):
                yield cc

    # -- actions --
    def on_open_folder(self):
        path = self.dir_var.get()
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            else:
                import webbrowser
                webbrowser.open(path)
        except Exception as e:
            messagebox.showerror("Папка", "Не смог открыть папку:\n%s" % e)

    def on_check(self):
        url = self.url_var.get().strip()
        ok, msg = validate_url(url)
        if not ok:
            self.url_hint.configure(text=msg, foreground="red")
            self.set_state("idle", msg)
            return
        self.url_hint.configure(text="Проверяю...", foreground="gray")
        self.set_state("checking", "Проверяю ссылку...")
        self.data = None
        t = threading.Thread(target=check_worker, args=(url, self.cfg, self.q), daemon=True)
        t.start()

    def on_start(self):
        if not self.data:
            self.set_state("error", "Сначала нажми «Проверить».")
            return
        if self.url_var.get().strip() != self.checked_url:
            self.set_state("error", "Ссылка изменилась — нажми «Проверить» заново.")
            return
        dub = self.dub_var.get()
        if dub not in self.data["groups"]:
            self.set_state("error", "Выбери озвучку из списка.")
            return
        ep_text = self.eps_var.get()
        ok, keep, msg = parse_episodes_text(ep_text, self.data["groups"][dub])
        if not ok:
            self.set_state("error", msg)
            return
        if self.data.get("is_tv"):
            enum = self.data["tv_enum"]
            tasks = [{
                "tv": True,
                "tv_enum": enum,
                "title_hint": enum.get("title", "?"),
                "dub": dub,
                "quality": self.q_var.get(),
                "keep": keep,
                "franchise_dir": None,
            }]
        else:
            tasks, msg = build_plan(self.data["page_id"], self.data["info"],
                                    self.data["groups"], dub, self.q_var.get(),
                                    ep_text, self.fran_var.get(), self.cfg)
            if not tasks:
                self.set_state("error", msg)
                return
        self._clear_rows()
        self.ep_rows = {}
        self.ep_total = 0
        self.ep_done = 0
        self.main_title = None
        self.done_bytes = 0
        self._active_cur = 0
        self._active_key = None
        self.over_tracker.reset()
        self.bar["value"] = 0
        self.bar_label.configure(text="0/…")
        self.tot_var.set("")
        self.stop_flag.clear()
        self.set_state("downloading", "Качаю...")
        try:
            workers = max(1, min(int(self.workers_var.get() or 4), 8))
        except Exception:
            workers = 4
        self.worker = threading.Thread(target=download_worker,
                                       args=(tasks, dict(self.cfg,
                                                          quality=self.q_var.get(),
                                                          workers=workers),
                                             self.q, self.stop_flag),
                                       daemon=True)
        self.worker.start()

    def on_cancel(self):
        self.stop_flag.set()
        self.status_var.set("Останавливаю после текущей серии...")
        self.cancel_btn.configure(state="disabled")

    def on_retry_one(self, _ev):
        # Подсказка: повтор одной упавшей серии — нажать Скачать заново,
        # готовые подхватятся как «уже есть». Отдельный точечный ретрай — фаза 2.
        self.status_var.set("Чтобы докачать упавшие серии, просто нажми «Скачать» еще раз.")

    # -- зависимости в 1 клик --
    def refresh_deps_label(self):
        """Тихая проверка при старте: только индикация, без установки."""
        try:
            ok, label = deps_status()
        except Exception:
            ok, label = False, "зависимости: ?"
        try:
            self.deps_var.set(label)
            self.deps_label.configure(foreground="green" if ok else "red")
        except Exception:
            pass

    def on_install_deps(self):
        if self._installing:
            return  # повторный клик во время установки игнорируем
        self._installing = True
        try:
            self.deps_btn.configure(state="disabled")
        except Exception:
            pass
        self.status_var.set("Установка зависимостей...")
        t = threading.Thread(target=install_deps_worker, args=(self.q,),
                             daemon=True)
        t.start()

    def refresh_summary(self):
        if not self.data or self.state in ("downloading", "checking"):
            return
        dub = self.dub_var.get()
        if dub not in self.data.get("groups", {}):
            return
        avail = self.data["groups"][dub]
        ok, keep, msg = parse_episodes_text(self.eps_var.get(), avail)
        if not ok:
            self.sum_var.set("Итог: " + msg)
            self.start_btn.configure(state="disabled")
            return
        self.start_btn.configure(state="normal" if self.state != "downloading" else "disabled")
        fr = ""
        if self.fran_var.get() and self.data.get("franchise_count"):
            fr = ", франшиза +%d" % self.data["franchise_count"]
        qlabel = {"max": "макс", "480": "480", "360": "360"}.get(self.q_var.get(),
                                                                 self.q_var.get())
        self.sum_var.set("Итог: %s, %s, серий %d%s" % (dub, qlabel, len(keep), fr))
        self.eps_hint.configure(
            text="Все: %d–%d, всего %d" % (avail[0], avail[-1], len(avail)))

    # -- queue pump --
    def _poll(self):
        try:
            while True:
                ev, payload = self.q.get_nowait()
                self._handle(ev, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _wheel_on(self, _ev):
        try:
            self.ep_canvas.bind_all("<MouseWheel>", self._wheel_scroll)
        except Exception:
            pass

    def _wheel_off(self, _ev):
        try:
            self.ep_canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass

    def _wheel_scroll(self, event):
        try:
            self.ep_canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass
        return "break"

    # -- строки серий (текст + свой прогресс-бар) --
    def _key(self, title, num):
        if self.main_title is not None and title == self.main_title:
            return ("main", num)
        return (title, num)

    def _clear_rows(self):
        try:
            for w in self.ep_inner.winfo_children():
                w.destroy()
        except Exception:
            pass
        self.ep_widgets = {}
        self.ep_info = {}

    def _add_row(self, key, head):
        try:
            frame = ttk.Frame(self.ep_inner)
            frame.pack(fill="x", pady=1)
            var = tk.StringVar(value=head + " — ждет")
            ttk.Label(frame, textvariable=var).pack(fill="x")
            bar = ttk.Progressbar(frame, mode="indeterminate")
            bar.pack(fill="x")
            self.ep_widgets[key] = {"var": var, "bar": bar,
                                    "head": head, "mode": "wait"}
            self.ep_info.setdefault(key, {"est": None, "final": None})
        except Exception:
            pass

    def _add_note(self, text):
        try:
            ttk.Label(self.ep_inner, text=text, foreground="gray").pack(fill="x")
        except Exception:
            pass

    def _set_row_text(self, key, text):
        w = self.ep_widgets.get(key)
        if not w:
            return
        try:
            w["var"].set(text)
        except Exception:
            pass

    def _set_row_bar(self, key, pct):
        """pct 0..100 -> determinate; None -> крутящийся indeterminate."""
        w = self.ep_widgets.get(key)
        if not w:
            return
        try:
            bar = w["bar"]
            if pct is None:
                if w["mode"] != "ind":
                    bar.configure(mode="indeterminate")
                    bar.start(120)
                    w["mode"] = "ind"
            else:
                if w["mode"] != "det":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=100)
                    w["mode"] = "det"
                bar["value"] = max(0, min(100, pct))
        except Exception:
            pass

    def _finish_row_bar(self, key):
        w = self.ep_widgets.get(key)
        if not w:
            return
        try:
            w["bar"].stop()
            w["bar"].configure(mode="determinate", maximum=100)
            w["bar"]["value"] = 100
            w["mode"] = "done"
        except Exception:
            pass

    def _active_text(self, key, pct, cur, total_est, speed):
        head = self.ep_widgets.get(key, {}).get("head", "Серия")
        spd = fmt_speed(speed) if speed and speed > 0 else ""
        if pct is not None and total_est:
            t = "%s — %d%% · %s" % (head, int(pct), fmt_pair(cur, total_est))
        elif total_est:
            t = "%s — %s" % (head, fmt_pair(cur, total_est))
        else:
            t = "%s — %s" % (head, fmt_mb(cur))
        if spd:
            t += " · " + spd
        return t

    def _handle(self, ev, p):
        if ev == "status":
            self.status_var.set(p)
        elif ev == "check_error":
            self.url_hint.configure(text="Ошибка: " + str(p)[:160], foreground="red")
            self.set_state("idle", "Не получилось проверить ссылку.")
        elif ev == "check_empty":
            self.data = {"page_id": p["page_id"], "info": p["info"],
                         "groups": {}, "franchise_count": 0}
            self.checked_url = self.url_var.get().strip()
            self.title_var.set(p["title"])
            self.url_hint.configure(
                text="В Kodik озвучек нет — этот тайтл не качается.", foreground="red")
            self.set_state("idle", "Озвучек в Kodik нет.")
        elif ev == "check_done":
            self.data = p
            self.checked_url = self.url_var.get().strip()
            self.title_var.set(p["title"])
            menu = self.dub_menu["menu"]
            menu.delete(0, "end")
            for n in p["names"]:
                label = "%s (%d сер.)" % (n, len(p["groups"][n]))
                menu.add_command(label=label,
                                 command=lambda v=n: self.dub_var.set(v))
            self.dub_var.set(p["default_dub"])
            if p["franchise_count"]:
                self.fran_check.configure(
                    state="normal",
                    text="Качать всю франшизу (+%d)" % p["franchise_count"])
            else:
                self.fran_var.set(False)
                self.fran_check.configure(state="disabled",
                                          text="Качать всю франшизу (других сезонов нет)")
            self.url_hint.configure(text="Найдено: %s" % p["title"], foreground="green")
            self.set_state("ready", "Готово к скачиванию.")
            self.refresh_summary()
        elif ev == "plan":
            nums = p["nums"]
            self._clear_rows()
            self.main_title = None
            for n in nums:
                self._add_row(("main", n), "Серия %02d" % n)
            self.ep_total = len(nums)
            self.ep_done = 0
            self.done_bytes = 0
            self._active_cur = 0
            self._active_key = None
            self.over_tracker.reset()
            self._update_bar()
        elif ev == "season":
            for n in p["nums"]:
                key = (p["title"], n)
                self._add_row(key, "%s · %02d" % (p["title"][:18], n))
            self.ep_total += len(p["nums"])
            self._update_bar()
        elif ev == "title_start":
            if self.main_title is None:
                self.main_title = p["title"]
            self.status_var.set("Качаю: %s (%s, серий %s)..." % (
                p["title"][:40], p["dub"], p["count"]))
        elif ev == "title_error":
            self._add_note("%s — пропуск: %s" % (p["hint"][:30], p["error"][:80]))
        elif ev == "ep_start":
            key = self._key(p["title"], p["num"])
            if key not in self.ep_widgets:
                head = ("Серия %02d" % p["num"] if key[0] == "main"
                        else "%s · %02d" % (p["title"][:18], p["num"]))
                self._add_row(key, head)
            self._set_row_text(key, self.ep_widgets[key]["head"] + " — качается…")
            self._set_row_bar(key, None)
            self._active_key = key
            self._active_cur = 0
            self.status_var.set("Качается серия %02d..." % p["num"])
        elif ev == "ep_prog":
            key = self._key(p["title"], p["num"])
            if key not in self.ep_widgets:
                return
            if p.get("total_est"):
                self.ep_info[key]["est"] = p["total_est"]
            self._set_row_text(key, self._active_text(
                key, p.get("pct"), p.get("cur", 0),
                p.get("total_est"), p.get("speed", 0)))
            self._set_row_bar(key, p.get("pct"))
            if self._active_key is None:
                self._active_key = key
            self._active_cur = p.get("cur", 0)
            try:
                self.over_tracker.add(time.time(), self.done_bytes + self._active_cur)
            except Exception:
                pass
            self._update_bar()
        elif ev == "ep_skip":
            key = self._key(p["title"], p["num"])
            size = p.get("size", 0)
            self.ep_info.setdefault(key, {"est": None, "final": None})["final"] = size
            self.done_bytes += size
            self._finish_row_bar(key)
            self._set_row_text(key, "%s — уже есть %s, пропуск" % (
                self.ep_widgets.get(key, {}).get("head", "Серия"), fmt_mb(size)))
        elif ev == "ep_done":
            key = self._key(p["title"], p["num"])
            size = p.get("size", p.get("size_mb", 0) * 1024 * 1024)
            self.ep_info.setdefault(key, {"est": None, "final": None})["final"] = size
            self.done_bytes += size
            self._active_cur = 0
            self._active_key = None
            self._finish_row_bar(key)
            self._set_row_text(key, "%s — готово %s (%sp)" % (
                self.ep_widgets.get(key, {}).get("head", "Серия"),
                fmt_mb(size), p["q"]))
        elif ev == "ep_error":
            key = self._key(p["title"], p["num"])
            dl = p.get("downloaded", 0)
            self._active_cur = 0
            self._active_key = None
            try:
                w = self.ep_widgets.get(key)
                if w:
                    w["bar"].stop()
            except Exception:
                pass
            self._set_row_text(key, "%s — ошибка: %s%s" % (
                self.ep_widgets.get(key, {}).get("head", "Серия"),
                str(p["error"])[:70],
                (" · скачано %s" % fmt_mb(dl)) if dl else ""))
            # очередь продолжается — так задумано
        elif ev == "overall":
            self.ep_done = p["done"]
            self._update_bar()
        elif ev == "finished":
            self.set_state("done", "Готово. Файлы в папке.")
            messagebox.showinfo("Готово", "Скачивание завершено.\nПапка: %s" % self.dir_var.get())
        elif ev == "stopped":
            self.set_state("stopped", str(p))
        elif ev == "fatal":
            self.set_state("error", "Ошибка: %s" % str(p)[:160])
        elif ev == "deps_done":
            self._installing = False
            try:
                self.deps_btn.configure(state="normal")
            except Exception:
                pass
            self.refresh_deps_label()
            if p.get("ok"):
                self.status_var.set("Зависимости установлены.")
            else:
                self.status_var.set("Не получилось поставить зависимости: %s"
                                    % str(p.get("msg") or "?")[:160])

    def _update_bar(self):
        total = max(self.ep_total, 1)
        self.bar["maximum"] = total
        self.bar["value"] = self.ep_done
        try:
            self.bar_label.configure(text="%d/%d" % (self.ep_done, self.ep_total))
        except Exception:
            pass
        # строка под баром: "5 из 12 · 620/2100 МБ · 4.1 МБ/с"
        try:
            cur = self.done_bytes + self._active_cur
            ests = [v.get("est") for v in self.ep_info.values()]
            known = [e for e in ests if e]
            parts = ["%d из %d" % (self.ep_done, self.ep_total)]
            if known:
                est_sum = sum(known)
                if len(known) < len(ests):
                    parts.append("%s/~%s" % (fmt_mb(cur), fmt_mb(est_sum)))
                else:
                    parts.append(fmt_pair(cur, est_sum))
            elif cur:
                parts.append("%s скачано" % fmt_mb(cur))
            spd = self.over_tracker.rate()
            if spd > 0:
                parts.append(fmt_speed(spd))
            self.tot_var.set(" · ".join(parts))
        except Exception:
            pass


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "windowingsystem")  # no-op, проверка что Tk живой
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
