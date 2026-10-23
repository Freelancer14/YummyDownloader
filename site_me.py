#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_me — адаптер old.yummyani.me (DLE + внутренний API /api/anime/<id>).

Разделение намеренное:
  * чистые функции (принимают текст/JSON, отдают данные) — тестируются на
    фикстурах без сети: parse_page_id, group_label, episode_number,
    groups_from_items, build_title_info;
  * сетевая обвязка — page_id_from / MeSite.title_info, и всё, что ниже,
    дергает core.http_get / core.api.

Хрупкое здесь: префиксы 'Озвучка '/'Субтитры ' в data.dubbing и фильтр
kodikplayer.com в iframe_url — поэтому и вынесено в отдельные функции.
"""

import re
import unicodedata

from core import SITE, CFG, http_get, api, sanitize, is_sub, SUB_PREFIX
from site_adapter import (SiteAdapter, TitleInfo, EpisodeRef,
                          KIND_IFRAME, ME_HOST)

DUB_PREFIX = "Озвучка "
SUB_RAW_PREFIX = "Субтитры "

_META_PAGE_ID_RE = re.compile(r"<meta\b[^>]*\bpage_id\b[^>]*>", re.I)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*["']\s*(\d+)\s*["']""", re.I)
_GROUP_PREFIX_RE = re.compile(r"^(озвучка|субтитры)\b\s*", re.I)


# --------------------------------------------------------- чистый разбор (без сети)
def parse_page_id(html):
    """page_id из <meta ... page_id ... content="12345"> — в любом порядке."""
    for tag in _META_PAGE_ID_RE.finditer(html or ""):
        m = _META_CONTENT_RE.search(tag.group(0))
        if m:
            return int(m.group(1))
    raise RuntimeError("не нашёл page_id на странице - проверь ссылку")


def is_kodik(item):
    """Оставить только плеер Kodik (Alloha/CVH не качаем)."""
    return "kodikplayer.com" in ((item or {}).get("iframe_url") or "")


def group_label(item):
    """data.dubbing -> имя группы: 'Озвучка AniLibria' -> 'AniLibria',
    'Субтитры Crunchyroll' -> '[саб] Crunchyroll'.

    Префикс ищется регуляркой после NFKC-нормализации: '.replace()' не ловил
    «Субтитры» без имени и неразрывные пробелы."""
    raw = ((item or {}).get("data") or {}).get("dubbing")
    d = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(raw or ""))).strip()
    m = _GROUP_PREFIX_RE.match(d)
    if not m:
        return d or "Без названия"
    rest = d[m.end():].strip()
    if m.group(1).lower() == "субтитры":
        return SUB_PREFIX + (rest or "сабы")
    return rest or "Без названия"


def episode_number(item):
    """Номер серии из item.number: первое целое в строке, иначе None.

    '1.5' -> 1 (а не 15), 'SP1' -> 1, 'OVA' -> None (серию без номера
    пропускаем, а не пишем под ключом 0)."""
    s = str((item or {}).get("number") or "")
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def groups_from_items(items):
    """Список серий из /videos -> {группа: {номер: iframe_url}} (только Kodik)."""
    groups = {}
    for it in items or []:
        if not is_kodik(it):
            continue
        num = episode_number(it)
        if num is None:
            print("[!] серия без номера пропущена: %r"
                  % ((it or {}).get("number"),))
            continue
        name = group_label(it)
        eps = groups.setdefault(name, {})
        if num in eps:
            print("[!] серия %s в группе «%s» повторяется — оставляю первую"
                  % (num, name))
            continue
        eps[num] = it.get("iframe_url") or ""
    return groups


def groups_to_refs(groups):
    """{группа: {номер: iframe}} -> {группа: {номер: EpisodeRef(kind='iframe')}}."""
    out = {}
    for name, eps in (groups or {}).items():
        out[name] = {num: EpisodeRef(number=num, label=name, kind=KIND_IFRAME,
                                     payload={"iframe_url": url})
                     for num, url in eps.items()}
    return out


def count_episodes(groups):
    """Сколько уникальных номеров серий у тайтла (по всем озвучкам)."""
    nums = set()
    for eps in (groups or {}).values():
        nums.update(eps)
    return len(nums)


def build_title_info(info, items, page_id=None):
    """Чистая сборка TitleInfo из ответов API: ни сети, ни файлов."""
    title = sanitize(info.get("title") or f"anime_{page_id}")
    raw = groups_from_items(items)
    ti = TitleInfo(title=title, groups=groups_to_refs(raw),
                   total=count_episodes(raw), is_tv=False,
                   meta={"api_info": info, "page_id": page_id, "site": SITE})
    return ti


def marker_for(label):
    """Имя группы -> (файл-маркер, слово для содержимого)."""
    return ("_субтитры.txt", "Субтитры") if is_sub(label) else ("_озвучка.txt", "Озвучка")


def kodik_groups(items):
    """Совместимая обёртка (GUI зовёт yummy_dl.kodik_groups) — {группа: {номер: iframe}}."""
    return groups_from_items(items)


# ------------------------------------------------------------------ сеть (обвязка)
def page_id_from(url_or_id):
    """Ссылка или голый page_id -> int (ссылку один раз качаем и разбираем)."""
    if isinstance(url_or_id, int):
        return url_or_id
    s = str(url_or_id or "").strip()
    if s.isdigit():
        return int(s)
    return parse_page_id(http_get(s))


def resolve_page_id(url):
    """Из ссылки на тайтл вытащить числовой page_id."""
    return parse_page_id(http_get(url))


class MeSite(SiteAdapter):
    """old.yummyani.me: /api/anime/<id> + /api/anime/<id>/videos -> TitleInfo."""

    name = ME_HOST

    def __init__(self, cfg=None):
        self.cfg = dict(cfg or CFG)

    def title_info(self, url_or_id) -> TitleInfo:
        pid = page_id_from(url_or_id)
        info = api(f"/anime/{pid}")
        items = api(f"/anime/{pid}/videos")
        return build_title_info(info, items, pid)

    def marker(self, label):
        return marker_for(label)

    def is_sub(self, label):
        return is_sub(label)

    def marker_extra(self, info):
        return ["Источник: " + SITE]

    def matches(self, url_or_id):
        if isinstance(url_or_id, int):
            return True
        s = str(url_or_id or "").strip()
        return s.isdigit() or ME_HOST in s.lower()