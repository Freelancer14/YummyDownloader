#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tv_adapter — вход на yummyanime.tv поверх Kodik-пайплайна.

Схема .tv (DLE + player-relay.js):
  1. GET карточки /<id>-<slug>.html (Chrome UA + cookie-jar) — в HTML лежат
     data-player-anime-id и блоки [data-player-slot + data-player-title].
  2. GET /engine/ajax/controller.php?mod=xfp (Referer карточки) — relay-болванка.
  3. GET mod=player&id=<id>&slot=N (Referer xfp + X-Requested-With) —
     JSON {status, data:{kind, src, name, label}}.
     Без VPN: слот 0 = Alloha iframe, слот 1 = Kodik serial iframe,
     слоты 2+ -> 404. Сюда ходим НАПРЯМУЮ, прокси из env не форсим.

Kodik serial iframe (вся ветка серий внутри одной страницы):
  - переводы (озвучки): <option data-media-id/hash/type/title/episode-count>
    в .serial-translations-box select.
  - сезоны/серии: <div class="series-options"> -> <div class="season-N">
    с <option value data-id data-hash data-title="N серия">.
  - выбор другого перевода в браузере = перезагрузка iframe на
    /serial/<media-id>/<media-hash>/720p; сервер отдаёт нужный перевод
    только при живых kodik-cookie + Referer kodik-страницы (иначе default).
  - серия резолвится POST /ftor {type:'seria', hash, id + sig-поля страницы}
    (это делает yummy_dl.tv_m3u8 поверх kodik_decode).

Публичное API:
  tv_slots(card_url)         -> [{slot, title, kind, src}]
  tv_kodik_iframes(card_url) -> [(title, src)]  (kind=iframe + kodikplayer.com)
  tv_card_meta(card_url)     -> {'id', 'title', 'slots': [(slot, title)]}
  class SerialSession: prime(iframe_url) -> html; page(media_id, media_hash,
      mtype='serial') -> html (цепочка Referer + проверка перевода)
  parse_translations(html)   -> [{tid, title, ttype, media_id, media_hash,
                                 ep_count, selected}]
  parse_seasons(html)        -> ({season: {ep: (epid, ephash)}}, selected_season|None)
  parse_sig(html)            -> {domain, d_sign, pd, pd_sign, ref, ref_sign}
  selected_translation_id(html) -> data-id выбранного перевода | None

Сеть: urllib + CookieJar, timeout 30, ретраи 2x (404 не ретраится).
Alloha/CVH не трогаем (точка входа зафиксирована, реализации нет).
"""

import re
import json
import time
import http.cookiejar
import urllib.request
import urllib.error

TV_HOST = "yummyanime.tv"
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 30
RETRIES = 2  # всего попыток (первая + 1 повтор)


class TvError(RuntimeError):
    """Не смогли пройти handshake .tv или разобрать страницу."""


# ------------------------------------------------------------------ http layer
class TvOpener:
    """urllib-opener с cookie-jar и Chrome UA. Прокси не форсит."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, ref=None, extra=None):
        hd = {"User-Agent": CHROME_UA,
              "Referer": ref or ("https://%s/" % TV_HOST)}
        if extra:
            hd.update(extra)
        last = None
        for _ in range(max(1, RETRIES)):
            try:
                req = urllib.request.Request(url, headers=hd)
                r = self.op.open(req, timeout=TIMEOUT)
                return r.status or 200, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise
                last = e
            except Exception as e:
                last = e
            time.sleep(1)
        raise TvError("GET %s: %s" % (url, last))


_XFP = "https://%s/engine/ajax/controller.php?mod=xfp" % TV_HOST
_PLAYER_TMPL = ("https://" + TV_HOST +
               "/engine/ajax/controller.php?mod=player&id=%s&slot=%d")

_URL_ID_RE = re.compile(r"/(\d+)-[^/]*\.html")
_ANIME_ID_RE = re.compile(r'data-player-anime-id="(\d+)"')
_SLOT_RE = re.compile(
    r'data-player-slot="(\d+)"[^>]*data-player-title="([^"]*)"')
_SLOT_RE_ALT = re.compile(
    r'data-player-title="([^"]*)"[^>]*data-player-slot="(\d+)"')


def tv_card_id(card_url, html=None):
    """id тайтла: сначала из URL /<id>-<slug>.html, fallback — data-player-anime-id."""
    m = _URL_ID_RE.search(card_url or "")
    if m:
        return int(m.group(1))
    if html:
        m = _ANIME_ID_RE.search(html)
        if m:
            return int(m.group(1))
    raise TvError("не нашёл id тайтла (ни в URL, ни в data-player-anime-id)")


def _card_slots(html):
    slots = {}
    for m in _SLOT_RE.finditer(html):
        slots[int(m.group(1))] = m.group(2)
    for m in _SLOT_RE_ALT.finditer(html):
        slots[int(m.group(2))] = m.group(1)
    return sorted(slots.items())


def tv_slots(card_url, opener=None):
    """Handshake .tv -> [{slot, title, kind, src}]. Слот с 404 пропускаем."""
    op = opener or TvOpener()
    _, card = op.get(card_url)
    try:
        anime_id = tv_card_id(card_url, card)
    except TvError:
        raise
    slots = _card_slots(card)
    if not slots:
        raise TvError("на карточке нет data-player-slot блоков")
    op.get(_XFP, ref=card_url)  # relay-болванка (куки/сессия)
    out = []
    for num, title in slots:
        url = _PLAYER_TMPL % (anime_id, num)
        try:
            _, body = op.get(url, ref=_XFP, extra={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json"})
        except urllib.error.HTTPError as e:
            if getattr(e, "code", None) == 404:
                continue  # слотов больше нет
            raise TvError("player slot %d: %s" % (num, e))
        try:
            j = json.loads(body)
            data = j.get("data") or {}
        except Exception:
            raise TvError("player slot %d: не JSON" % num)
        if not j.get("status") or not data.get("src"):
            continue
        out.append({"slot": num, "title": title or data.get("name") or "",
                    "kind": data.get("kind") or "",
                    "src": data.get("src") or ""})
    return out


def tv_kodik_iframes(card_url, opener=None):
    """[(title, src)] kodik-слотов: kind=iframe + kodikplayer.com в src."""
    return [(s["title"], s["src"]) for s in tv_slots(card_url, opener)
            if s["kind"] == "iframe" and "kodikplayer.com" in s["src"]]


_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_OG_RE = re.compile(r'og:title"\s+content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def _clean_title(raw):
    t = re.sub(r"<[^>]+>", "", raw or "").strip()
    for tail in (" смотреть на YummyAnime",
                 " смотреть аниме сериал онлайн на русском"):
        if t.endswith(tail):
            t = t[:-len(tail)].strip()
    return t


def tv_card_meta(card_url, opener=None):
    """{'id', 'title', 'slots': [(slot, title)]}. Один GET карточки."""
    op = opener or TvOpener()
    _, card = op.get(card_url)
    meta = {"id": tv_card_id(card_url, card), "title": "", "slots": []}
    m = _H1_RE.search(card)
    if m and _clean_title(m.group(1)):
        meta["title"] = _clean_title(m.group(1))
    if not meta["title"]:
        m = _OG_RE.search(card)
        if m and _clean_title(m.group(1)):
            meta["title"] = _clean_title(m.group(1))
    if not meta["title"]:
        m = _TITLE_RE.search(card)
        if m:
            meta["title"] = _clean_title(m.group(1))
    if not meta["title"]:
        m = re.search(r"/\d+-([^/]+)\.html", card_url)
        meta["title"] = (m.group(1).replace("-", " ") if m
                         else "tv_%d" % meta["id"])
    meta["slots"] = _card_slots(card)
    return meta


# ------------------------------------------------------- kodik serial parsing
_OPT_RE = re.compile(r"<option\b(.*?)>(.*?)</option>", re.S)
_ATTR_RE = re.compile(r'data-([a-z-]+)="([^"]*)"')
_SEASON_DIV_RE = re.compile(r'<div class="season-(\d+)">(.*?)</div>\s*(?=<div|<style|<script|</div)',
                            re.S)
_SIG_RE = re.compile(r'var (domain|d_sign|pd|pd_sign|ref|ref_sign) = "([^"]*)";')
_TR_ID_RE = re.compile(r"var translationId = (\d+);")
_SEL_TR_RE = re.compile(
    r'<option\b[^>]*data-id="(\d+)"[^>]*selected="selected"[^>]*>', re.S)


def _attrs(opt_tag):
    return dict(_ATTR_RE.findall(opt_tag or ""))


def parse_translations(html):
    """Все озвучки serial-страницы (включая выбранную)."""
    out = []
    for m in _OPT_RE.finditer(html):
        a = _attrs(m.group(1))
        if not (a.get("media-id") and a.get("media-hash")):
            continue
        try:
            ep_count = int(a.get("episode-count") or 0)
        except Exception:
            ep_count = 0
        out.append({
            "tid": a.get("id") or "",
            "title": (a.get("title") or "").strip(),
            "ttype": a.get("translation-type") or "",
            "media_id": a.get("media-id") or "",
            "media_hash": a.get("media-hash") or "",
            "media_type": a.get("media-type") or "serial",
            "ep_count": ep_count,
            "selected": 'selected="selected"' in m.group(1),
        })
    return out


def parse_seasons(html):
    """({season: {ep: (epid, ephash)}}, selected_season|None).

    Берём только <div class="season-N"> (эпизоды текущего перевода);
    одиночные <select>-дубликаты игнорируем, чтобы не задвоить серии."""
    seasons = {}
    for m in _SEASON_DIV_RE.finditer(html):
        eps = {}
        for om in _OPT_RE.finditer(m.group(2)):
            a = _attrs(om.group(1))
            if not (a.get("id") and a.get("hash")):
                continue
            try:
                ep = int(re.sub(r"\D", "", a.get("title") or "") or 0)
            except Exception:
                ep = 0
            if not ep:
                try:
                    ep = int(a.get("value") or 0)
                except Exception:
                    ep = 0
            if ep > 0:
                eps[ep] = (a["id"], a["hash"])
        if eps:
            seasons[int(m.group(1))] = eps
    sel = None
    sm = re.search(
        r'<div class="serial-seasons-box">.*?<option\b[^>]*value="(\d+)"[^>]*selected="selected"',
        html, re.S)
    if sm:
        try:
            sel = int(sm.group(1))
        except Exception:
            sel = None
    return seasons, sel


def parse_sig(html):
    """Sig-поля страницы для POST /ftor."""
    sig = dict(_SIG_RE.findall(html or ""))
    for k in ("domain", "d_sign", "pd", "pd_sign", "ref", "ref_sign"):
        sig.setdefault(k, "")
    return sig


def selected_translation_id(html):
    m = _TR_ID_RE.search(html or "")
    if m:
        return m.group(1)
    m = _SEL_TR_RE.search(html or "")
    return m.group(1) if m else None


class SerialSession:
    """Cookie-сессия для перебора переводов serial-страницы.

    Первый GET (prime) ставит kodik-cookie; дальше каждый перевод грузим
    своим GET с Referer предыдущей kodik-страницы — иначе сервер отдаёт
    дефолтный перевод вместо запрошенного. Проверяем translationId."""

    def __init__(self, opener=None):
        self.op = opener or TvOpener()
        self.last_url = None

    def prime(self, iframe_url):
        _, html = self.op.get(iframe_url, ref="https://%s/" % TV_HOST)
        self.last_url = iframe_url
        return html

    def page(self, media_id, media_hash, mtype="serial", expect_tid=None):
        url = "https://kodikplayer.com/%s/%s/%s/720p" % (
            mtype, media_id, media_hash)
        _, html = self.op.get(url, ref=self.last_url or
                              ("https://%s/" % TV_HOST))
        self.last_url = url
        if expect_tid:
            got = selected_translation_id(html)
            if got is not None and str(got) != str(expect_tid):
                raise TvError(
                    "перевод %s не выбрался (сервер отдал %s)" % (
                        expect_tid, got))
        return html
