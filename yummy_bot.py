#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YummyBot — Telegram-бот к YummyDownloader.

Что умеет:
  * поиск аниме по названию (просто напиши текст);
  * принять ссылку на тайтл old.yummyani.me;
  * выбрать озвучку кнопками, указать серию/диапазон;
  * скачать серию и прислать её прямо в чат (видео, смотрится сразу);
  * сразу после отправки удалить файл с диска.

Telegram ходит через HTTP-прокси (v2RayTun, по умолчанию 127.0.0.1:10801).
YummyAnime/Kodik качаются напрямую, без прокси.

Запуск:  py yummy_bot.py
Токен и прокси — в bot_config.json рядом со скриптом (или переменные окружения).
"""

import os, sys, re, json, time, ssl, traceback, mimetypes
import urllib.request, urllib.parse

import certifi
import yummy_dl as Y   # переиспользуем всю логику качалки

# ----- консоль cp866 на русской Windows: не падаем на символах -----
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------- config
DEFAULT_BOT_CFG = {
    "token": "",                      # токен бота (или env YUMMY_BOT_TOKEN)
    "proxy": "http://127.0.0.1:10801",# HTTP-прокси для Telegram; "" — без прокси
    "quality": "480",                 # под лимит Telegram (50 МБ): 480/360
    "max_episodes_per_request": 5,    # сколько серий за один запрос максимум
    "allowed_users": [],              # [] — отвечать всем; иначе список user_id
    "tmp_dir": os.path.join(HERE, "bot_tmp"),
}

def load_bot_cfg():
    cfg = dict(DEFAULT_BOT_CFG)
    p = os.path.join(HERE, "bot_config.json")
    if os.path.exists(p):
        try:
            cfg.update(json.load(open(p, encoding="utf-8")))
        except Exception as e:
            print(f"[!] bot_config.json кривой: {e}")
    cfg["token"] = os.environ.get("YUMMY_BOT_TOKEN") or cfg["token"]
    if os.environ.get("YUMMY_BOT_PROXY"):
        cfg["proxy"] = os.environ["YUMMY_BOT_PROXY"]
    # относительный tmp_dir считаем от папки скрипта
    td = cfg.get("tmp_dir") or "bot_tmp"
    if not os.path.isabs(td):
        td = os.path.join(HERE, td)
    cfg["tmp_dir"] = td
    return cfg

CFG = load_bot_cfg()
CTX = ssl.create_default_context(cafile=certifi.where())
TG_LIMIT = 49 * 1024 * 1024   # запас под лимит Telegram Bot API (50 МБ)

# --------------------------------------------------------------------------- telegram через прокси
def _opener():
    handlers = [urllib.request.HTTPSHandler(context=CTX)]
    if CFG.get("proxy"):
        handlers.insert(0, urllib.request.ProxyHandler(
            {"https": CFG["proxy"], "http": CFG["proxy"]}))
    return urllib.request.build_opener(*handlers)

OPENER = _opener()
API = None  # https://api.telegram.org/bot<token>

def tg(method, params=None, timeout=60):
    """Вызов Bot API (application/x-www-form-urlencoded) через прокси."""
    data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(API + "/" + method, data=data)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def tg_send_video(chat_id, path, caption=""):
    """Загрузка видео multipart/form-data через прокси. Видео играется в чате."""
    boundary = "----yummybot" + str(len(path)) + str(os.path.getsize(path))
    fields = {"chat_id": str(chat_id), "caption": caption,
              "supports_streaming": "true"}
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
    with OPENER.open(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def send(chat_id, text, **kw):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    p.update(kw)
    try:
        return tg("sendMessage", p)
    except Exception as e:
        print("send err:", e)

def kb(rows):
    """rows: [[(text, data), ...], ...] -> inline keyboard json."""
    return json.dumps({"inline_keyboard": [
        [{"text": t, "callback_data": d} for (t, d) in row] for row in rows]})

# --------------------------------------------------------------------------- состояние диалога
STATE = {}   # chat_id -> {"anime_id","title","groups","dub"}

def allowed(uid):
    a = CFG.get("allowed_users") or []
    return (not a) or (uid in a)

# --------------------------------------------------------------------------- yummy helpers
def search(query, limit=8):
    data = Y.api("/search?q=" + urllib.parse.quote(query))
    out = []
    for it in (data or [])[:limit]:
        aid = it.get("anime_id")
        if not aid:
            continue
        t = it.get("title") or "?"
        year = it.get("year") or ""
        typ = ((it.get("type") or {}).get("shortname")) or ""
        out.append((aid, t, year, typ))
    return out

def load_groups(anime_id):
    info = Y.api(f"/anime/{anime_id}")
    items = Y.api(f"/anime/{anime_id}/videos")
    groups = Y.kodik_groups(items)
    title = info.get("title") or f"anime_{anime_id}"
    return title, groups

# --------------------------------------------------------------------------- сценарии
def show_dubbings(chat_id, anime_id):
    send(chat_id, "Загружаю озвучки…")
    try:
        title, groups = load_groups(anime_id)
    except Exception as e:
        send(chat_id, f"Не получилось загрузить тайтл: {e}")
        return
    if not groups:
        send(chat_id, "У этого тайтла нет озвучек в плеере Kodik — скачать нельзя.")
        return
    names = sorted(groups, key=lambda n: -len(groups[n]))
    STATE[chat_id] = {"anime_id": anime_id, "title": title,
                      "groups": groups, "dub": None}
    rows = [[(f"{n} ({len(groups[n])} сер.)", f"d:{i}")] for i, n in enumerate(names)]
    send(chat_id, f"<b>{title}</b>\nВыбери озвучку:", reply_markup=kb(rows))

def pick_dub(chat_id, idx):
    st = STATE.get(chat_id)
    if not st:
        send(chat_id, "Сначала выбери аниме (просто напиши название).")
        return
    names = sorted(st["groups"], key=lambda n: -len(st["groups"][n]))
    if not (0 <= idx < len(names)):
        return
    dub = names[idx]
    st["dub"] = dub
    eps = sorted(st["groups"][dub])
    rng = f"{eps[0]}–{eps[-1]}" if eps else "?"
    send(chat_id,
         f"Озвучка: <b>{dub}</b>\nДоступно серий: {len(eps)} ({rng}).\n\n"
         f"Напиши номер серии (например <code>1</code>) "
         f"или диапазон (<code>1-3</code>). За раз — до "
         f"{CFG['max_episodes_per_request']} серий.")

def fits(qualities, iframe):
    """Пробуем качества по очереди, пока mp4 не влезет в лимит Telegram."""
    last_err = None
    for q in qualities:
        try:
            m3u8, real_q = Y.kodik_m3u8(iframe, q)
        except Exception as e:
            last_err = e
            continue
        tmp = os.path.join(CFG["tmp_dir"], f"ep_{int(time.time()*1000)}.mp4")
        try:
            Y.download_hls(m3u8, tmp)
        except Exception as e:
            last_err = e
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        if os.path.getsize(tmp) <= TG_LIMIT:
            return tmp, real_q
        os.remove(tmp)  # слишком большой — пробуем качество ниже
    if last_err:
        raise last_err
    return None, None

def download_and_send(chat_id, ep_text):
    st = STATE.get(chat_id)
    if not st or not st.get("dub"):
        send(chat_id, "Сначала выбери аниме и озвучку.")
        return
    eps_map = st["groups"][st["dub"]]
    avail = sorted(eps_map)
    keep = sorted(Y.parse_ep_range(ep_text, avail))
    cap = CFG["max_episodes_per_request"]
    if len(keep) > cap:
        send(chat_id, f"За раз максимум {cap} серий — беру первые {cap}.")
        keep = keep[:cap]
    if not keep:
        send(chat_id, "Не понял номера серий. Пример: 1  или  1-3")
        return

    os.makedirs(CFG["tmp_dir"], exist_ok=True)
    # порядок качеств: сначала из конфига, ниже — как запас под лимит
    order, base = [], str(CFG.get("quality", "480"))
    for q in [base, "480", "360"]:
        if q not in order:
            order.append(q)

    for num in keep:
        send(chat_id, f"⏳ Качаю серию {num} ({st['dub']})…")
        path = None
        try:
            path, real_q = fits(order, eps_map[num])
            if not path:
                send(chat_id, f"Серия {num}: не нашёл вариант под лимит Telegram (50 МБ).")
                continue
            cap_txt = f"{st['title']} — серия {num} • {st['dub']} • {real_q}p"
            res = tg_send_video(chat_id, path, caption=cap_txt)
            if not res.get("ok"):
                send(chat_id, f"Серия {num}: Telegram отклонил файл: {res.get('description')}")
        except Exception as e:
            send(chat_id, f"Серия {num}: ошибка — {e}")
        finally:
            if path and os.path.exists(path):
                os.remove(path)   # сразу удаляем файл с ПК
    send(chat_id, "Готово.")

# --------------------------------------------------------------------------- обработка апдейтов
EP_RE = re.compile(r"^[\d\s,\-]+$")

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    uid = (msg.get("from") or {}).get("id")
    if not allowed(uid):
        send(chat_id, "Доступ запрещён.")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text in ("/start", "/help"):
        send(chat_id,
             "Привет! Я качаю аниме с YummyAnime и присылаю серии прямо сюда.\n\n"
             "• Напиши <b>название</b> — поищу.\n"
             "• Или пришли <b>ссылку</b> на тайтл old.yummyani.me.\n"
             "• Выбери озвучку и номер серии — пришлю видео.\n\n"
             "Файлы после отправки удаляются с диска.")
        return
    # ссылка на тайтл
    if text.startswith("http"):
        send(chat_id, "Разбираю ссылку…")
        try:
            aid = Y.resolve_page_id(text)
        except Exception as e:
            send(chat_id, f"Не смог разобрать ссылку: {e}")
            return
        show_dubbings(chat_id, aid)
        return
    # номер серии (если уже выбрана озвучка)
    st = STATE.get(chat_id)
    if st and st.get("dub") and EP_RE.match(text):
        download_and_send(chat_id, text)
        return
    # иначе — поиск по названию
    send(chat_id, f"Ищу «{text}»…")
    try:
        res = search(text)
    except Exception as e:
        send(chat_id, f"Поиск не удался: {e}")
        return
    if not res:
        send(chat_id, "Ничего не нашёл. Попробуй другое название.")
        return
    rows = []
    for aid, t, year, typ in res:
        label = t + (f" ({year})" if year else "") + (f" · {typ}" if typ else "")
        rows.append([(label[:60], f"a:{aid}")])
    send(chat_id, "Что из этого?", reply_markup=kb(rows))

def handle_callback(cq):
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    uid = (cq.get("from") or {}).get("id")
    try:
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
    except Exception:
        pass
    if chat_id is None or not allowed(uid):
        return
    if data.startswith("a:"):
        show_dubbings(chat_id, int(data[2:]))
    elif data.startswith("d:"):
        pick_dub(chat_id, int(data[2:]))

def main():
    global API
    if not CFG.get("token"):
        print("Нет токена. Укажи token в bot_config.json или env YUMMY_BOT_TOKEN.")
        sys.exit(1)
    API = "https://api.telegram.org/bot" + CFG["token"]
    os.makedirs(CFG["tmp_dir"], exist_ok=True)
    # проверим связь
    try:
        me = tg("getMe", timeout=30)
        if not me.get("ok"):
            print("getMe не ок:", me); sys.exit(1)
        print("Бот запущен: @" + me["result"].get("username", "?"),
              "| прокси:", CFG.get("proxy") or "нет")
    except Exception as e:
        print("Не подключиться к Telegram (прокси?):", e); sys.exit(1)

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
