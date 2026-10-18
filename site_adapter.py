#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_adapter — интерфейс site-адаптеров YummyDownloader. Логики здесь нет.

Зачем слой: хрупкие парсеры сайтов живут за адаптерами и разбиты на
  * чистые функции разбора уже сохранённого текста/JSON (тестируются на фикстурах),
  * сетевую обвязку (единственное место, где Allowed сеть).
Ядро (core.py) о сайтах не знает: работает с EpisodeRef, а не со страницами.

Типы:
  EpisodeRef(number, label, kind, payload)
      kind == 'iframe'  -> payload = {'iframe_url': str}
      kind == 'serial'  -> payload = {'sig': {...}, 'id': str, 'hash': str,
                                      'ref': str}   (серия внутри kodik-serial)
  TitleInfo(title, groups, total, is_tv, meta)
      groups: {группа(озвучка/сабы): {номер серии: EpisodeRef}}
      total:  сколько уникальных номеров серий нашлось у тайтла
      meta:   сырые ответы сайта, которые адаптер хочет отдать дальше
              (api_info для .me, enum для .tv) — ядро их не читает.

SiteAdapter:
  title_info(url_or_id) -> TitleInfo   # единственный сетевой метод адаптера
  marker(label)         -> ('_озвучка.txt', 'Озвучка') / ('_субтитры.txt', 'Субтитры')
  is_sub(label)         -> bool        # группа — субтитры, а не озвучка
  marker_extra(info)    -> [str, ...]  # доп. строки файла-маркера (плеер/источник)

Реализации: site_me.MeSite (old.yummyani.me), site_tv.TvSite (yummyanime.tv).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

ME_HOST = "yummyani.me"
TV_HOST = "yummyanime.tv"

#: kind EpisodeRef, который уходит в kodik_resolve по обычному iframe-пути.
KIND_IFRAME = "iframe"
#: kind EpisodeRef, который уходит в kodik_resolve по type=seria (kodik serial).
KIND_SERIAL = "serial"


@dataclass
class EpisodeRef:
    """Одна серия тайтла: куда идти за потоком (kind + payload)."""
    number: int
    label: str
    kind: str
    payload: dict = field(default_factory=dict)

    def __repr__(self):
        return "EpisodeRef(#%s %s %s %r)" % (self.number, self.label,
                                             self.kind, self.payload)


@dataclass
class TitleInfo:
    """Всё, что ядру нужно знать про тайтл, без привязки к сайту."""
    title: str
    groups: dict
    total: int = 0
    is_tv: bool = False
    meta: dict = field(default_factory=dict)

    def overview(self):
        """{группа: [номера серий]} — для меню, GUI и смоук-проверок."""
        return {n: sorted(eps) for n, eps in (self.groups or {}).items()}


class SiteAdapter(ABC):
    """Базовый класс адаптера. Наследник обязан реализовать три метода ниже."""

    name = "site"

    @abstractmethod
    def title_info(self, url_or_id) -> TitleInfo:
        """Ссылка или id тайтла -> TitleInfo (единственное место с сетью)."""
        raise NotImplementedError

    @abstractmethod
    def marker(self, label) -> tuple:
        """Имя группы -> (имя файла-маркера, слово для его содержимого)."""
        raise NotImplementedError

    @abstractmethod
    def is_sub(self, label) -> bool:
        """True, если группа — субтитры, а не озвучка."""
        raise NotImplementedError

    def marker_extra(self, info) -> list:
        """Доп. строки файла-маркера после «Озвучка: X» (плеер/источник)."""
        return []

    def matches(self, url_or_id) -> bool:
        """За этот вход адаптер отвечает (используется в get_adapter)."""
        return False


# --------------------------------------------------------------------- выбор
def is_tv_url(url):
    """True, если вход — карточка yummyanime.tv."""
    return isinstance(url, str) and TV_HOST in url.lower()


def is_me_url(url):
    """True, если вход — страница/ссылка old.yummyani.me."""
    return isinstance(url, str) and ME_HOST in url.lower()


def is_tv_id(url_or_id):
    """Числовой id страницы kodik-serial сам по себе не говорит о сайте."""
    return False


def get_adapter(url_or_id, cfg=None):
    """Выбрать адаптера по входу: ссылка .tv / .me / голый page_id."""
    import site_me          # локальный импорт: адаптеры импортируют этот модуль
    import site_tv
    if is_tv_url(url_or_id):
        return site_tv.TvSite(cfg)
    if isinstance(url_or_id, int):
        return site_me.MeSite(cfg)
    if isinstance(url_or_id, str):
        s = url_or_id.strip()
        if s.isdigit():
            return site_me.MeSite(cfg)
        if is_me_url(s) or s.startswith("http"):
            return site_me.MeSite(cfg)
    site = str((cfg or {}).get("site") or "")
    if ME_HOST in site.lower():
        return site_me.MeSite(cfg)
    raise RuntimeError("не знаю сайт по «%s»: жду %s или %s"
                       % (url_or_id, ME_HOST, TV_HOST))