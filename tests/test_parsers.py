#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_parsers — юнит-тесты чистых парсеров на сохранённых ответах сайтов.

Сеть не используется вообще: тесты читают только файлы из tests/fixtures/ и
вызывают функции, которые принимают текст/JSON на входе
(site_me.*, core.kodik_decode, tv_adapter.parse_*, site_tv.*).
Функции с сетью (http_get/api/tv_slots/title_info) здесь не вызываются.

Запуск:  python tests/run_tests.py   (или unittest discover по tests/)
"""

import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core
import site_me
import site_tv
import tv_adapter as tva

ALL_FIXTURES = [
    "me_title_page.html", "me_api_anime.json", "me_api_videos.json",
    "kodik_season_player.html", "kodik_serial_player.html",
    "kodik_ftor_response.json", "tv_card.html", "tv_player_relay.js",
    "tv_xfp.html", "tv_player_slot0.json", "tv_player_slot1.json",
]

_cache = {}


def fx(name):
    """Текст фикстуры (UTF-8)."""
    if name not in _cache:
        with io.open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
            _cache[name] = f.read()
    return _cache[name]


def fx_json(name):
    return json.loads(fx(name))


class FixturePresenceTests(unittest.TestCase):
    """Фикстуры должны быть на месте — иначе остальные тесты врут."""

    def test_all_fixtures_exist_and_nonempty(self):
        for name in ALL_FIXTURES:
            path = os.path.join(FIXTURES, name)
            with self.subTest(fixture=name):
                self.assertTrue(os.path.isfile(path), "нет фикстуры " + name)
                self.assertGreater(os.path.getsize(path), 0, "фикстура пустая: " + name)

    def test_fixtures_are_utf8_without_bom(self):
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name):
                raw = open(os.path.join(FIXTURES, name), "rb").read()
                self.assertNotEqual(raw[:3], b"\xef\xbb\xbf", "BOM в " + name)
                raw.decode("utf-8")  # бросит, если не UTF-8


class MeTitlePageTests(unittest.TestCase):
    """site_me.parse_page_id — единственный разбор HTML страницы тайтла .me."""

    def test_parse_page_id_returns_17482(self):
        self.assertEqual(site_me.parse_page_id(fx("me_title_page.html")), 17482)

    def test_parse_page_id_tolerates_any_attribute_order(self):
        # Атрибут page_id может стоять где угодно в <meta>.
        html = '<meta charset="utf-8"><meta page_id" content="204">'
        self.assertEqual(site_me.parse_page_id(html), 204)

    def test_missing_page_id_raises_readable_error(self):
        # Не «молчаливый мусор»: страница без page_id — это ошибка, а не 0.
        with self.assertRaises(RuntimeError) as cm:
            site_me.parse_page_id("<html><body>капча</body></html>")
        self.assertIn("page_id", str(cm.exception))

    def test_empty_or_none_html_raises(self):
        for bad in ("", None, "<html>"):
            with self.subTest(html=bad):
                with self.assertRaises(RuntimeError):
                    site_me.parse_page_id(bad)


class MeGroupsTests(unittest.TestCase):
    """site_me.groups_from_items — /api/anime/<id>/videos -> {озвучка: {номер: iframe}}."""

    @classmethod
    def setUpClass(cls):
        cls.items = fx_json("me_api_videos.json")["response"]
        cls.groups = site_me.groups_from_items(cls.items)

    def test_group_count_is_14(self):
        self.assertEqual(len(self.groups), 14)

    def test_total_episodes_across_groups_is_142(self):
        self.assertEqual(sum(len(eps) for eps in self.groups.values()), 142)

    def test_every_iframe_is_kodik(self):
        for name, eps in self.groups.items():
            for num, url in eps.items():
                with self.subTest(group=name, ep=num):
                    self.assertIn("kodikplayer.com", url)

    def test_only_kodik_survives_non_kodik_items(self):
        # В фикстуре 260 записей: kodik 142 + alloha 36 + yummyani 82.
        self.assertEqual(len(self.items), 260)
        self.assertTrue(all(site_me.is_kodik(i)
                            for i in self.items
                            if any(u == i["iframe_url"]
                                   for eps in self.groups.values()
                                   for u in eps.values())))

    def test_there_is_a_subtitles_group(self):
        subs = [n for n in self.groups if core.is_sub(n)]
        self.assertTrue(subs, "в тайтле не нашлось ни одной группы-сабов")
        for n in subs:
            self.assertTrue(n.startswith(core.SUB_PREFIX), n)

    def test_episode_numbers_start_from_one(self):
        self.assertEqual(min(min(eps) for eps in self.groups.values()), 1)
        for name, eps in self.groups.items():
            with self.subTest(group=name):
                self.assertIn(1, eps)

    def test_numbers_are_contiguous_ints(self):
        eps = self.groups["AniLibria"]
        self.assertEqual(sorted(eps), list(range(1, len(eps) + 1)))

    def test_unique_episode_count_is_12(self):
        self.assertEqual(site_me.count_episodes(self.groups), 12)

    def test_build_title_info_from_fixtures(self):
        info = fx_json("me_api_anime.json")["response"]
        ti = site_me.build_title_info(info, self.items, 17482)
        self.assertEqual(ti.title, "Маг воды Центральные провинции")
        self.assertEqual(ti.total, 12)
        self.assertFalse(ti.is_tv)
        self.assertEqual(len(ti.groups), 14)
        self.assertEqual(len(ti.overview()), 14)
        self.assertEqual(ti.meta["page_id"], 17482)
        # groups -> EpisodeRef(kind='iframe') с payload под /ftor
        ref = ti.groups["AniLibria"][1]
        self.assertEqual(ref.kind, "iframe")
        self.assertEqual(ref.label, "AniLibria")
        self.assertIn("iframe_url", ref.payload)

    # ---------------------------------------------------------- регресс
    def test_empty_input_gives_empty_groups(self):
        for bad in (None, [], {}):
            with self.subTest(items=bad):
                self.assertEqual(site_me.groups_from_items(bad), {})
        self.assertEqual(site_me.count_episodes({}), 0)

    def test_non_kodik_items_do_not_create_junk_groups(self):
        only_alloha = [{"number": "1", "iframe_url": "https://alloha.yani.tv/x",
                        "data": {"dubbing": "Озвучка AniLibria"}}]
        self.assertEqual(site_me.groups_from_items(only_alloha), {})

    def test_group_label_prefixes(self):
        mk = lambda d: site_me.group_label({"data": {"dubbing": d}})
        self.assertEqual(mk("Озвучка AniLibria"), "AniLibria")
        self.assertEqual(mk("Субтитры Crunchyroll"), "[саб] Crunchyroll")
        # Нет dubbing — понятное имя-заглушка, а не None/пустая строка.
        self.assertEqual(site_me.group_label({}), "Без названия")
        self.assertEqual(site_me.group_label(None), "Без названия")

    def test_episode_number_survives_junk(self):
        self.assertEqual(site_me.episode_number({"number": "7"}), 7)
        self.assertEqual(site_me.episode_number({"number": "12 (TV)"}), 12)
        # Спецвыпуск без номера больше не превращается в «Серия 00» (и не
        # затирает нормальную серию ключом 0) — такие записи пропускаются.
        self.assertIsNone(site_me.episode_number({"number": "OVA"}))
        self.assertIsNone(site_me.episode_number({}))
        self.assertIsNone(site_me.episode_number(None))

    def test_is_kodik_handles_missing_field(self):
        self.assertFalse(site_me.is_kodik({}))
        self.assertFalse(site_me.is_kodik(None))
        self.assertFalse(site_me.is_kodik({"iframe_url": None}))


class KodikSeasonPageTests(unittest.TestCase):
    """tv_adapter.parse_sig — sig-поля страницы плеера Kodik для POST /ftor."""

    SIG_KEYS = ("domain", "d_sign", "pd", "pd_sign", "ref", "ref_sign")

    @classmethod
    def setUpClass(cls):
        cls.html = fx("kodik_season_player.html")
        cls.sig = tva.parse_sig(cls.html)

    def test_all_six_sig_vars_found(self):
        self.assertEqual(set(self.sig), set(self.SIG_KEYS))
        for key in self.SIG_KEYS:
            with self.subTest(sig=key):
                self.assertTrue(self.sig[key], "пустой sig: " + key)

    def test_sig_ref_points_at_me_card(self):
        self.assertEqual(self.sig["domain"], "old.yummyani.me")
        self.assertEqual(self.sig["pd"], "kodikplayer.com")
        self.assertIn("mag-vody", self.sig["ref"])

    def test_type_hash_id_from_player_vars(self):
        # Эти три идут в payload /ftor и не входят в parse_sig.
        for key, expected in (("type", "seria"), ("id", "1478013"),
                              ("hash", "8019bbcdcf0da3cb00d4ab0e90ad6eb5")):
            with self.subTest(key=key):
                m = re.search(r"\.%s = '([^']*)'" % key, self.html)
                self.assertIsNotNone(m, "нет ." + key + " на странице")
                self.assertEqual(m.group(1), expected)

    def test_broken_page_gives_empty_strings_not_missing_keys(self):
        # Ключи должны быть всегда: иначе payload для /ftor упадёт по KeyError.
        sig = tva.parse_sig("<html>битая страница</html>")
        self.assertEqual(set(sig), set(self.SIG_KEYS))
        self.assertFalse(any(sig.values()))
        self.assertEqual(tva.parse_sig(None), sig)
        self.assertEqual(tva.parse_sig(""), sig)


class KodikSerialPageTests(unittest.TestCase):
    """tv_adapter.parse_translations / parse_seasons — переводы и сезоны serial."""

    @classmethod
    def setUpClass(cls):
        cls.html = fx("kodik_serial_player.html")
        cls.translations = tva.parse_translations(cls.html)
        cls.seasons, cls.selected = tva.parse_seasons(cls.html)

    def test_ten_translations(self):
        self.assertEqual(len(self.translations), 10)

    def test_translation_fields_are_filled(self):
        for tr in self.translations:
            with self.subTest(tid=tr["tid"]):
                self.assertTrue(tr["tid"])
                self.assertTrue(tr["title"])
                self.assertTrue(tr["media_id"])
                self.assertTrue(tr["media_hash"])
                self.assertEqual(tr["media_type"], "serial")

    def test_exactly_one_translation_selected(self):
        chosen = [t for t in self.translations if t["selected"]]
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["title"], "AniStar")
        self.assertEqual(tva.selected_translation_id(self.html), chosen[0]["tid"])

    def test_episode_counts_vary_between_translations(self):
        counts = sorted({t["ep_count"] for t in self.translations})
        self.assertIn(11, counts)
        self.assertGreater(len(counts), 1, "все переводы одинаковой длины")

    def test_seasons_shape(self):
        self.assertEqual({k: len(v) for k, v in self.seasons.items()}, {0: 4, 1: 11})

    def test_selected_season_is_1(self):
        self.assertEqual(self.selected, 1)
        self.assertIn(self.selected, self.seasons)

    def test_season_episodes_are_id_hash_pairs(self):
        for season, eps in self.seasons.items():
            for ep, (ep_id, ep_hash) in eps.items():
                with self.subTest(season=season, ep=ep):
                    self.assertGreater(ep, 0)
                    self.assertTrue(ep_id and ep_hash)

    def test_broken_page_gives_empty_structures(self):
        for bad in ("", "<div>нет ни переводов, ни сезонов</div>",
                    "не html вовсе"):
            with self.subTest(html=bad):
                self.assertEqual(tva.parse_translations(bad), [])
                self.assertEqual(tva.parse_seasons(bad), ({}, None))
        self.assertIsNone(tva.selected_translation_id(""))

    def test_none_html_is_not_silently_swallowed(self):
        # parse_translations/parse_seasons ждут строку: None честно падает,
        # а не возвращает пустую структуру (parse_sig, наоборот, терпим к None).
        for bad in (None, 42):
            with self.subTest(html=bad):
                with self.assertRaises(TypeError):
                    tva.parse_translations(bad)
        self.assertEqual(tva.parse_sig(None),
                         {k: "" for k in ("domain", "d_sign", "pd", "pd_sign",
                                          "ref", "ref_sign")})


class FtorResponseTests(unittest.TestCase):
    """POST kodikplayer.com/ftor -> links{качество: [{src}]} + kodik_decode."""

    @classmethod
    def setUpClass(cls):
        cls.ftor = fx_json("kodik_ftor_response.json")

    def test_links_subset_of_known_qualities(self):
        links = self.ftor["links"]
        self.assertTrue(set(links) <= {"360", "480", "720"}, links.keys())

    def test_has_720(self):
        self.assertIn("720", self.ftor["links"])

    def test_every_quality_entry_has_src(self):
        for quality, entries in self.ftor["links"].items():
            with self.subTest(quality=quality):
                self.assertTrue(entries)
                self.assertTrue(entries[0]["src"])

    def test_kodik_decode_gives_m3u8(self):
        src = self.ftor["links"]["720"][0]["src"]
        url = core.kodik_decode(src)
        self.assertIn("m3u8", url)
        self.assertTrue(url.startswith("https://"))

    def test_kodik_decode_works_for_every_quality(self):
        for quality in sorted(self.ftor["links"], key=int):
            with self.subTest(quality=quality):
                self.assertIn("m3u8", core.kodik_decode(self.ftor["links"][quality][0]["src"]))

    def test_ftor_top_level_keys(self):
        # Ядро читает только links; остальное — для контекста ответа.
        self.assertIn("links", self.ftor)

    # ---------------------------------------------------------- регресс
    def test_kodik_decode_rejects_garbage(self):
        for bad in ("", "   ", "not-base64-%%%", "!!!", "TQ"):
            with self.subTest(src=bad):
                with self.assertRaises(RuntimeError):
                    core.kodik_decode(bad)

    def test_kodik_decode_does_not_accept_non_string(self):
        # Известная острота: kodik_decode ждёт строку. Не-строка падает
        # сразу, а не молча возвращает ерунду — фиксируем текущее поведение.
        for bad in (None, 12345, [], {}):
            with self.subTest(src=bad):
                with self.assertRaises((AttributeError, TypeError, RuntimeError)):
                    core.kodik_decode(bad)


class TvCardTests(unittest.TestCase):
    """tv_adapter — разбор карточки .tv (id + слоты)."""

    CARD_URL = "https://yummyanime.tv/8383-nukitasi-d1.html"

    @classmethod
    def setUpClass(cls):
        cls.html = fx("tv_card.html")

    def test_card_id_is_8383(self):
        self.assertEqual(tva.tv_card_id(self.CARD_URL, self.html), 8383)

    def test_card_id_falls_back_to_data_attribute(self):
        # id без цифр в URL — парсер берёт его из разметки.
        self.assertEqual(tva.tv_card_id("https://yummyanime.tv/nukitasi.html",
                                        self.html), 8383)

    def test_two_slots_with_titles(self):
        self.assertEqual(tva._card_slots(self.html),
                         [(0, "Плеер Все озвучки"), (1, "Плеер Kodik")])

    def test_card_title_from_og_tag(self):
        # tv_card_meta ходит в сеть, поэтому здесь только разбор разметки.
        self.assertEqual(re.search(r'og:title"\s+content="([^"]*)"',
                                   self.html).group(1),
                         "Нукитаси смотреть на YummyAnime")

    def test_card_without_id_raises(self):
        with self.assertRaises(tva.TvError):
            tva.tv_card_id("https://yummyanime.tv/some-page.html", "")

    def test_card_without_slots_raises_readable_error(self):
        class NoSlotsOpener:
            """Карточка без блоков data-player-slot -> понятная ошибка."""

            def get(self, url, ref=None, extra=None):
                return 200, "<html><body>капча</body></html>"

        with self.assertRaises(tva.TvError) as cm:
            tva.tv_slots(self.CARD_URL, opener=NoSlotsOpener())
        self.assertIn("data-player-slot", str(cm.exception))

    def test_card_slots_empty_for_foreign_markup(self):
        self.assertEqual(tva._card_slots("<div>ничего похожего</div>"), [])
        self.assertEqual(tva._card_slots(""), [])


class TvSlotsTests(unittest.TestCase):
    """Ответы mod=player -> слоты -> site_tv отбирает kodik-сериал."""

    @classmethod
    def setUpClass(cls):
        cls.slots = []
        for num, title in tva._card_slots(fx("tv_card.html")):
            data = fx_json("tv_player_slot%d.json" % num)["data"]
            cls.slots.append({"slot": num, "title": title or data.get("name") or "",
                              "kind": data["kind"], "src": data["src"]})

    def test_slot0_is_alloha(self):
        s0 = self.slots[0]
        self.assertEqual(s0["slot"], 0)
        self.assertEqual(s0["kind"], "iframe")
        self.assertIn("ani5.top", s0["src"])          # Alloha
        self.assertNotIn("kodikplayer.com", s0["src"])

    def test_slot1_is_kodik_serial(self):
        s1 = self.slots[1]
        self.assertEqual(s1["slot"], 1)
        self.assertIn("kodikplayer.com/serial/", s1["src"])
        self.assertNotIn("season", s1["src"])         # serial, не season-iframe

    def test_slots_report_status_ok(self):
        for n in (0, 1):
            with self.subTest(slot=n):
                self.assertTrue(fx_json("tv_player_slot%d.json" % n)["status"])

    def test_kodik_slots_filters_alloha_out(self):
        kodik = site_tv.kodik_slots(self.slots)
        self.assertEqual(len(kodik), 1)
        self.assertEqual(kodik[0][0], "Плеер Kodik")

    def test_pick_slot_returns_kodik_iframe(self):
        title, iframe = site_tv.pick_slot(self.slots)
        self.assertEqual(title, "Плеер Kodik")
        self.assertIn("kodikplayer.com", iframe)

    def test_slot1_iframe_matches_serial_fixture(self):
        # src слота 1 — ровно тот вход, с которого начинается serial-фикстура.
        src = self.slots[1]["src"]
        self.assertEqual(src, "https://kodikplayer.com/serial/68923/"
                              "ac726260717824f47286e55e43fc62d6/720p")

    # ---------------------------------------------------------- регресс
    def test_kodik_slots_on_empty_input(self):
        for bad in (None, [], [{}], [{"kind": "iframe"}]):
            with self.subTest(slots=bad):
                self.assertEqual(site_tv.kodik_slots(bad), [])

    def test_pick_slot_without_kodik_raises_readable_error(self):
        only_alloha = [self.slots[0]]
        with self.assertRaises(RuntimeError) as cm:
            site_tv.pick_slot(only_alloha)
        self.assertIn("Kodik", str(cm.exception))

    def test_pick_slot_on_empty_raises(self):
        with self.assertRaises(RuntimeError):
            site_tv.pick_slot([])


class TvHandshakeArtifactsTests(unittest.TestCase):
    """Промежуточные ответы handshake .tv — нужны, чтобы цепочка была полной."""

    def test_xfp_response_nonempty(self):
        xfp = fx("tv_xfp.html")
        self.assertTrue(xfp.strip(), "mod=xfp вернул пустую страницу")

    def test_relay_js_mentions_player_endpoint(self):
        js = fx("tv_player_relay.js")
        self.assertIn("controller.php", js)
        self.assertIn("mod=player", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
