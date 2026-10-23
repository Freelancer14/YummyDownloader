#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_tv — адаптер yummyanime.tv (DLE + player-relay.js + kodik serial-iframe).

Handshake остался в tv_adapter.py (TvOpener / tv_slots / SerialSession /
parse_* — там же cookie-цепочка Referer'ов, её не дублируем), а здесь только
сборка TitleInfo и разбор разметки kodik-serial:

  * data-player-anime-id / data-player-slot + data-player-title -> слоты;
  * .serial-translations-box  -> озвучки/переводы (чистая: parse_translations);
  * .series-options .season-N -> серии сезона (чистая: parse_seasons);
  * ttype='subtitles' (или '.Subtitles' в имени) -> префикс '[саб] '.

Чистые функции (сеть не трогают, тестируются на фикстурах):
  kodik_slots, pick_slot, translation_label, dub_name, translation_entry,
  enum_to_title_info.
Сеть — только в tv_enumerate / TvSite.title_info (через tv_adapter).
"""

import re
import time

import tv_adapter as tva

from core import CFG, sanitize, is_sub, SUB_PREFIX
from site_adapter import (SiteAdapter, TitleInfo, EpisodeRef,
                          KIND_IFRAME, KIND_SERIAL, TV_HOST)

SUB_MARK = ".Subtitles"
TV_HOST_LABEL = "(yummyanime.tv)"
_NO_SEASON_WARNED = []


# ----------------------------------------------------- чистый разбор (без сети)
def kodik_slots(slots):
    """[(title, src)] kodik-слотов: kind=iframe + kodikplayer.com в src."""
    return [((s or {}).get("title") or "", (s or {}).get("src") or "")
            for s in (slots or [])
            if (s or {}).get("kind") == "iframe"
            and "kodikplayer.com" in ((s or {}).get("src") or "")]


def pick_slot(slots):
    """Первый kodik-слот -> (название, абсолютный iframe)."""
    slots = slots or []
    kodik = kodik_slots(slots)
    if not kodik:
        have = ", ".join("%s (%s)" % ((s or {}).get("title"), (s or {}).get("kind"))
                         for s in slots) or "пусто"
        raise RuntimeError("на .tv-карточке нет Kodik-слота, есть: " + have)
    slot_title, iframe = kodik[0]
    if iframe.startswith("//"):
        iframe = "https:" + iframe
    return (slot_title or "Kodik"), iframe


def translation_label(tr):
    """Имя перевода -> имя группы: сабы по ttype, иначе по '.Subtitles'."""
    tr = tr or {}
    raw = tr.get("title") or ""
    bare = re.sub(re.escape(SUB_MARK), "", raw, flags=re.I).strip()
    if str(tr.get("ttype") or "").strip().lower() == "subtitles":
        return SUB_PREFIX + (bare or "сабы")
    if SUB_MARK in raw:
        return SUB_PREFIX + bare
    return raw


def dub_name(tr):
    """Алиас короче: убрать '.Subtitles' (то же, что делает tv_enumerate)."""
    return translation_label(tr)


def translation_entry(tr, html, ref):
    """Перевод + его страница -> запись для enum['dubs'] либо None.

    Сезон по умолчанию — selected на странице, иначе максимальный."""
    seasons, sel = tva.parse_seasons(html)
    if not seasons:
        return None
    if sel is not None and sel not in seasons:
        raise tva.TvError("сезон %s не найден в плеере (есть %s)"
                          % (sel, sorted(seasons)))
    if sel is None:
        season = max(seasons)
        if not _NO_SEASON_WARNED:
            _NO_SEASON_WARNED.append(1)
            print("[!] сезон не выбран в плеере — беру последний (%s)" % season)
    else:
        season = sel
    name = translation_label(tr)
    return name, {"tid": tr["tid"], "season": season, "eps": seasons[season],
                  "sig": tva.parse_sig(html), "ref": ref}


def enum_to_title_info(enum):
    """Чистая сборка TitleInfo из enum tv_enumerate() — ни сети, ни файлов.

    single-фильм -> EpisodeRef(kind='iframe'), сезон -> EpisodeRef(kind='serial')
    с payload для POST /ftor type=seria."""
    title = sanitize((enum or {}).get("title") or "tv_%s" % (enum or {}).get("id", "?"))
    groups = {}
    for name, di in ((enum or {}).get("dubs") or {}).items():
        if di.get("single"):
            eps = {1: EpisodeRef(number=1, label=name, kind=KIND_IFRAME,
                                 payload={"iframe_url": di["single"]})}
        else:
            eps = {n: EpisodeRef(number=n, label=name, kind=KIND_SERIAL,
                                 payload={"sig": di.get("sig") or {}, "id": eid,
                                          "hash": ehash, "ref": di.get("ref") or ""})
                   for n, (eid, ehash) in (di.get("eps") or {}).items()}
        groups[name] = eps
    nums = set()
    for eps in groups.values():
        nums.update(eps)
    return TitleInfo(title=title, groups=groups, total=len(nums), is_tv=True,
                     meta={"enum": enum, "site": TV_HOST})


def marker_for(label):
    """Имя группы -> (файл-маркер, слово для содержимого)."""
    return ("_субтитры.txt", "Субтитры") if is_sub(label) else ("_озвучка.txt", "Озвучка")


# ------------------------------------------------------------------ сеть (обвязка)
def tv_enumerate(card_url):
    """Карточка .tv -> {'id','title','card_url','slot','iframe','dubs'}.

    dubs: {dub_title: {'tid','season','eps':{ep:(epid,ephash)},
                       'sig':{...},'ref':page_url} | {'single':iframe_url}}."""
    op = tva.TvOpener()
    meta = tva.tv_card_meta(card_url, op)
    slots = tva.tv_slots(card_url, op)
    slot_title, iframe = pick_slot(slots)
    enum = {"id": meta["id"], "title": meta["title"] or "tv_%d" % meta["id"],
            "card_url": card_url, "slot": slot_title,
            "iframe": iframe, "dubs": {}}
    if "/serial/" not in iframe:
        # разовый несоставной iframe (фильм): одна "серия" штатным разбором
        enum["dubs"][slot_title] = {"single": iframe}
        return enum
    sess = tva.SerialSession()
    first = sess.prime(iframe)
    trs = tva.parse_translations(first)
    if not trs:
        raise RuntimeError("serial-страница без переводов")
    done = set()

    def _store(tr, html, ref):
        entry = translation_entry(tr, html, ref)
        if entry is None:
            return False
        name, payload = entry
        enum["dubs"][name] = payload
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


class TvSite(SiteAdapter):
    """yummyanime.tv: handshake из tv_adapter -> enum -> TitleInfo."""

    name = TV_HOST

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or CFG)

    def title_info(self, url_or_id) -> TitleInfo:
        if not isinstance(url_or_id, str) or not url_or_id.strip().startswith("http"):
            raise tva.TvError("для yummyanime.tv нужна ссылка на карточку "
                              "вида /<id>-<slug>.html")
        return enum_to_title_info(tv_enumerate(url_or_id))

    def marker(self, label):
        return marker_for(label)

    def is_sub(self, label):
        return is_sub(label)

    def marker_extra(self, info):
        enum = (getattr(info, "meta", None) or {}).get("enum") or {}
        return ["Плеер: %s %s" % (enum.get("slot"), TV_HOST_LABEL),
                "Источник: %s" % enum.get("card_url")]

    def matches(self, url_or_id):
        return isinstance(url_or_id, str) and TV_HOST in url_or_id.lower()