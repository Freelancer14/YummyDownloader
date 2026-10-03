#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yummy_anime_bot.py — самодостаточный Telegram-бот: ищет аниме сразу по нескольким
источникам (YummyAnime/Kodik, AniLibria, Zona), качает серии и присылает видео
прямо в чат.

Один файл. Нужны только два пакета:
    pip install certifi imageio-ffmpeg

Telegram ходит через HTTP-прокси (v2RayTun, по умолчанию 127.0.0.1:10801).
Источники аниме качаются напрямую (без прокси).

Особенности:
  * параллельный поиск по всем источникам сразу;
  * очередь загрузок: одновременно качается не более WORKERS (по умолчанию 2),
    остальным бот пишет позицию в очереди;
  * контроль места на диске: если свободно меньше MIN_FREE_GB — бот отвечает
    "ошибка на сервере, слишком мало места" и не качает.

Запуск:  python3 yummy_anime_bot.py
"""

import os, sys, re, json, time, ssl, base64, shutil, subprocess, threading, queue, traceback, glob, random
import urllib.request, urllib.parse

import certifi
import imageio_ffmpeg

# =========================== НАСТРОЙКИ =====================================
# СЕКРЕТЫ (токен бота, API_HASH, id канала, телефон/2FA) НЕ храним в коде —
# они читаются ниже из bot_secrets.json (файл в .gitignore) или из env.
# Значения тут — пустые заглушки для публичного репозитория.
TOKEN = ""
PROXY = "http://127.0.0.1:10801"      # HTTP-прокси для Telegram; "" = без прокси
QUALITY = "max"                        # "max" = лучшее доступное; либо "480"/"720"/"360"
MAX_EPISODES_PER_REQUEST = 0           # 0 = без лимита серий за один запрос
WORKERS = 2                            # сколько РАЗНЫХ запросов качается параллельно
PIPELINE = 3                           # сколько серий ОДНОГО запроса готовим разом (доставка — по очереди)
MIN_FREE_GB = 15                       # меньше столько ГБ свободно -> ошибка сервера
ALLOWED_USERS = []                     # [] = отвечать всем; иначе список user_id
TMP_DIR = "bot_tmp"                    # временная папка для скачивания
# Локальный Telegram Bot API сервер (self-hosted) поднимает лимит файла с 50 МБ
# до 2 ГБ. Запускается отдельно (launcher выставляет env YUMMY_BOT_TG_API).
# Пусто = обычный api.telegram.org (лимит 50 МБ на файл, его задаёт сам Telegram).
TG_API_BASE = ""   # напр. "http://127.0.0.1:8744" если поднял локальный сервер

# --- ЮЗЕРБОТ для больших файлов (>50 МБ) ---------------------------------------
# Схема: серии больше лимита бота юзербот заливает в приватный канал-хранилище
# (аккаунт может слать файлы до ~2 ГБ), а бот пересылает их пользователю через
# copyMessage — повторной заливки нет, лимит 50 МБ не действует.
#
# Что подготовить ОДИН раз на сервере:
#   1. Создать приватный канал (телеграм -> новый канал).
#   2. Добавить туда АДМИНами: и этого бота (@dwnldanimebot), и свой аккаунт-юзербот.
#   3. Узнать id канала (перешли любое сообщение из канала боту @username_to_id_bot,
#      либо смотри в логах при первой заливке) и впиши ниже как STORAGE_CHAT (-100...).
#   4. Залогинить юзербот:  python3 userbot_login.py   (введёшь телефон + код).
USERBOT_ENABLED   = True
USERBOT_API_ID    = 0                             # задаётся в bot_secrets.json
USERBOT_API_HASH  = ""                            # задаётся в bot_secrets.json
# Уникальное имя сессии — чтобы НЕ пересеклось с другими .session на сервере.
USERBOT_SESSION   = "yummy_storage_userbot_8252748715"
USERBOT_PROXY     = "SOCKS5://127.0.0.1:10801"   # прокси для входа юзербота (тот же порт, SOCKS5 — надёжнее для Telethon)
USERBOT_PHONE     = ""           # ПУСТО — спросит ТВОЙ номер в консоли (код придёт в это же приложение Telegram)
USERBOT_2FA       = ""           # пароль 2FA (если включён); пусто = спросит в консоли
STORAGE_CHAT      = 0                 # id приватного канала-хранилища (в bot_secrets.json)
SIGNATURE         = "Скачано через @dwnldanimebot"   # приписка в конце подписи к видео
# ------------------------------------------------------------------------------
YUMMY_SITE = "https://old.yummyani.me"
ANILIBRIA_API = "https://api.anilibria.app"
ZONA_API = "https://w1.zona.plus"
# ===========================================================================

TOKEN = os.environ.get("YUMMY_BOT_TOKEN") or TOKEN
PROXY = os.environ.get("YUMMY_BOT_PROXY", PROXY)
TG_API_BASE = os.environ.get("YUMMY_BOT_TG_API", TG_API_BASE).rstrip("/")
USERBOT_PROXY = os.environ.get("YUMMY_USERBOT_PROXY", USERBOT_PROXY)
if os.environ.get("YUMMY_STORAGE_CHAT"):
    try:
        STORAGE_CHAT = int(os.environ["YUMMY_STORAGE_CHAT"])
    except ValueError:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# --- секреты из локального bot_secrets.json (в .gitignore, в репозиторий не попадает).
#     Скопируй bot_secrets.example.json -> bot_secrets.json и впиши свои значения.
_secrets_path = os.path.join(HERE, "bot_secrets.json")
try:
    with open(_secrets_path, encoding="utf-8") as _sf:
        _secrets = json.load(_sf)
    TOKEN            = _secrets.get("token") or TOKEN
    USERBOT_API_ID   = int(_secrets.get("userbot_api_id") or USERBOT_API_ID or 0)
    USERBOT_API_HASH = _secrets.get("userbot_api_hash") or USERBOT_API_HASH
    USERBOT_PHONE    = _secrets.get("userbot_phone") or USERBOT_PHONE
    USERBOT_2FA      = _secrets.get("userbot_2fa") or USERBOT_2FA
    if _secrets.get("storage_chat"):
        STORAGE_CHAT = int(_secrets["storage_chat"])
    if _secrets.get("proxy") is not None:
        PROXY = _secrets["proxy"]
    if _secrets.get("userbot_proxy") is not None:
        USERBOT_PROXY = _secrets["userbot_proxy"]
except FileNotFoundError:
    pass
except Exception as _e:
    print("[secrets] не смог прочитать bot_secrets.json:", _e)

if not os.path.isabs(TMP_DIR):
    TMP_DIR = os.path.join(HERE, TMP_DIR)

# консоль на русской Windows (cp866): не падаем на редких символах вывода
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

CTX = ssl.create_default_context(cafile=certifi.where())
# Проверка SSL-сертификата отключена (прокси без валидного цепочки сертификатов).
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/120.0"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# Лимит файла: 2 ГБ при локальном Bot API сервере, иначе 50 МБ (правило Telegram).
TG_LIMIT = (2000 if TG_API_BASE else 49) * 1024 * 1024
API = (TG_API_BASE or "https://api.telegram.org") + "/bot" + TOKEN

# ----------------------------------------------------------------- общие http-хелперы (без прокси)
def http_get(url, ref=None, extra=None, t=30):
    hd = {"User-Agent": UA}
    if ref:
        hd["Referer"] = ref
    if extra:
        hd.update(extra)
    r = urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=t, context=CTX)
    return r.read().decode("utf-8", "replace")

def http_post(url, data, ref):
    hd = {"User-Agent": UA, "Referer": ref, "Origin": "https://kodikplayer.com",
          "Content-Type": "application/x-www-form-urlencoded"}
    body = urllib.parse.urlencode(data).encode()
    r = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hd), timeout=30, context=CTX)
    return r.read().decode("utf-8", "replace")

def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|]', "", str(name)).strip().rstrip(".")
    return re.sub(r"\s+", " ", name) or "untitled"

# ================================================================= ИСТОЧНИК: YummyAnime (Kodik)
def yummy_api(path):
    raw = http_get(YUMMY_SITE + "/api" + path, ref=YUMMY_SITE + "/",
                   extra={"X-Application": "wawegr8j13it4rdw", "Vary": "json", "Lang": "ru",
                          "Accept": "application/json"})
    return json.loads(raw)["response"]

ROT_TABLES = [str.maketrans(
    {**{chr(c): chr((c - 65 + n) % 26 + 65) for c in range(65, 91)},
     **{chr(c): chr((c - 97 + n) % 26 + 97) for c in range(97, 123)}}) for n in range(26)]

def kodik_decode(src):
    for tbl in ROT_TABLES:
        t = src.translate(tbl)
        try:
            d = base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")
            if "m3u8" in d:
                return ("https:" + d) if d.startswith("//") else d
        except Exception:
            pass
    raise RuntimeError("не смог декодировать ссылку Kodik")

def kodik_m3u8(iframe_url, quality):
    if iframe_url.startswith("//"):
        iframe_url = "https:" + iframe_url
    html = http_get(iframe_url, ref=YUMMY_SITE + "/")
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
        raise RuntimeError("Kodik не отдал ссылки")
    avail = sorted((int(q) for q in links if q.isdigit()))
    want = int(quality) if str(quality).isdigit() else avail[-1]
    le = [q for q in avail if q <= want]
    pick = le[-1] if le else avail[-1]
    return kodik_decode(links[str(pick)][0]["src"]), pick, "https://kodikplayer.com/"

def yummy_groups(items):
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

def yummy_search(query, limit=6):
    data = yummy_api("/search?q=" + urllib.parse.quote(query))
    out = []
    for it in (data or [])[:limit]:
        aid = it.get("anime_id")
        if not aid:
            continue
        out.append({"src": "yummy", "id": aid, "title": it.get("title") or "?",
                    "year": it.get("year") or "", "type": ((it.get("type") or {}).get("shortname")) or ""})
    return out

def yummy_load(anime_id):
    """Возвращает (title, dubs) где dubs = {озвучка: {номер: iframe}}."""
    info = yummy_api(f"/anime/{anime_id}")
    items = yummy_api(f"/anime/{anime_id}/videos")
    return (info.get("title") or f"anime_{anime_id}"), yummy_groups(items)

def yummy_resolve_link(url):
    html = http_get(url, ref=YUMMY_SITE + "/")
    m = re.search(r'page_id"\s*content="(\d+)"', html)
    if not m:
        raise RuntimeError("не нашёл page_id на странице")
    return int(m.group(1))

# ================================================================= ИСТОЧНИК: AniLibria
def lib_search(query, limit=6):
    raw = http_get(ANILIBRIA_API + "/api/v1/app/search/releases?query=" + urllib.parse.quote(query),
                   ref="https://anilibria.top/")
    data = json.loads(raw)
    out = []
    for it in (data or [])[:limit]:
        nm = it.get("name") or {}
        t = nm.get("main") or nm.get("english") or "?"
        typ = ((it.get("type") or {}).get("description")) or ""
        out.append({"src": "lib", "id": it.get("id"), "title": t,
                    "year": it.get("year") or "", "type": typ})
    return out

def lib_load(release_id):
    raw = http_get(ANILIBRIA_API + f"/api/v1/anime/releases/{release_id}?include=episodes",
                   ref="https://anilibria.top/")
    rel = json.loads(raw)
    nm = rel.get("name") or {}
    title = nm.get("main") or nm.get("english") or f"release_{release_id}"
    eps = {}
    for e in (rel.get("episodes") or []):
        try:
            num = int(round(float(e.get("ordinal"))))
        except Exception:
            continue
        # на эпизод храним словарь качество->url, плюс реферер
        eps[num] = {"480": e.get("hls_480"), "720": e.get("hls_720"), "1080": e.get("hls_1080")}
    # AniLibria всегда одна озвучка
    return title, {"AniLibria": eps}

# ================================================================= ИСТОЧНИК: Zona
def zona_get(path):
    return json.loads(http_get(ZONA_API + path, ref=ZONA_API + "/",
                               extra={"Accept": "application/json"}))

def zona_search(query, limit=6):
    data = zona_get("/api/v1/search/" + urllib.parse.quote(query))
    out = []
    for it in (data.get("items") or [])[:limit]:
        t = it.get("name_rus") or it.get("name_original") or "?"
        out.append({"src": "zona", "id": it.get("mobi_link_id"),
                    "name_id": it.get("name_id"), "serial": bool(it.get("serial")),
                    "title": t, "year": it.get("year") or "",
                    "type": "Zona сериал" if it.get("serial") else "Zona"})
    return out

def zona_video(mobi_id):
    """mobi_link_id ролика -> словарь ссылок качеств."""
    j = zona_get(f"/api/v1/video/{mobi_id}")
    return {"hls": j.get("lqHlsUrl"), "mp4": j.get("url") or j.get("lqUrl"), "lq": j.get("lqUrl")}

def zona_load(item):
    """item (dict из поиска) -> (title, dubs). Фильм = 1 серия; сериал = все серии всех сезонов.
    Для сериала на серию храним mobi_link_id, видео-ссылку резолвим при скачивании."""
    title = item.get("title") or "Zona"
    if not item.get("serial"):
        return title, {"Zona": {1: {"mobi": item["id"]}}}
    # сериал: пройдём сезоны
    nid = item.get("name_id")
    info = zona_get("/api/v1/tvseries/" + nid)
    title = info.get("name_rus") or title
    seasons = int(info.get("seasons_count") or 1)
    eps = {}
    counter = 0
    for s in range(1, seasons + 1):
        try:
            sj = zona_get(f"/api/v1/tvseries/{nid}/season/{s}")
        except Exception:
            continue
        for e in ((sj.get("episodes") or {}).get("items") or []):
            counter += 1
            label = e.get("episode_key") or f"E{counter}"
            eps[counter] = {"mobi": e.get("mobi_link_id"), "key": label}
    if not eps:
        # на всякий случай — как фильм
        return title, {"Zona": {1: {"mobi": item["id"]}}}
    return title, {"Zona": eps}

# ================================================================= ИСТОЧНИК: ссылки (TikTok / YouTube) через yt-dlp
# Прямые ссылки качаем yt-dlp'ом, обязательно через прокси (PROXY) — без поиска.
def link_kind(url):
    """Определяет источник по ссылке: 'youtube' / 'tiktok' / None."""
    try:
        host = urllib.parse.urlparse(url.strip()).netloc.lower()
    except Exception:
        return None
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    return None

LINK_NAME = {"youtube": "YouTube", "tiktok": "TikTok"}

def _ytdlp_base():
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
           "--socket-timeout", "30"]
    if PROXY:
        cmd += ["--proxy", PROXY]      # TikTok/YouTube ходят только через прокси
    return cmd

def ytdlp_title(url):
    try:
        r = subprocess.run(_ytdlp_base() + ["--simulate", "--print", "%(title)s", url],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=120)
        lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""

def download_link(url, kind, tag):
    """Качает прямую ссылку (TikTok/YouTube) в mp4 через yt-dlp. Возвращает путь."""
    out = os.path.join(TMP_DIR, f"link_{tag}.%(ext)s")
    # ВАЖНО: для YouTube берём именно H.264 (avc1) + AAC (mp4a). VP9/AV1, которые
    # YouTube отдаёт по умолчанию для обычных видео, Telegram не декодирует —
    # получается «первый кадр на всё видео + звук». avc1 играется везде.
    fmt = ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
           "bv*[vcodec^=avc1][height<=1080]+ba/"
           "b[vcodec^=avc1][height<=1080]/b[ext=mp4][height<=1080]/"
           "bv*[height<=1080]+ba/b[height<=1080]/best" if kind == "youtube"
           else "b/bv*+ba/best")
    cmd = _ytdlp_base() + [
        "--ffmpeg-location", FFMPEG, "-f", fmt,
        "--merge-output-format", "mp4",
        # faststart: moov-атом в начало файла — иначе стриминг в TG может лагать
        "--postprocessor-args", "Merger+ffmpeg:-movflags +faststart",
        "--no-part", "-o", out, "--no-simulate", "--print", "after_move:filepath", url]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=3600)
    path = ""
    for ln in reversed((r.stdout or "").splitlines()):
        ln = ln.strip()
        if ln and os.path.exists(ln):
            path = ln
            break
    if not path:                       # запасной вариант — ищем по префиксу
        cands = [c for c in glob.glob(os.path.join(TMP_DIR, f"link_{tag}.*"))
                 if not c.endswith((".part", ".ytdl"))]
        cands.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
        path = cands[0] if cands else ""
    if not path or not os.path.exists(path) or os.path.getsize(path) < 10000:
        err = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
        raise RuntimeError("yt-dlp: " + (err[-1] if err else "не удалось скачать"))
    return path

# ================================================================= скачивание -> mp4
def disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return 999.0

def download_hls(m3u8, out_path, ref, attempts=3):
    tmp = out_path + ".part.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-referer", ref, "-user_agent", UA,
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
            time.sleep(2 * attempt)
    raise RuntimeError("ffmpeg не справился: " + last_err)

def download_mp4(url, out_path, ref):
    tmp = out_path + ".part.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-referer", ref, "-user_agent", UA,
           "-i", url, "-c", "copy", tmp]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 10000:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError("ffmpeg не справился (mp4)")
    os.replace(tmp, out_path)

def video_meta(path):
    """Ширина, высота и длительность (сек) видео — чтобы Telegram не сплющивал в квадрат.
    Берём из вывода ffmpeg (ffprobe отдельно не нужен). При неудаче -> (0,0,0)."""
    w = h = 0
    dur = 0
    try:
        r = subprocess.run([FFMPEG, "-i", path, "-hide_banner"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        out = r.stderr or ""
        m = re.search(r"(\d{2,5})x(\d{2,5})", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", out)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    except Exception:
        pass
    return w, h, dur

def fetch_episode(item, ep_obj, want_quality, tag):
    """Качает одну серию из любого источника в максимальном (или заданном) качестве.
    Возвращает (path, quality_label). Размер не ограничиваем."""
    src = item["src"]
    out = os.path.join(TMP_DIR, f"{tag}_{int(time.time()*1000)}.mp4")
    if src == "yummy":
        m3u8, real_q, ref = kodik_m3u8(ep_obj, want_quality)
        download_hls(m3u8, out, ref)
        return out, f"{real_q}p"
    if src == "lib":
        # AniLibria: берём максимум (1080 -> 720 -> 480) или конкретное качество
        if str(want_quality).isdigit():
            url = ep_obj.get(str(want_quality))
        else:
            url = ep_obj.get("1080") or ep_obj.get("720") or ep_obj.get("480")
        qlabel = next((q for q in ("1080", "720", "480") if ep_obj.get(q) == url), "?")
        if not url:
            url = ep_obj.get("720") or ep_obj.get("480")
        if not url:
            raise RuntimeError("AniLibria: нет ссылки на серию")
        download_hls(url, out, "https://anilibria.top/")
        return out, f"{qlabel}p"
    if src == "zona":
        links = ep_obj
        if "mobi" in ep_obj and not ep_obj.get("hls"):
            links = zona_video(ep_obj["mobi"])
        url = links.get("mp4") or links.get("hls") or links.get("lq")  # mp4 = лучшее качество
        if not url:
            raise RuntimeError("Zona: нет ссылки на серию")
        if "m3u8" in url:
            download_hls(url, out, ZONA_API + "/")
        else:
            download_mp4(url, out, ZONA_API + "/")
        return out, "Zona"
    raise RuntimeError("неизвестный источник")

# ----------------------------------------------------------------- Telegram (через прокси)
def _opener():
    handlers = [urllib.request.HTTPSHandler(context=CTX)]
    if PROXY:
        handlers.insert(0, urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    return urllib.request.build_opener(*handlers)

OPENER = _opener()
_tg_lock = threading.Lock()   # сериализуем обращения к Telegram API из потоков

def tg(method, params=None, timeout=60):
    data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(API + "/" + method, data=data)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def tg_send_video(chat_id, path, caption=""):
    boundary = "----yummybot" + str(os.path.getsize(path))
    w, h, dur = video_meta(path)   # размеры — чтобы видео не было квадратным
    fields = {"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true"}
    if w and h:
        fields["width"] = str(w)
        fields["height"] = str(h)
    if dur:
        fields["duration"] = str(dur)
    pre = b""
    for k, v in fields.items():
        pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode("utf-8")
    fname = os.path.basename(path)
    pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; "
            f"filename=\"{fname}\"\r\nContent-Type: video/mp4\r\n\r\n").encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("utf-8")
    with open(path, "rb") as f:
        body = pre + f.read() + post
    req = urllib.request.Request(API + "/sendVideo", data=body)
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with OPENER.open(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def send(chat_id, text, **kw):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    p.update(kw)
    try:
        with _tg_lock:
            return tg("sendMessage", p)
    except Exception as e:
        print("send err:", e)

def send_video(chat_id, path, caption):
    with _tg_lock:
        return tg_send_video(chat_id, path, caption)

def edit(chat_id, message_id, text):
    """Отредактировать текст ранее отправленного сообщения (для живого прогресса)."""
    if not message_id:
        return
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": "true"}
    try:
        with _tg_lock:
            return tg("editMessageText", p)
    except Exception as e:
        print("edit err:", e)

def edit_full(chat_id, message_id, text, reply_markup=None):
    """Как edit(), но можно заодно поменять инлайн-клавиатуру (для экрана настроек)."""
    if not message_id:
        return
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if reply_markup:
        p["reply_markup"] = reply_markup
    try:
        with _tg_lock:
            return tg("editMessageText", p)
    except Exception as e:
        print("edit_full err:", e)

def delete_msg(chat_id, message_id):
    """Удалить ранее отправленное сообщение (чистим лишние статусы из чата)."""
    if not message_id:
        return
    try:
        with _tg_lock:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass

def _fmt_eta(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} сек"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} мин {s:02d} сек"
    h, m = divmod(m, 60)
    return f"{h} ч {m:02d} мин"

def _bar(frac, width=12):
    frac = min(1.0, max(0.0, frac))
    full = int(frac * width)
    return "█" * full + "░" * (width - full)

def tg_copy_message(chat_id, from_chat_id, message_id, caption=None):
    """Переслать (скопировать) уже загруженное в канал сообщение пользователю.
    Файл уже в облаке Telegram — никакой повторной заливки и лимита 50 МБ."""
    p = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    if caption is not None:
        p["caption"] = caption
    with _tg_lock:
        return tg("copyMessage", p)

def kb(rows):
    return json.dumps({"inline_keyboard": [
        [{"text": t, "callback_data": d} for (t, d) in row] for row in rows]})

def kb_url(rows):
    """Инлайн-клавиатура из URL-кнопок (rows = [[(текст, url), ...], ...]).
    URL-кнопки переживают пересылку сообщения другому человеку."""
    return json.dumps({"inline_keyboard": [
        [{"text": t, "url": u} for (t, u) in row] for row in rows]})

# ----------------------------------------------------------------- ЮЗЕРБОТ (большие файлы)
# Telethon живёт в отдельном потоке со своим event loop. Заливку вызываем из
# воркеров синхронно через run_coroutine_threadsafe.
# При первом запуске (нет файла сессии) бот прямо в консоли спросит телефон и код.
_UB = {"client": None, "loop": None, "ready": False, "err": None}
_UB_DONE = threading.Event()   # выставится, когда логин/проверка завершатся

def _userbot_proxy():
    if not USERBOT_PROXY:
        return None
    import socks
    u = urllib.parse.urlparse(USERBOT_PROXY)
    ptype = socks.SOCKS5 if u.scheme.startswith("socks") else socks.HTTP
    # 3 элемента — как в рабочем tg_userbot2.py (без rdns).
    return (ptype, u.hostname, u.port)

def userbot_start():
    """Поднять юзербот в фоне. Если сессии нет — интерактивный вход (телефон+код).
    Схема входа повторяет рабочий ai.py: ConnectionTcpAbridged + ручной send_code/sign_in."""
    if not USERBOT_ENABLED:
        _UB_DONE.set()
        return
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except Exception as e:
        print("[userbot] нет telethon (pip install telethon cryptg python-socks) — большие файлы недоступны:", e)
        _UB_DONE.set()
        return

    def run():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _UB["loop"] = loop
        # Минимальная схема — как в рабочем tg_userbot2.py: без ConnectionTcpAbridged.
        client = TelegramClient(
            os.path.join(HERE, USERBOT_SESSION), USERBOT_API_ID, USERBOT_API_HASH,
            proxy=_userbot_proxy())
        # не-премиум ловит короткие флуд-вейты при заливке — пусть Telethon
        # сам пережидает их (до 120 c) вместо падения серии
        client.flood_sleep_threshold = 120

        async def boot():
            await client.connect()
            if not await client.is_user_authorized():
                print("\n" + "=" * 60)
                print("ПЕРВЫЙ ЗАПУСК: вход аккаунта для отправки больших файлов.")
                print("Код придёт ВНУТРИ Telegram (сообщение от 'Telegram'), не по СМС.")
                print("=" * 60)
                phone = USERBOT_PHONE or input("📱 Номер телефона (+7...): ").strip()
                await client.send_code_request(phone)
                code = input("📟 Код из Telegram: ").strip()
                try:
                    await client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    pwd = USERBOT_2FA or input("🔐 Пароль 2FA: ").strip()
                    await client.sign_in(password=pwd)
            me = await client.get_me()
            _UB["client"] = client
            _UB["ready"] = True
            print(f"[userbot] вход выполнен: {me.first_name} "
                  f"(@{getattr(me,'username',None) or me.id})")
            # Диагностика скорости: без cryptg шифрование идёт на чистом Python
            # и душит заливку до ~0.1 МБ/с при высокой нагрузке на CPU.
            try:
                import cryptg  # noqa
                print("[userbot] ✅ cryptg активен — шифрование на C (быстро).")
            except Exception:
                print("[userbot] ⚠️  cryptg НЕ установлен! Заливка будет МЕДЛЕННОЙ "
                      "(~0.1 МБ/с) и грузить CPU. Поставь:  pip install cryptg")
            print(f"[userbot] параллельных соединений на заливку: {UPLOAD_CONNECTIONS}")
        try:
            loop.run_until_complete(boot())
        except Exception as e:
            _UB["err"] = str(e)
            print("[userbot] ошибка входа:", e)
        finally:
            _UB_DONE.set()
        if _UB["ready"]:
            loop.run_forever()
    threading.Thread(target=run, daemon=True).start()

def userbot_ready():
    return USERBOT_ENABLED and _UB["ready"] and STORAGE_CHAT

# Кол-во параллельных соединений для заливки. Через прокси с высокой латентностью
# каждое доп. соединение даёт почти линейный прирост (проверено: 1=18, 4=52, 8=103 Mbit/s).
# 1 = обычная последовательная заливка.
UPLOAD_CONNECTIONS = 8

async def _make_extra_sender(client):
    """Доп. соединение к ТЕКУЩЕМУ DC с тем же auth_key (приём FastTelethon).
    _borrow_exported_sender тут не годится — Telegram отдаёт DcIdInvalidError
    при экспорте авторизации на уже подключённый DC."""
    from telethon.network import MTProtoSender
    dc = await client._get_dc(client.session.dc_id)
    sender = MTProtoSender(client.session.auth_key, loggers=client._log)
    conn = client._connection(dc.ip_address, dc.port, dc.id,
                              loggers=client._log, proxy=client._proxy)
    await sender.connect(conn)
    return sender

async def _fast_upload(client, path, part_cb=None):
    """Параллельная выгрузка файла несколькими соединениями к одному DC.
    Возвращает InputFile/InputFileBig, который потом уходит в send_file.
    Проверено реальной заливкой 60 МБ с проверкой целостности в канале."""
    import asyncio, hashlib
    from telethon import helpers
    from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
    from telethon.tl.types import InputFile, InputFileBig
    from telethon.errors import FloodWaitError

    async def send_part(sender, req):
        """Отправить кусок с учётом флуд-вейта. Сырой sender.send() не проходит
        через авто-паузу Telethon, поэтому FloodWaitError ловим сами и ждём —
        иначе не-премиум аккаунт валит заливку 'A wait of N seconds is required'."""
        while True:
            try:
                await sender.send(req)
                return
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)

    size = os.path.getsize(path)
    part_size = 512 * 1024
    part_count = (size + part_size - 1) // part_size
    is_big = size > 10 * 1024 * 1024
    file_id = helpers.generate_random_long()
    name = os.path.basename(path)
    conns = max(1, UPLOAD_CONNECTIONS) if is_big else 1
    conns = min(conns, part_count)

    sent = [0]
    lock = asyncio.Lock()

    # Маленькие файлы (<10 МБ) или 1 соединение — обычный последовательный путь.
    if conns == 1:
        md5 = hashlib.md5()
        sender = client._sender
        with open(path, "rb") as f:
            for idx in range(part_count):
                part = f.read(part_size)
                if not is_big:
                    md5.update(part)
                    req = SaveFilePartRequest(file_id, idx, part)
                else:
                    req = SaveBigFilePartRequest(file_id, idx, part_count, part)
                await send_part(sender, req)
                sent[0] += len(part)
                if part_cb:
                    part_cb(sent[0], size)
        if is_big:
            return InputFileBig(file_id, part_count, name)
        return InputFile(file_id, part_count, name, md5.hexdigest())

    # Большой файл: основное соединение + (conns-1) дополнительных, части по кругу.
    extra = []
    for _ in range(conns - 1):
        try:
            extra.append(await _make_extra_sender(client))
        except Exception as e:
            print("[userbot] доп. соединение не открылось:", e)
    senders = [client._sender] + extra
    n = len(senders)

    async def worker(slot, sender):
        with open(path, "rb") as f:
            idx = slot
            while idx < part_count:
                f.seek(idx * part_size)
                part = f.read(part_size)
                await send_part(sender, SaveBigFilePartRequest(file_id, idx, part_count, part))
                async with lock:
                    sent[0] += len(part)
                    if part_cb:
                        part_cb(sent[0], size)
                idx += n

    try:
        await asyncio.gather(*(worker(i, s) for i, s in enumerate(senders)))
    finally:
        for s in extra:
            try:
                await s.disconnect()
            except Exception:
                pass
    return InputFileBig(file_id, part_count, name)

def userbot_upload_to_storage(path, caption, progress=None):
    """Залить файл в канал-хранилище от имени юзербота. Возвращает message_id.
    progress(sent, total) — необязательный колбэк прогресса (зовётся из event-loop потока)."""
    import asyncio
    client = _UB["client"]; loop = _UB["loop"]

    # реальные размеры — иначе Telegram рисует превью квадратом
    w, h, dur = video_meta(path)
    async def up():
        # Сначала параллельно заливаем тело файла, получаем handle...
        handle = await _fast_upload(client, path, part_cb=progress)
        # ...затем привязываем его к сообщению в канале как стримящееся видео.
        from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
        attrs = [DocumentAttributeVideo(dur, w, h, supports_streaming=True),
                 DocumentAttributeFilename(os.path.basename(path))]
        msg = await client.send_file(
            STORAGE_CHAT, handle, caption=caption[:1024],
            attributes=attrs, supports_streaming=True, mime_type="video/mp4")
        return msg.id
    fut = asyncio.run_coroutine_threadsafe(up(), loop)
    return fut.result(timeout=7200)

def userbot_delete_from_storage(message_id):
    """Удалить сообщение с видео из канала-хранилища (после пересылки пользователю)."""
    import asyncio
    client = _UB["client"]; loop = _UB["loop"]
    if not client or not message_id:
        return
    async def rm():
        await client.delete_messages(STORAGE_CHAT, [message_id])
    try:
        asyncio.run_coroutine_threadsafe(rm(), loop).result(timeout=60)
    except Exception as e:
        print("[userbot] не удалил из хранилища:", e)

# ----------------------------------------------------------------- состояние и поиск
STATE = {}          # chat_id -> {"results":[...], "anime":{...}, "title", "dubs", "dub"}
EP_RE = re.compile(r"^[\d\s,\-]+$")

# Zona отключена: её API отдаёт только защищённую заглушку (~0.5 МБ, «скачайте
# android.zona»), реальный поток закрыт авторизацией их приложения. Чтобы вернуть —
# добавь обратно ("Zona", zona_search) и разберись с токеном приложения.
SOURCES = [("YummyAnime", yummy_search), ("AniLibria", lib_search)]

# ----------------------------------------------------------------- настройки пользователя + коды-шеринга
# Две новые подсистемы, обе с диском (переживают перезапуск бота):
#   * USER_SETTINGS — личные настройки видео (качество, скрывать ли саб-озвучки);
#   * CODES — короткие числовые коды, в которых «заморожены» аниме+озвучка+серии+
#     качество, чтобы делиться выбором с друзьями одним числом.
QUALITY_CHOICES = ["max", "1080", "720", "480", "360"]
SETTINGS_FILE = os.path.join(HERE, "bot_user_settings.json")
CODES_FILE    = os.path.join(HERE, "share_codes.json")
CODE_LEN = 8                               # длина генерируемого кода (8 цифр)

BOT_USERNAME = "dwnldanimebot"  # дефолт; в main() перезапишется реальным из getMe — для deep-link t.me/<bot>?start=КОД
_store_lock = threading.Lock()
USER_SETTINGS = {}   # str(chat_id) -> {"quality": "...", "skip_subs": bool}
CODES = {}           # str(code)    -> {"entries":[entry,...], "by": uid, "title": str}
                     # entry = {"item": {...}, "title", "dub", "eps", "quality"}

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)              # атомарная замена — файл не побьётся при сбое
    except Exception as e:
        print("save err", path, e)

def load_store():
    global USER_SETTINGS, CODES
    USER_SETTINGS = _load_json(SETTINGS_FILE, {}) or {}
    CODES = _load_json(CODES_FILE, {}) or {}

def save_settings():
    with _store_lock:
        _save_json(SETTINGS_FILE, USER_SETTINGS)

def save_codes():
    with _store_lock:
        _save_json(CODES_FILE, CODES)

def user_settings(chat_id):
    """Настройки пользователя (создаёт дефолтные при первом обращении)."""
    s = USER_SETTINGS.get(str(chat_id))
    if not s:
        s = {"quality": QUALITY, "skip_subs": False}
        USER_SETTINGS[str(chat_id)] = s
    s.setdefault("quality", QUALITY)
    s.setdefault("skip_subs", False)
    return s

def get_quality(chat_id):
    return user_settings(chat_id).get("quality") or QUALITY

def settings_view(chat_id):
    """Текст + инлайн-клавиатура экрана настроек."""
    s = user_settings(chat_id); q = s["quality"]
    qrow = [(("• " if ch == q else "") + ("Макс" if ch == "max" else ch + "p"), f"q:{ch}")
            for ch in QUALITY_CHOICES]
    rows = [qrow[:3], qrow[3:],
            [(("☑ " if s["skip_subs"] else "☐ ") + "Скрывать саб-озвучки", "sub:t")]]
    txt = ("⚙️ <b>Настройки видео</b>\n\n"
           f"🎚 Качество: <b>{'максимум' if q == 'max' else q + 'p'}</b> — бот берёт ближайшее не выше выбранного.\n"
           f"💬 Саб-озвучки: <b>{'скрыты' if s['skip_subs'] else 'видны'}</b>.\n\n"
           "Настройки запоминаются и записываются в коды, которыми ты делишься.")
    return txt, kb(rows)

def show_settings(chat_id, message_id=None):
    txt, k = settings_view(chat_id)
    if message_id:
        edit_full(chat_id, message_id, txt, k)   # перерисовываем тот же экран
    else:
        send(chat_id, txt, reply_markup=k)

# --- генерация и разбор кодов ---
def gen_code():
    with _store_lock:
        for _ in range(80):
            c = str(random.randint(10 ** (CODE_LEN - 1), 10 ** CODE_LEN - 1))
            if c not in CODES:
                return c
    return str(random.randint(10 ** (CODE_LEN - 1), 10 ** CODE_LEN - 1))

def find_code(text):
    """Извлекает код из сообщения: '/start 123', '/start(123)', '/code 123',
    'код 123' или просто голое число, если такой код существует."""
    t = (text or "").strip()
    m = re.match(r"^/(?:start|code)[\s(]*([0-9]{4,})\)?\s*$", t, re.I)
    if m:
        return m.group(1)
    if t in CODES:
        return t
    digits = re.sub(r"\D", "", t)
    if digits and digits in CODES and len(digits) >= 6:
        return digits
    return None

def code_label(entry):
    return f"{entry.get('title','?')} · {entry.get('dub','?')} · сер. {entry.get('eps','все')}"

def deep_link(code):
    return f"https://t.me/{BOT_USERNAME}?start={code}" if BOT_USERNAME else None

def _entry_from_state(chat_id, eps=None):
    st = STATE.get(chat_id)
    if not st or not st.get("dub"):
        return None
    return {"item": dict(st.get("anime") or {}), "title": st.get("title") or "?",
            "dub": st.get("dub"), "eps": eps or st.get("last_eps") or "все",
            "quality": get_quality(chat_id)}

def share_code_message(chat_id, code, entries):
    """Шлёт пересылаемое сообщение с кнопкой-переходом в бота (deep-link).
    entries — список выбранных тайтлов (один или несколько)."""
    if isinstance(entries, dict):          # обратная совместимость
        entries = [entries]
    if len(entries) == 1:
        e = entries[0]; q = e["quality"]
        body = (f"🎬 <b>{e['title']}</b>\n"
                f"Озвучка: <b>{e['dub']}</b> · серии: <b>{e['eps']}</b> · "
                f"качество: <b>{'макс' if q == 'max' else q}</b>\n\n"
                f"👇 Нажми кнопку — откроется в боте и можно качать.\n"
                f"<i>Это сообщение можно переслать другу.</i>\n\n"
                f"Код: <code>{code}</code>")
    else:
        lines = [f"📦 <b>Подборка из {len(entries)} аниме</b>\n"]
        for i, e in enumerate(entries, 1):
            lines.append(f"<b>{i}.</b> {code_label(e)}")
        body = ("\n".join(lines) +
                f"\n\n👇 Нажми кнопку — откроется в боте, можно качать по одному или всё сразу.\n"
                f"<i>Это сообщение можно переслать другу.</i>\n\n"
                f"Код: <code>{code}</code>")
    url = deep_link(code)
    if url:
        send(chat_id, body, reply_markup=kb_url([[("▶️ Открыть в боте", url)]]))
    else:   # на всякий — без username кнопку не сделать
        send(chat_id, body + f"\n\nИли пришли боту: <code>/start {code}</code>")

def _share_done_kb():
    return kb([[("✅ Готово — получить ссылку", "sharedone")]])

def add_share_entry(chat_id, uid, eps=None):
    """Режим /share: добавить текущий выбор (аниме+озвучка+серии+качество) в
    накопительную подборку. Несколько аниме собираются в один код по очереди."""
    entry = _entry_from_state(chat_id, eps)
    if not entry:
        send(chat_id, "Сначала выбери аниме и озвучку.")
        return
    st = STATE.setdefault(chat_id, {})
    entries = st.setdefault("share_entries", [])
    entries.append(entry)
    lst = "\n".join(f"<b>{i}.</b> {code_label(e)}" for i, e in enumerate(entries, 1))
    send(chat_id,
         f"➕ Добавил «{entry['title']}» (серии {entry['eps']}).\n\n"
         f"<b>В подборке {len(entries)} тайтл(ов):</b>\n{lst}\n\n"
         f"Выбери ещё аниме (просто напиши название) — или заверши кнопкой ниже "
         f"(либо пришли /share ещё раз).",
         reply_markup=_share_done_kb())

def finish_share(chat_id, uid):
    """Завершить режим /share: собрать все накопленные тайтлы в один код и
    прислать пересылаемое сообщение с кнопкой."""
    st = STATE.setdefault(chat_id, {})
    entries = list(st.get("share_entries") or [])
    st.pop("share_mode", None)
    st.pop("share_entries", None)
    if not entries:
        send(chat_id, "Подборка пустая — код не создан. Включи /share, выбери аниме и серии.")
        return
    code = gen_code()
    title = entries[0]["title"] + (f" +{len(entries) - 1}" if len(entries) > 1 else "")
    CODES[code] = {"entries": entries, "by": uid, "title": title}
    save_codes()
    share_code_message(chat_id, code, entries)
    if len(entries) > 1:
        send(chat_id, f"📦 Собрал <b>{len(entries)}</b> тайтл(ов) в код <code>{code}</code>.")

def show_mycodes(chat_id, uid):
    """Список кодов, созданных этим пользователем, с кнопками открыть/удалить."""
    mine = [(c, rec) for c, rec in CODES.items() if rec.get("by") == uid]
    if not mine:
        send(chat_id, "У тебя пока нет кодов. Выбери аниме и жми /share, чтобы создать.")
        return
    mine.sort(key=lambda cr: cr[0])
    lines = [f"🔖 <b>Твои коды</b> ({len(mine)}):\n"]
    rows = []
    for code, rec in mine:
        ents = rec.get("entries") or []
        if len(ents) == 1:
            descr = code_label(ents[0])
        else:
            descr = f"📦 комбо · {len(ents)} тайтл.: " + ", ".join(e.get("title", "?") for e in ents[:3])
            if len(ents) > 3:
                descr += "…"
        lines.append(f"<code>{code}</code> — {descr}")
        rows.append([(f"🔁 {code}", f"open:{code}"), ("🗑", f"del:{code}")])
    send(chat_id, "\n".join(lines) + "\n\n🔁 — открыть · 🗑 — удалить.", reply_markup=kb(rows))

def delete_code(chat_id, uid, code):
    rec = CODES.get(code)
    if not rec:
        send(chat_id, "Такого кода уже нет.")
        return
    if rec.get("by") != uid:
        send(chat_id, "Этот код создал не ты — удалить нельзя.")
        return
    CODES.pop(code, None)
    save_codes()
    send(chat_id, f"🗑 Код <code>{code}</code> удалён.")

def open_code(chat_id, code):
    """Открыть код: один тайтл — сразу к выбору серий; комбо — список с кнопками."""
    rec = CODES.get(code)
    if not rec:
        send(chat_id, "Не нашёл такой код. Проверь цифры.")
        return
    entries = rec.get("entries") or []
    if not entries:
        send(chat_id, "Код пустой.")
        return
    if len(entries) == 1:
        restore_entry(chat_id, entries[0], code=code)
        return
    lines = [f"📦 <b>Комбо</b> <code>{code}</code> — {len(entries)} тайтл(ов):\n"]
    btns = []
    for i, e in enumerate(entries, 1):
        lines.append(f"<b>{i}.</b> {code_label(e)}")
        btns.append((str(i), f"co:{code}:{i-1}"))
    rows = [btns[j:j + 5] for j in range(0, len(btns), 5)]
    rows.append([("⬇️ Скачать весь комбо", f"coall:{code}")])
    send(chat_id, "\n".join(lines) + "\n\nНажми номер, чтобы открыть тайтл, или скачай всё сразу.",
         reply_markup=kb(rows))

def restore_entry(chat_id, entry, code=None):
    """Развернуть один сохранённый выбор: подгрузить тайтл, выставить озвучку и
    предложить скачать как в коде (или ввести свои серии)."""
    item = dict(entry.get("item") or {})
    item.setdefault("src", "yummy")
    hint = entry.get("title") or item.get("title") or "?"
    send(chat_id, f"Открываю «{hint}» из кода…")
    try:
        title, dubs = load_item(item)
    except Exception as e:
        send(chat_id, f"Не получилось открыть: {e}")
        return
    dubs = {d: eps for d, eps in dubs.items() if eps}
    if not dubs:
        send(chat_id, "У этого тайтла сейчас нет доступных серий.")
        return
    want = entry.get("dub")
    dub = want if want in dubs else max(dubs, key=lambda n: len(dubs[n]))
    eps_text = entry.get("eps") or "все"
    q = entry.get("quality") or get_quality(chat_id)
    st = STATE.setdefault(chat_id, {})
    st["anime"] = item; st["title"] = title; st["dubs"] = dubs; st["dub"] = dub
    st["last_eps"] = eps_text; st["code_quality"] = q
    avail = sorted(dubs[dub])
    info = (f"🎬 <b>{title}</b>\nОзвучка: <b>{dub}</b>\n"
            f"Серий доступно: {len(avail)}\n"
            f"В коде: серии <b>{eps_text}</b> · качество <b>{'макс' if q=='max' else q}</b>\n\n"
            f"Напиши номер/диапазон серий — или качай как в коде:")
    send(chat_id, info, reply_markup=kb([[(f"⬇️ Скачать ({eps_text})", "dlcode")]]))

def combo_download_all(chat_id, code):
    """Поставить в очередь все тайтлы комбо. Загрузка тайтлов сетевая, поэтому
    разворачиваем их в отдельном потоке, чтобы не блокировать опрос апдейтов."""
    rec = CODES.get(code)
    if not rec:
        send(chat_id, "Не нашёл такой код.")
        return
    entries = rec.get("entries") or []

    def run():
        for e in entries:
            try:
                item = dict(e.get("item") or {})
                item.setdefault("src", "yummy")
                title, dubs = load_item(item)
                dubs = {d: x for d, x in dubs.items() if x}
                if not dubs:
                    send(chat_id, f"«{e.get('title','?')}»: нет доступных серий.")
                    continue
                want = e.get("dub")
                dub = want if want in dubs else max(dubs, key=lambda n: len(dubs[n]))
                start_download(chat_id, item, title, dub, dubs[dub],
                               e.get("eps") or "все", e.get("quality") or get_quality(chat_id))
            except Exception as ex:
                send(chat_id, f"«{e.get('title','?')}»: не вышло — {ex}")
    threading.Thread(target=run, daemon=True).start()
    send(chat_id, f"📦 Ставлю в очередь весь комбо <code>{code}</code> ({len(entries)} тайтл.)…")

def allowed(uid):
    return (not ALLOWED_USERS) or (uid in ALLOWED_USERS)

def multi_search(query):
    """Параллельный поиск по всем источникам. Возвращает плоский список result-dict."""
    results = {}
    threads = []
    def run(name, fn):
        try:
            results[name] = fn(query)
        except Exception as e:
            results[name] = []
            print(f"[search] {name} fail: {e}")
    for name, fn in SOURCES:
        th = threading.Thread(target=run, args=(name, fn), daemon=True)
        th.start(); threads.append(th)
    for th in threads:
        th.join(timeout=20)
    # склеиваем, сохраняя порядок источников
    flat = []
    for name, _ in SOURCES:
        flat.extend(results.get(name, []))
    return flat

def load_item(item):
    """item -> (title, dubs). dubs = {озвучка: {номер: ep_obj}}."""
    if item["src"] == "yummy":
        return yummy_load(item["id"])
    if item["src"] == "lib":
        title, dubs = lib_load(item["id"])
        # у release-эндпоинта нет имени — берём из результата поиска
        if item.get("title") and item["title"] != "?":
            title = item["title"]
        return title, dubs
    if item["src"] == "zona":
        return zona_load(item)
    raise RuntimeError("неизвестный источник")

def parse_ep_range(text, available):
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

SRC_BADGE = {"yummy": "YummyAnime", "lib": "AniLibria", "zona": "Zona"}

# ----------------------------------------------------------------- сценарии
def do_search(chat_id, query):
    send(chat_id, f"Ищу «{query}» по всем источникам…")
    res = multi_search(query)
    if not res:
        send(chat_id, "Ничего не нашёл. Попробуй другое название.")
        return
    res = res[:20]
    STATE.setdefault(chat_id, {})["results"] = res
    lines = ["🔎 <b>Нашёл:</b>\n"]
    btns = []
    for i, it in enumerate(res, 1):
        extra = " · ".join(x for x in [str(it["year"]) if it["year"] else "", it["type"]] if x)
        badge = SRC_BADGE.get(it["src"], it["src"])
        lines.append(f"<b>{i}.</b> {it['title']}" + (f"  <i>({extra})</i>" if extra else "") + f"  — {badge}")
        btns.append((str(i), f"i:{i-1}"))
    rows = [btns[j:j + 5] for j in range(0, len(btns), 5)]
    send(chat_id, "\n".join(lines) + "\n\nНажми номер нужного:", reply_markup=kb(rows))

def show_dubbings(chat_id, item):
    send(chat_id, "Загружаю…")
    try:
        title, dubs = load_item(item)
    except Exception as e:
        send(chat_id, f"Не получилось загрузить: {e}")
        return
    dubs = {d: eps for d, eps in dubs.items() if eps}
    if not dubs:
        send(chat_id, "У этого тайтла нет доступных для скачивания серий.")
        return
    if user_settings(chat_id).get("skip_subs"):    # настройка: прятать [саб]-озвучки
        no_subs = {d: e for d, e in dubs.items() if not d.startswith("[саб]")}
        if no_subs:
            dubs = no_subs
    st = STATE.setdefault(chat_id, {})
    st.pop("code_quality", None)            # обычный выбор — без качества из чужого кода
    st["anime"] = item; st["title"] = title; st["dubs"] = dubs; st["dub"] = None
    names = sorted(dubs, key=lambda n: -len(dubs[n]))
    if len(names) == 1:
        # одна озвучка — сразу к выбору серий
        pick_dub(chat_id, 0)
        return
    lines = [f"<b>{title}</b>\n\n🎙 <b>Озвучки:</b>\n"]
    btns = []
    for i, n in enumerate(names):
        lines.append(f"<b>{i+1}.</b> {n}  <i>({len(dubs[n])} сер.)</i>")
        btns.append((str(i+1), f"d:{i}"))
    rows = [btns[j:j + 5] for j in range(0, len(btns), 5)]
    send(chat_id, "\n".join(lines) + "\n\nНажми номер озвучки:", reply_markup=kb(rows))

def pick_dub(chat_id, idx):
    st = STATE.get(chat_id)
    if not st or "dubs" not in st:
        send(chat_id, "Сначала выбери аниме (просто напиши название).")
        return
    names = sorted(st["dubs"], key=lambda n: -len(st["dubs"][n]))
    if not (0 <= idx < len(names)):
        return
    st["dub"] = names[idx]
    eps = sorted(st["dubs"][names[idx]])
    sharing = st.get("share_mode")
    act = "добавить в подборку" if sharing else "скачать"
    tail = " — добавлю в подборку (потом «Готово» → ссылка для друга)." if sharing else "."
    if len(eps) == 1:
        send(chat_id, f"<b>{st['title']}</b>\nОзвучка: <b>{names[idx]}</b>\n\n"
                      f"Напиши <code>1</code>, чтобы {act}.")
        return
    rng = f"{eps[0]}–{eps[-1]}"
    limit_txt = (f"\nЗа раз — до {MAX_EPISODES_PER_REQUEST} серий."
                 if MAX_EPISODES_PER_REQUEST else "")
    send(chat_id,
         f"<b>{st['title']}</b>\nОзвучка: <b>{names[idx]}</b>\n"
         f"Доступно серий: {len(eps)} ({rng}).\n\n"
         f"Напиши номер серии (<code>1</code>), диапазон (<code>1-3</code>) "
         f"или <code>все</code>{tail}" + limit_txt)

# ----------------------------------------------------------------- очередь загрузок
JOBS = queue.Queue()
_qlock = threading.Lock()
_queued = 0   # сколько задач ждёт/в работе (для показа позиции)

def start_download(chat_id, item, title, dub, eps_map, ep_text, quality):
    """Поставить в очередь скачивание конкретного выбора. Используется как при
    ручном вводе серий, так и при открытии кодов/комбо."""
    if disk_free_gb(TMP_DIR) < MIN_FREE_GB:
        send(chat_id, "⚠️ Ошибка на сервере: слишком мало места. Попробуй позже.")
        return
    avail = sorted(eps_map)
    keep = sorted(parse_ep_range(ep_text, avail))
    if MAX_EPISODES_PER_REQUEST and len(keep) > MAX_EPISODES_PER_REQUEST:
        send(chat_id, f"За раз максимум {MAX_EPISODES_PER_REQUEST} серий — беру первые {MAX_EPISODES_PER_REQUEST}.")
        keep = keep[:MAX_EPISODES_PER_REQUEST]
    if not keep:
        send(chat_id, "Не понял номера серий. Пример: 1  или  1-3")
        return

    job = {"chat_id": chat_id, "item": dict(item), "title": title, "dub": dub,
           "eps_map": dict(eps_map), "keep": keep, "quality": quality}
    with _qlock:
        global_pos = JOBS.qsize()   # сколько уже ждёт перед этой
    JOBS.put(job)
    if global_pos == 0 and _queued < WORKERS:
        send(chat_id, f"«{title}» — серии {', '.join(map(str, keep))} — начинаю загрузку…")
    else:
        send(chat_id, f"🕓 «{title}» в очереди (перед тобой {global_pos + max(0,_queued-WORKERS+1)} задач(и)). "
                      f"Начну, как освободится загрузчик.")

def enqueue_download(chat_id, ep_text):
    st = STATE.get(chat_id)
    if not st or not st.get("dub"):
        send(chat_id, "Сначала выбери аниме и озвучку.")
        return
    st["last_eps"] = ep_text                       # запомним для /share
    quality = st.get("code_quality") or get_quality(chat_id)
    start_download(chat_id, st["anime"], st["title"], st["dub"], st["dubs"][st["dub"]],
                   ep_text, quality)

def enqueue_link(chat_id, url, kind):
    """Ставит в общую очередь задачу скачивания прямой ссылки (TikTok/YouTube)."""
    if disk_free_gb(TMP_DIR) < MIN_FREE_GB:
        send(chat_id, "⚠️ Ошибка на сервере: слишком мало места. Попробуй позже.")
        return
    job = {"chat_id": chat_id, "link": url, "kind": kind}
    with _qlock:
        global_pos = JOBS.qsize()
    JOBS.put(job)
    if global_pos == 0 and _queued < WORKERS:
        send(chat_id, f"Ссылка {LINK_NAME.get(kind, kind)} — начинаю загрузку…")
    else:
        send(chat_id, f"🕓 Ты в очереди (перед тобой {global_pos + max(0,_queued-WORKERS+1)} задач(и)).")

def worker(wid):
    global _queued
    while True:
        job = JOBS.get()
        with _qlock:
            _queued += 1
        chat_id = job["chat_id"]
        try:
            process_job(job)
        except Exception:
            traceback.print_exc()
            try:
                send(chat_id, "Серия: внутренняя ошибка загрузчика.")
            except Exception:
                pass
        finally:
            with _qlock:
                _queued -= 1
            JOBS.task_done()

def _upload_storage_progress(chat_id, msg_id, path, cap, num, size_mb):
    """Залить крупный файл в канал-хранилище через юзербот, редактируя
    переданное (временное) сообщение живым прогрессом с ETA. Возвращает id
    сообщения в хранилище. Само сообщение НЕ удаляет — это делает доставка."""
    st = {"sent": 0, "total": int(size_mb * 1024 * 1024) or 1,
          "t0": time.time(), "done": False}

    def on_progress(sent, total):
        st["sent"] = sent
        if total:
            st["total"] = total

    head = f"Серия {num}" if num not in (None, "") else "Видео"

    def ticker():
        while not st["done"]:
            time.sleep(4)
            if st["done"]:
                break
            sent, total = st["sent"], st["total"]
            if sent <= 0:
                continue
            frac = sent / total
            elapsed = max(0.1, time.time() - st["t0"])
            speed = sent / elapsed                       # байт/сек
            eta = (total - sent) / speed if speed > 0 else 0
            mb = 1024 * 1024
            edit(chat_id, msg_id,
                 f"📤 {head} • {_bar(frac)} {frac*100:.0f}%\n"
                 f"{sent/mb:.0f}/{total/mb:.0f} МБ • {speed/mb:.1f} МБ/с • осталось ~{_fmt_eta(eta)}")

    th = threading.Thread(target=ticker, daemon=True)
    th.start()
    try:
        return userbot_upload_to_storage(path, cap, progress=on_progress)
    finally:
        st["done"] = True

def prepare_episode(job, num, badge):
    """Тяжёлая часть: скачать серию и (если крупная) залить в хранилище.
    Выполняется параллельно для нескольких серий. Возвращает всё нужное для
    последующей доставки СТРОГО по очереди."""
    chat_id = job["chat_id"]; item = job["item"]; title = job["title"]
    dub = job["dub"]; eps_map = job["eps_map"]
    r = {"num": num, "ok": False, "kind": None, "msg_id": None,
         "path": None, "mid": None, "cap": "", "err": None}
    m = send(chat_id, f"⏳ Серия {num} — качаю…")
    r["msg_id"] = (m or {}).get("result", {}).get("message_id")
    try:
        if disk_free_gb(TMP_DIR) < MIN_FREE_GB:
            r["err"] = "на сервере мало места"; return r
        tag = f"{abs(hash((chat_id,title,num)))%10**8}"
        path, qlabel = fetch_episode(item, eps_map[num], job.get("quality") or QUALITY, tag)
        r["path"] = path
        size_mb = os.path.getsize(path) / 1024 / 1024
        cap = f"{title} — серия {num} • {dub} • {qlabel} • {badge}"
        if SIGNATURE:
            cap += f"\n\n{SIGNATURE}"
        r["cap"] = cap
        if os.path.getsize(path) > TG_LIMIT:
            if not userbot_ready():
                lim_mb = TG_LIMIT // 1024 // 1024
                r["err"] = f"файл {size_mb:.0f} МБ больше лимита ({lim_mb} МБ), юзербот не настроен"
                return r
            r["mid"] = _upload_storage_progress(chat_id, r["msg_id"], path, cap, num, size_mb)
            r["kind"] = "large"
        else:
            r["kind"] = "small"
        r["ok"] = True
    except Exception as e:
        r["err"] = str(e)
    return r

def deliver_episode(chat_id, r):
    """Лёгкая часть: отдать готовую серию пользователю. Вызывается строго по
    порядку (1 → 2 → 3 …). Временное статус-сообщение в конце удаляем."""
    num = r["num"]
    try:
        if not r["ok"]:
            edit(chat_id, r["msg_id"], f"Серия {num}: ошибка — {r['err']}")
            return
        if r["kind"] == "large":
            res = tg_copy_message(chat_id, STORAGE_CHAT, r["mid"], caption=r["cap"])
            userbot_delete_from_storage(r["mid"])   # чистим исходник из хранилища
            if not res.get("ok"):
                edit(chat_id, r["msg_id"], f"Серия {num}: не удалось переслать: {res.get('description')}")
                return
        else:
            res = send_video(chat_id, r["path"], r["cap"])
            if not res.get("ok"):
                edit(chat_id, r["msg_id"], f"Серия {num}: Telegram отклонил файл: {res.get('description')}")
                return
        delete_msg(chat_id, r["msg_id"])   # успех -> убираем статус, остаётся только видео
    finally:
        if r.get("path") and os.path.exists(r["path"]):
            os.remove(r["path"])           # сразу удаляем файл с диска

def deliver_file(chat_id, path, cap, status_msg_id):
    """Отдать готовый файл пользователю: крупный — через хранилище юзербота,
    мелкий — напрямую. Статус-сообщение удаляем на успехе / редактируем на ошибке."""
    try:
        size = os.path.getsize(path)
        if size > TG_LIMIT:
            if not userbot_ready():
                lim_mb = TG_LIMIT // 1024 // 1024
                edit(chat_id, status_msg_id,
                     f"Файл {size/1048576:.0f} МБ больше лимита ({lim_mb} МБ), юзербот не настроен")
                return
            mid = _upload_storage_progress(chat_id, status_msg_id, path, cap, "", size / 1048576)
            res = tg_copy_message(chat_id, STORAGE_CHAT, mid, caption=cap)
            userbot_delete_from_storage(mid)
            if not res.get("ok"):
                edit(chat_id, status_msg_id, f"Не удалось переслать: {res.get('description')}")
                return
        else:
            res = send_video(chat_id, path, cap)
            if not res.get("ok"):
                edit(chat_id, status_msg_id, f"Telegram отклонил файл: {res.get('description')}")
                return
        delete_msg(chat_id, status_msg_id)
    finally:
        if path and os.path.exists(path):
            os.remove(path)

def process_link_job(job):
    """Скачать прямую ссылку (TikTok/YouTube) через yt-dlp и прислать видео."""
    chat_id = job["chat_id"]; url = job["link"]; kind = job["kind"]
    m = send(chat_id, f"⏳ Качаю с {LINK_NAME.get(kind, kind)}…")
    sid = (m or {}).get("result", {}).get("message_id")
    try:
        if disk_free_gb(TMP_DIR) < MIN_FREE_GB:
            edit(chat_id, sid, "⚠️ На сервере мало места, попробуй позже.")
            return
        tag = f"{abs(hash((chat_id, url, time.time())))%10**8}"
        title = ytdlp_title(url) or LINK_NAME.get(kind, "видео")
        path = download_link(url, kind, tag)
        cap = f"{title} • {LINK_NAME.get(kind, kind)}"
        if SIGNATURE:
            cap += f"\n\n{SIGNATURE}"
        deliver_file(chat_id, path, cap, sid)
    except Exception as e:
        edit(chat_id, sid, f"Не получилось скачать: {e}")

def process_job(job):
    if job.get("link"):
        process_link_job(job)
        return
    chat_id = job["chat_id"]; item = job["item"]; keep = job["keep"]
    badge = SRC_BADGE.get(item["src"], item["src"])

    # Готовим до PIPELINE серий разом, но доставляем строго по очереди (1,2,3,4).
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(PIPELINE, len(keep)))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(prepare_episode, job, num, badge) for num in keep]
        for fut in futures:                # порядок submit == порядок keep == порядок доставки
            try:
                r = fut.result()
            except Exception as e:
                send(chat_id, f"Серия: внутренняя ошибка — {e}")
                continue
            deliver_episode(chat_id, r)
    finally:
        pool.shutdown(wait=True)
    send(chat_id, "Готово ✅")

# ----------------------------------------------------------------- апдейты
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = (msg.get("from") or {}).get("id")
    if not allowed(uid):
        send(chat_id, "Доступ запрещён.")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    low = text.lower()

    # --- коды-шеринг: /start CODE, /code CODE, '/start(CODE)' или просто число-код ---
    code = find_code(text)
    if code:
        open_code(chat_id, code)
        return

    if low in ("/start", "/help", "/menu"):
        send(chat_id,
             "Привет! Я ищу аниме сразу по нескольким источникам "
             "(YummyAnime, AniLibria) и присылаю серии прямо сюда.\n\n"
             "• Напиши <b>название</b> — поищу везде.\n"
             "• Пришли <b>ссылку</b> на тайтл old.yummyani.me.\n"
             "• Пришли <b>ссылку TikTok или YouTube</b> — скачаю видео.\n"
             "• Выбери результат, озвучку и номер серии — пришлю видео.\n\n"
             "🎛 <b>/settings</b> — качество и другие настройки видео.\n"
             "🔗 <b>/share</b> — режим подборки: выбирай аниме <b>по очереди</b> (озвучка + "
             "серии у каждого), а когда закончишь — жми «Готово», и бот пришлёт одно "
             "<b>сообщение с кнопкой</b> «Открыть в боте» на всю подборку. Его можно "
             "переслать другу.\n"
             "🔖 <b>/mycodes</b> — твои коды (открыть/удалить).\n"
             "Получил код? Пришли его сюда (или <code>/start КОД</code>) — открою.")
        return
    if low in ("/settings", "/настройки"):
        show_settings(chat_id)
        return
    if low.startswith("/share"):
        st = STATE.setdefault(chat_id, {})
        if st.get("share_mode"):                       # повторный /share — завершаем подборку
            finish_share(chat_id, uid)
            return
        st["share_mode"] = True
        st["share_entries"] = []
        send(chat_id, "🔗 <b>Режим подборки включён.</b> Выбирай аниме <b>по очереди</b>: "
                      "найди тайтл (напиши название), выбери озвучку и серии — я добавлю его "
                      "в подборку. Так можно набрать сколько угодно аниме.\n\n"
                      "Когда закончишь — нажми «Готово» под последним добавленным или пришли "
                      "/share ещё раз: соберу всё в один код-ссылку для друга.")
        return
    if low in ("/mycodes", "/codes", "/мои"):
        show_mycodes(chat_id, uid)
        return
    if low.startswith("/code"):
        send(chat_id, "Пришли код цифрами: <code>/code 12345678</code> или просто число.")
        return
    if text.startswith("http"):
        kind = link_kind(text)
        if kind:                       # прямая ссылка TikTok/YouTube — качаем yt-dlp'ом
            enqueue_link(chat_id, text, kind)
            return
        send(chat_id, "Разбираю ссылку…")
        try:
            aid = yummy_resolve_link(text)
        except Exception as e:
            send(chat_id, f"Не смог разобрать ссылку: {e}")
            return
        show_dubbings(chat_id, {"src": "yummy", "id": aid, "title": "?", "year": "", "type": ""})
        return
    st = STATE.get(chat_id)
    if st and st.get("dub") and EP_RE.match(text):
        if st.get("share_mode"):                       # режим /share — копим в подборку
            add_share_entry(chat_id, uid, eps=text)
        else:
            enqueue_download(chat_id, text)
        return
    do_search(chat_id, text)

def handle_callback(cq):
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    uid = (cq.get("from") or {}).get("id")
    try:
        with _tg_lock:
            tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
    except Exception:
        pass
    if chat_id is None or not allowed(uid):
        return
    if data.startswith("i:"):
        st = STATE.get(chat_id) or {}
        res = st.get("results") or []
        idx = int(data[2:])
        if 0 <= idx < len(res):
            show_dubbings(chat_id, res[idx])
    elif data.startswith("d:"):
        pick_dub(chat_id, int(data[2:]))
    elif data.startswith("q:"):                    # выбор качества в настройках
        q = data[2:]
        if q in QUALITY_CHOICES:
            user_settings(chat_id)["quality"] = q
            save_settings()
            show_settings(chat_id, message_id=mid)
    elif data == "sub:t":                          # тумблер саб-озвучек
        s = user_settings(chat_id)
        s["skip_subs"] = not s.get("skip_subs")
        save_settings()
        show_settings(chat_id, message_id=mid)
    elif data == "sharedone":                       # завершить подборку /share
        finish_share(chat_id, uid)
    elif data == "dlcode":                          # «скачать как в коде»
        st = STATE.get(chat_id) or {}
        if st.get("dub"):
            start_download(chat_id, st["anime"], st["title"], st["dub"], st["dubs"][st["dub"]],
                           st.get("last_eps") or "все", st.get("code_quality") or get_quality(chat_id))
        else:
            send(chat_id, "Сначала открой код заново.")
    elif data.startswith("co:"):                    # открыть один тайтл из комбо
        _, code, idx = data.split(":", 2)
        rec = CODES.get(code)
        if rec and idx.isdigit() and int(idx) < len(rec.get("entries") or []):
            restore_entry(chat_id, rec["entries"][int(idx)], code=code)
    elif data.startswith("coall:"):                 # скачать весь комбо
        combo_download_all(chat_id, data.split(":", 1)[1])
    elif data.startswith("open:"):                  # открыть код из /mycodes
        open_code(chat_id, data.split(":", 1)[1])
    elif data.startswith("del:"):                   # удалить свой код из /mycodes
        delete_code(chat_id, uid, data.split(":", 1)[1])

def main():
    if not TOKEN:
        print("Нет токена. Укажи TOKEN или env YUMMY_BOT_TOKEN."); sys.exit(1)
    os.makedirs(TMP_DIR, exist_ok=True)
    load_store()
    print(f"Настроек пользователей: {len(USER_SETTINGS)} | сохранённых кодов: {len(CODES)}")
    try:
        me = tg("getMe", timeout=30)
        if not me.get("ok"):
            print("getMe не ок:", me); sys.exit(1)
        global BOT_USERNAME
        BOT_USERNAME = me["result"].get("username") or BOT_USERNAME   # для deep-link кнопок
        print("Бот запущен: @" + me["result"].get("username", "?"),
              "| прокси:", PROXY or "нет", "| воркеров:", WORKERS,
              "| свободно ГБ:", round(disk_free_gb(TMP_DIR), 1))
    except Exception as e:
        print("Не подключиться к Telegram (прокси?):", e); sys.exit(1)

    if USERBOT_ENABLED and STORAGE_CHAT:
        userbot_start()
        # ждём завершения входа (при первом запуске тут будет ввод телефона/кода),
        # чтобы интерактивный ввод не смешивался с работой бота
        _UB_DONE.wait()
        if _UB["ready"]:
            print("Юзербот для больших файлов: включён | хранилище:", STORAGE_CHAT)
        else:
            print("Юзербот не вошёл — большие файлы (>50 МБ) пока недоступны.",
                  "Бот работает в обычном режиме.")
    elif USERBOT_ENABLED:
        print("Юзербот включён, но STORAGE_CHAT не задан — большие файлы недоступны.")

    for i in range(WORKERS):
        threading.Thread(target=worker, args=(i,), daemon=True).start()

    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            upd = tg("getUpdates", params, timeout=45)
        except Exception as e:
            print("getUpdates err:", e); time.sleep(3); continue
        if not upd.get("ok"):
            time.sleep(2); continue
        for u in upd["result"]:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception:
                traceback.print_exc()

if __name__ == "__main__":
    main()
