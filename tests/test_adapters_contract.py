#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_adapters_contract — контракт слоя адаптеров сайтов.

Проверяем то, на что опирается ядро (core.py):
  * MeSite/TvSite действительно реализуют интерфейс SiteAdapter;
  * get_adapter() выбирает адаптер по URL / голому id;
  * TitleInfo/EpisodeRef имеют ожидаемые поля и смысл kind/payload;
  * marker()/is_sub()/translation_label() работают на синтетике.

Сеть не используется: title_info()/tv_enumerate() здесь не вызываются,
проверяются только чистые функции и сигнатуры.

Запуск:  python tests/run_tests.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core
import site_me
import site_tv
from site_adapter import (SiteAdapter, TitleInfo, EpisodeRef, get_adapter,
                          is_tv_url, is_me_url, ME_HOST, TV_HOST,
                          KIND_IFRAME, KIND_SERIAL)

ME_URL = "https://old.yummyani.me/catalog/item/mag-vody"
TV_URL = "https://yummyanime.tv/8383-nukitasi-d1.html"

ADAPTERS = ((site_me.MeSite, ME_HOST), (site_tv.TvSite, TV_HOST))


class SiteAdapterInterfaceTests(unittest.TestCase):
    """Оба адаптера реализуют интерфейс целиком."""

    def test_base_is_abstract(self):
        with self.assertRaises(TypeError):
            SiteAdapter()

    def test_both_subclass_siteadapter(self):
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                self.assertTrue(issubclass(cls, SiteAdapter))

    def test_no_abstract_methods_left(self):
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                self.assertEqual(getattr(cls, "__abstractmethods__", set()), set())

    def test_all_interface_methods_present(self):
        for method in ("title_info", "marker", "is_sub", "marker_extra",
                       "matches"):
            for cls, _ in ADAPTERS:
                with self.subTest(adapter=cls.__name__, method=method):
                    self.assertTrue(callable(getattr(cls, method, None)),
                                    "%s.%s" % (cls.__name__, method))

    def test_name_attribute(self):
        for cls, host in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                self.assertEqual(cls.name, host)
                self.assertEqual(cls({}).name, host)

    def test_cfg_is_copied_not_shared(self):
        cfg = {"quality": "720"}
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                ad = cls(cfg)
                self.assertEqual(ad.cfg["quality"], "720")
                ad.cfg["quality"] = "360"
                self.assertEqual(cfg["quality"], "720", "адаптер делит cfg с вызывающим")

    def test_marker_extra_is_optional(self):
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                ad = cls({})
                self.assertIsInstance(ad.marker_extra(TitleInfo(title="x", groups={})),
                                      list)


class GetAdapterTests(unittest.TestCase):
    """Выбор адаптера по входу."""

    def test_tv_url_picks_tvsite(self):
        self.assertIsInstance(get_adapter(TV_URL), site_tv.TvSite)

    def test_me_url_picks_mesite(self):
        self.assertIsInstance(get_adapter(ME_URL), site_me.MeSite)

    def test_numeric_id_picks_mesite(self):
        # Голый page_id однозначно .me: kodik-serial у .tv таких не отдаёт.
        self.assertIsInstance(get_adapter(17482), site_me.MeSite)
        self.assertIsInstance(get_adapter("204"), site_me.MeSite)

    def test_tv_checked_before_me(self):
        # Ссылка с обоими хостами в строке должна уйти в .tv.
        self.assertIsInstance(get_adapter("https://yummyanime.tv/x?from=yummyani.me"),
                              site_tv.TvSite)

    def test_cfg_fallback_for_empty_input(self):
        ad = get_adapter("", cfg={"site": "https://old.yummyani.me"})
        self.assertIsInstance(ad, site_me.MeSite)

    def test_matches_is_consistent_with_get_adapter(self):
        for url, expected in ((ME_URL, site_me.MeSite), (TV_URL, site_tv.TvSite)):
            with self.subTest(url=url):
                self.assertTrue(get_adapter(url).matches(url))
                other = (site_tv.TvSite if expected is site_me.MeSite
                         else site_me.MeSite)
                self.assertFalse(other({}).matches(url))

    def test_url_predicates(self):
        self.assertTrue(is_tv_url(TV_URL))
        self.assertFalse(is_tv_url(ME_URL))
        self.assertTrue(is_me_url(ME_URL))
        self.assertFalse(is_me_url(TV_URL))
        self.assertFalse(is_tv_url(None))
        self.assertFalse(is_me_url(None))

    def test_any_http_url_defaults_to_me_site(self):
        # Не-.tv http-ссылка считается .me (сайт из config по умолчанию),
        # а не «ошибкой» — так задумана функция.
        for url in ("https://example.org/catalog/item/x", "http://old.yummyani.me/x"):
            with self.subTest(url=url):
                self.assertIsInstance(get_adapter(url), site_me.MeSite)

    # ---------------------------------------------------------- регресс
    def test_unknown_input_raises_readable_error(self):
        for bad in ("", None, "не-ссылка"):
            with self.subTest(input=bad):
                with self.assertRaises(RuntimeError) as cm:
                    get_adapter(bad)
                self.assertIn("не знаю сайт", str(cm.exception))


class DataTypeTests(unittest.TestCase):
    """TitleInfo / EpisodeRef — то, что ядро читает."""

    def test_title_info_fields(self):
        ti = TitleInfo(title="X", groups={"A": {1: EpisodeRef(1, "A", KIND_IFRAME)}},
                       total=1, is_tv=False, meta={"k": "v"})
        self.assertEqual(ti.title, "X")
        self.assertEqual(ti.total, 1)
        self.assertFalse(ti.is_tv)
        self.assertEqual(ti.meta, {"k": "v"})
        self.assertIn("A", ti.overview())
        self.assertEqual(ti.overview()["A"], [1])

    def test_title_info_defaults(self):
        ti = TitleInfo(title="X", groups={})
        self.assertEqual(ti.total, 0)
        self.assertFalse(ti.is_tv)
        self.assertEqual(ti.meta, {})

    def test_episode_ref_fields_and_payload_isolation(self):
        a = EpisodeRef(number=1, label="A", kind=KIND_IFRAME)
        b = EpisodeRef(number=1, label="A", kind=KIND_IFRAME)
        a.payload["iframe_url"] = "x"
        self.assertEqual(b.payload, {}, "payload'ы делятся между сериями")

    def test_episode_ref_repr(self):
        text = repr(EpisodeRef(number=3, label="AniLibria", kind=KIND_IFRAME))
        self.assertIn("#3", text)
        self.assertIn("AniLibria", text)
        self.assertIn(KIND_IFRAME, text)

    def test_kind_constants(self):
        self.assertEqual(KIND_IFRAME, "iframe")
        self.assertEqual(KIND_SERIAL, "serial")


class MarkerAndSubTests(unittest.TestCase):
    """Файлы-маркеры и распознавание сабов — на синтетике."""

    DUB = "AniLibria"
    SUB = "[саб] Crunchyroll"

    def test_marker_for_dubbing(self):
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                self.assertEqual(cls({}).marker(self.DUB),
                                 ("_озвучка.txt", "Озвучка"))

    def test_marker_for_subtitles(self):
        for cls, _ in ADAPTERS:
            with self.subTest(adapter=cls.__name__):
                self.assertEqual(cls({}).marker(self.SUB),
                                 ("_субтитры.txt", "Субтитры"))

    def test_both_adapters_agree_on_marker(self):
        self.assertEqual(site_me.marker_for(self.DUB), site_tv.marker_for(self.DUB))
        self.assertEqual(site_me.marker_for(self.SUB), site_tv.marker_for(self.SUB))

    def test_is_sub_synthetic(self):
        for label, expected in ((self.DUB, False), (self.SUB, True),
                                ("", False), (None, False),
                                ("Crunchyroll (Субтитры)", True)):
            with self.subTest(label=label):
                self.assertIs(core.is_sub(label), expected)
                self.assertIs(site_me.MeSite({}).is_sub(label), expected)
                self.assertIs(site_tv.TvSite({}).is_sub(label), expected)

    def test_translation_label_marks_subtitles(self):
        self.assertEqual(site_tv.translation_label({"title": "AniStar"}), "AniStar")
        self.assertEqual(site_tv.translation_label({"title": "AniJoy.Subtitles"}),
                         "[саб] AniJoy")
        self.assertEqual(site_tv.translation_label({}), "")
        self.assertEqual(site_tv.translation_label(None), "")

    def test_choose_dubbing_prefers_voice_over_subs(self):
        groups = {self.DUB: {1: 1}, self.SUB: {1: 1}}
        self.assertEqual(core.choose_dubbing(groups, []), self.DUB)
        self.assertEqual(core.choose_dubbing(groups, [], allow_subs=False), self.DUB)

    def test_choose_dubbing_falls_back_to_subs(self):
        groups = {self.SUB: {1: 1}}
        self.assertEqual(core.choose_dubbing(groups, []), self.SUB)
        self.assertIsNone(core.choose_dubbing(groups, [], allow_subs=False))
        self.assertEqual(core.choose_dubbing({}, [], allow_subs=True), None)


class TvPureHelpersTests(unittest.TestCase):
    """Чистые функции site_tv на синтетике."""

    ALLOHA = {"slot": 0, "title": "Все озвучки", "kind": "iframe",
              "src": "https://ani5.top/2/embed/agp?a=1"}
    KODIK = {"slot": 1, "title": "Kodik", "kind": "iframe",
             "src": "//kodikplayer.com/serial/1/abc/720p"}

    def test_kodik_slots_filters_alloha(self):
        self.assertEqual(site_tv.kodik_slots([self.ALLOHA, self.KODIK]),
                         [("Kodik", self.KODIK["src"])])

    def test_kodik_slots_filters_wrong_kind(self):
        mp4 = {"slot": 2, "title": "mp4", "kind": "video",
               "src": "https://kodikplayer.com/x.mp4"}
        self.assertEqual(site_tv.kodik_slots([mp4]), [])

    def test_pick_slot_makes_protocol_absolute(self):
        title, iframe = site_tv.pick_slot([self.ALLOHA, self.KODIK])
        self.assertEqual(title, "Kodik")
        self.assertTrue(iframe.startswith("https://"))

    def test_pick_slot_falls_back_to_default_title(self):
        _, iframe = site_tv.pick_slot([{"kind": "iframe", "title": "",
                                        "src": "https://kodikplayer.com/x"}])
        self.assertTrue(iframe.startswith("https://kodikplayer.com/"))

    def test_translation_entry_without_seasons_returns_none(self):
        tr = {"tid": "1", "title": "AniStar", "media_id": "1", "media_hash": "h",
              "media_type": "serial", "ep_count": 1, "selected": True}
        self.assertIsNone(site_tv.translation_entry(tr, "<div>нет сезонов</div>", "ref"))

    def test_enum_to_title_info_serial(self):
        enum = {"id": 8383, "title": "Нукитаси", "card_url": TV_URL,
                "slot": "Kodik", "iframe": self.KODIK["src"],
                "dubs": {"AniStar": {"tid": "910", "season": 1,
                                     "eps": {1: ("i1", "h1"), 2: ("i2", "h2")},
                                     "sig": {"domain": "kodikplayer.com"},
                                     "ref": "ref-url"}}}
        ti = site_tv.enum_to_title_info(enum)
        self.assertEqual(ti.title, "Нукитаси")
        self.assertTrue(ti.is_tv)
        self.assertEqual(ti.total, 2)
        ref = ti.groups["AniStar"][2]
        self.assertEqual(ref.kind, KIND_SERIAL)
        self.assertEqual(ref.payload["id"], "i2")
        self.assertEqual(ref.payload["hash"], "h2")
        self.assertEqual(ref.payload["ref"], "ref-url")
        self.assertEqual(ref.payload["sig"], {"domain": "kodikplayer.com"})
        self.assertEqual(ti.meta["site"], TV_HOST)

    def test_enum_to_title_info_single_movie(self):
        enum = {"id": 1, "title": "Фильм", "card_url": TV_URL, "slot": "Kodik",
                "iframe": "https://kodikplayer.com/season/1/h/720p",
                "dubs": {"Kodik": {"single": "https://kodikplayer.com/season/1/h/720p"}}}
        ti = site_tv.enum_to_title_info(enum)
        self.assertEqual(ti.total, 1)
        ref = ti.groups["Kodik"][1]
        self.assertEqual(ref.kind, KIND_IFRAME)
        self.assertIn("iframe_url", ref.payload)

    def test_enum_to_title_info_empty_is_safe(self):
        for bad in (None, {}):
            with self.subTest(enum=bad):
                ti = site_tv.enum_to_title_info(bad)
                self.assertEqual(ti.groups, {})
                self.assertEqual(ti.total, 0)
                self.assertTrue(ti.title)


class MePureHelpersTests(unittest.TestCase):
    """Чистые функции site_me, не покрытые фикстурами."""

    def test_build_title_info_without_data(self):
        ti = site_me.build_title_info({}, [], None)
        self.assertEqual(ti.title, "anime_None")
        self.assertEqual(ti.groups, {})
        self.assertEqual(ti.total, 0)
        self.assertFalse(ti.is_tv)

    def test_groups_to_refs_and_back(self):
        groups = {"AniLibria": {1: "//kodikplayer.com/season/1/h/720p"}}
        refs = site_me.groups_to_refs(groups)
        ref = refs["AniLibria"][1]
        self.assertEqual(ref.kind, KIND_IFRAME)
        self.assertEqual(ref.payload["iframe_url"], groups["AniLibria"][1])
        self.assertEqual(site_me.groups_to_refs({}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
