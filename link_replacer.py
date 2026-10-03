#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
link_replacer.py — ЮЗЕРБОТ на ОТДЕЛЬНОЙ сессии (вторая сессия твоего аккаунта).

Что делает: как только ТЫ сам (с этого аккаунта) отправляешь в ЛЮБОЙ чат
ссылку на YouTube или TikTok — он удаляет твоё сообщение и присылает в этот же
чат скачанное по ссылке видео.

Это не бот @dwnldanimebot, а отдельный вход в твой аккаунт (своя .session, чтобы
не конфликтовать с юзерботом-хранилищем основного бота). Аккаунт тот же — просто
второе устройство/сессия в списке сессий Telegram.

Нужно (уже стоит в этом окружении):  pip install telethon cryptg python-socks yt-dlp imageio-ffmpeg

Запуск:  py -3.14 link_replacer.py
Первый запуск спросит номер телефона и код (код придёт ВНУТРИ Telegram).
"""

import os, sys, re, json, time, glob, asyncio, subprocess, urllib.parse

import imageio_ffmpeg

# =========================== НАСТРОЙКИ =====================================
# СЕКРЕТЫ (API_ID/API_HASH) читаются из bot_secrets.json (в .gitignore) —
# пустые заглушки тут для публичного репозитория.
API_ID   = 0
API_HASH = ""
# Своё имя сессии — НЕ пересекается с сессией хранилища основного бота.
SESSION  = "yummy_link_replacer"
PROXY    = "http://127.0.0.1:10801"          # HTTP-прокси для yt-dlp (TikTok/YouTube)
TG_PROXY = "socks5://127.0.0.1:10801"        # прокси для входа Telethon (тот же порт)
PHONE    = ""                                 # пусто — спросит в консоли
TWO_FA   = ""                                 # пароль 2FA, если включён; пусто — спросит
TMP_DIR  = "link_tmp"                          # временная папка
STATUS_TXT = "⏳ Скачиваю видео по ссылке…"     # текст, на который меняем сообщение, пока качаем
DELETE_ON_FAIL = False                         # True = удалять сообщение даже если скачать не вышло
# ===========================================================================

HERE = os.path.dirname(os.path.abspath(__file__))

# --- секреты из bot_secrets.json (в .gitignore): API_ID/API_HASH, при желании 2FA/телефон
try:
    with open(os.path.join(HERE, "bot_secrets.json"), encoding="utf-8") as _sf:
        _secrets = json.load(_sf)
    API_ID   = int(_secrets.get("userbot_api_id") or API_ID or 0)
    API_HASH = _secrets.get("userbot_api_hash") or API_HASH
    TWO_FA   = _secrets.get("userbot_2fa") or TWO_FA
    if _secrets.get("userbot_proxy") is not None:
        TG_PROXY = _secrets["userbot_proxy"]
except FileNotFoundError:
    pass
except Exception as _e:
    print("[secrets] не смог прочитать bot_secrets.json:", _e)

if not os.path.isabs(TMP_DIR):
    TMP_DIR = os.path.join(HERE, TMP_DIR)
os.makedirs(TMP_DIR, exist_ok=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
URL_RE = re.compile(r"https?://[^\s]+", re.I)
LINK_NAME = {"youtube": "YouTube", "tiktok": "TikTok"}

# ----------------------------------------------------------------- определение ссылки
def link_kind(url):
    try:
        host = urllib.parse.urlparse(url.strip()).netloc.lower()
    except Exception:
        return None
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "tiktok.com" in host:
        return "tiktok"
    return None

def find_link(text):
    """Первая поддерживаемая ссылка в тексте -> (url, kind) или (None, None)."""
    for m in URL_RE.findall(text or ""):
        url = m.rstrip(").,>]\"'")
        k = link_kind(url)
        if k:
            return url, k
    return None, None

# ----------------------------------------------------------------- yt-dlp
def _ytdlp_base():
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
           "--socket-timeout", "30"]
    if PROXY:
        cmd += ["--proxy", PROXY]
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
    """Качает ссылку (TikTok/YouTube) в mp4 через yt-dlp. Возвращает путь к файлу."""
    out = os.path.join(TMP_DIR, f"link_{tag}.%(ext)s")
    # H.264 (avc1) + AAC для YouTube: VP9/AV1 Telegram не декодирует во встроенном
    # плеере — иначе «первый кадр на всё видео + звук».
    fmt = ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/"
           "bv*[vcodec^=avc1][height<=1080]+ba/"
           "b[vcodec^=avc1][height<=1080]/b[ext=mp4][height<=1080]/"
           "bv*[height<=1080]+ba/b[height<=1080]/best" if kind == "youtube"
           else "b/bv*+ba/best")
    cmd = _ytdlp_base() + [
        "--ffmpeg-location", FFMPEG, "-f", fmt,
        "--merge-output-format", "mp4",
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
    if not path:
        cands = [c for c in glob.glob(os.path.join(TMP_DIR, f"link_{tag}.*"))
                 if not c.endswith((".part", ".ytdl"))]
        cands.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
        path = cands[0] if cands else ""
    if not path or not os.path.exists(path) or os.path.getsize(path) < 10000:
        err = [l.strip() for l in (r.stderr or "").splitlines() if l.strip()]
        raise RuntimeError("yt-dlp: " + (err[-1] if err else "не удалось скачать"))
    return path

def video_meta(path):
    """(ширина, высота, длительность) — чтобы Telegram не сплющивал видео в квадрат."""
    w = h = dur = 0
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

# ----------------------------------------------------------------- Telethon userbot
def _tg_proxy():
    if not TG_PROXY:
        return None
    import socks
    u = urllib.parse.urlparse(TG_PROXY)
    ptype = socks.SOCKS5 if u.scheme.startswith("socks") else socks.HTTP
    return (ptype, u.hostname, u.port)

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeVideo

client = TelegramClient(os.path.join(HERE, SESSION), API_ID, API_HASH, proxy=_tg_proxy())

# чтобы не качать одно и то же дважды (повторный апдеёт/правка)
_seen = set()

@client.on(events.NewMessage(outgoing=True))
async def on_my_message(event):
    msg = event.message
    # пропускаем уже-медиа сообщения (включая видео, которые шлём сами) — иначе зацикливание
    if msg.media:
        return
    text = msg.message or ""
    url, kind = find_link(text)
    if not url:
        return
    key = (event.chat_id, msg.id)
    if key in _seen:
        return
    _seen.add(key)
    if len(_seen) > 2000:
        _seen.clear()

    loop = asyncio.get_event_loop()
    tag = f"{abs(hash((event.chat_id, msg.id, url)))%10**9}"
    # текст без самой ссылки -> в подпись к видео (если что-то осталось)
    extra = URL_RE.sub("", text).strip()

    try:
        await event.edit(STATUS_TXT)
    except Exception:
        pass

    try:
        title = await loop.run_in_executor(None, ytdlp_title, url)
        path = await loop.run_in_executor(None, download_link, url, kind, tag)
    except Exception as e:
        print(f"[{LINK_NAME.get(kind, kind)}] не скачал: {e}")
        try:
            if DELETE_ON_FAIL:
                await msg.delete()
            else:
                await event.edit(f"⚠️ Не вышло скачать: {str(e)[:200]}")
        except Exception:
            pass
        return

    try:
        w, h, dur = video_meta(path)
        cap = title or LINK_NAME.get(kind, "")
        if extra:
            cap = (extra + ("\n" + cap if cap else "")).strip()
        cap = cap[:1024]
        attrs = [DocumentAttributeVideo(dur, w, h, supports_streaming=True)]
        await client.send_file(event.chat_id, path, caption=cap,
                               attributes=attrs, supports_streaming=True,
                               reply_to=msg.reply_to_msg_id)
        await msg.delete()   # заменили ссылку видео — убираем исходное сообщение
        print(f"[{LINK_NAME.get(kind, kind)}] отправил: {title[:60]} "
              f"({os.path.getsize(path)//1024} КБ, {w}x{h})")
    except Exception as e:
        print(f"[{LINK_NAME.get(kind, kind)}] не отправил: {e}")
        try:
            await event.edit(f"⚠️ Скачал, но не отправил: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

async def _amain():
    await client.connect()
    if not await client.is_user_authorized():
        print("\n" + "=" * 60)
        print("ПЕРВЫЙ ЗАПУСК: вход во ВТОРУЮ сессию твоего аккаунта.")
        print("Код придёт ВНУТРИ Telegram (сообщение от 'Telegram'), не по СМС.")
        print("=" * 60)
        phone = PHONE or input("📱 Номер телефона (+7...): ").strip()
        await client.send_code_request(phone)
        code = input("📟 Код из Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = TWO_FA or input("🔐 Пароль 2FA: ").strip()
            await client.sign_in(password=pwd)
    me = await client.get_me()
    print(f"[link_replacer] вошёл: {me.first_name} "
          f"(@{getattr(me,'username',None) or me.id})")
    print(f"[link_replacer] прокси: {TG_PROXY or 'нет'} | yt-dlp прокси: {PROXY or 'нет'}")
    print("Готов. Отправь в любой чат ссылку YouTube/TikTok — заменю её на видео.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        import cryptg  # noqa
    except Exception:
        print("[link_replacer] ⚠️ cryptg не установлен — заливка будет медленнее. pip install cryptg")
    client.loop.run_until_complete(_amain())
