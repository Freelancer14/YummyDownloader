#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_regressions — регрессы на баги, найденные в аудите 1.1.1.

Каждый тест закрепляет конкретное поведение, которое раньше молча ломалось:
разбор диапазона серий, sanitize имён для Windows, Kodik /ftor, склейка
сегментов и политика fallback. Без сети и без реального ffmpeg.
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import core      # noqa: E402
import seg_dl    # noqa: E402


class ParseEpRangeTests(unittest.TestCase):
    """Промах по номеру не должен превращаться в «качать всё»."""

    AVAIL = [1, 2, 3, 4, 5]

    def test_missing_episode_is_empty_not_all(self):
        self.assertEqual(core.parse_ep_range("99", self.AVAIL), set())

    def test_reversed_range_is_empty_not_all(self):
        self.assertEqual(core.parse_ep_range("10-5", self.AVAIL), set())

    def test_space_is_separator_not_digit_glue(self):
        # раньше "1 2" -> "12" (одна несуществующая серия)
        self.assertEqual(core.parse_ep_range("1 2", self.AVAIL), {1, 2})

    def test_malformed_range_does_not_raise(self):
        self.assertEqual(core.parse_ep_range("1-3-5", self.AVAIL), set())

    def test_open_range_uses_available_bounds(self):
        self.assertEqual(core.parse_ep_range("2-", self.AVAIL), {2, 3, 4, 5})
        self.assertEqual(core.parse_ep_range("-3", self.AVAIL), {1, 2, 3})

    def test_range_is_clamped_to_available(self):
        self.assertEqual(core.parse_ep_range("4-99", self.AVAIL), {4, 5})

    def test_empty_input_means_all(self):
        self.assertEqual(core.parse_ep_range("", self.AVAIL), set(self.AVAIL))
        self.assertEqual(core.parse_ep_range("все", self.AVAIL), set(self.AVAIL))

    def test_empty_available_does_not_crash(self):
        self.assertEqual(core.parse_ep_range("", []), set())
        self.assertEqual(core.parse_ep_range("3-", []), set())
        self.assertEqual(core.parse_ep_range("-3", []), set())


class SanitizeTests(unittest.TestCase):
    """Имена, которые Windows молча обрежет или не создаст вовсе."""

    def test_trailing_dot_and_space_removed(self):
        self.assertEqual(core.sanitize("Foo ."), "Foo")

    def test_reserved_names_are_escaped(self):
        for name in ("NUL", "CON", "PRN", "AUX", "COM1", "LPT9"):
            self.assertEqual(core.sanitize(name), "_" + name)
            self.assertEqual(core.sanitize(name + ".mp4"), "_" + name + ".mp4")

    def test_reserved_lookalikes_untouched(self):
        self.assertEqual(core.sanitize("CONAN"), "CONAN")
        self.assertEqual(core.sanitize("NULl"), "NULl")

    def test_forbidden_chars_and_empties(self):
        self.assertEqual(core.sanitize('a\\b/c:d*e?f"g<h>i|j'), "abcdefghij")
        self.assertEqual(core.sanitize("   "), "untitled")
        self.assertEqual(core.sanitize(None), "untitled")


class FtorTests(unittest.TestCase):
    """Kodik /ftor: качество, типы и понятные ошибки вместо IndexError."""

    def setUp(self):
        p = mock.patch.object(core, "kodik_decode",
                              lambda s: "https://cdn.example/x.m3u8")
        p.start()
        self.addCleanup(p.stop)

    def _links(self, links):
        p = mock.patch.object(core, "http_post",
                              lambda *a, **k: json.dumps({"links": links}))
        p.start()
        self.addCleanup(p.stop)

    def test_quality_as_int_does_not_crash(self):
        self._links({"480": [{"src": "a"}], "720": [{"src": "b"}]})
        m3u8, q = core._ftor({}, "season", "1", "h", "u", 720)
        self.assertEqual(q, 720)          # ровно запрошенное качество

    def test_lower_quality_when_requested_missing(self):
        self._links({"480": [{"src": "a"}], "1080": [{"src": "b"}]})
        _, q = core._ftor({}, "season", "1", "h", "u", "720")
        self.assertEqual(q, 480)          # ближайшее снизу, а не 1080

    def test_empty_quality_list_is_skipped(self):
        # links["720"] == [] раньше давал IndexError
        self._links({"480": [{"src": "a"}], "720": []})
        _, q = core._ftor({}, "season", "1", "h", "u", "max")
        self.assertEqual(q, 480)

    def test_no_links_is_runtime_error(self):
        self._links({})
        with self.assertRaises(RuntimeError):
            core._ftor({}, "season", "1", "h", "u", "max")

    def test_links_without_src_is_runtime_error(self):
        self._links({"720": []})
        with self.assertRaises(RuntimeError):
            core._ftor({}, "season", "1", "h", "u", "max")


class IframeSigTests(unittest.TestCase):
    """Битая страница плеера — понятная ошибка, а не AttributeError."""

    def test_missing_fields_raise_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            core._iframe_sig("<html></html>")
        self.assertIn("не разобрана", str(ctx.exception))

    def test_valid_page_parsed(self):
        html = ('var domain = "kodikplayer.com";'
                " .type = 'season'; .hash = 'abc'; .id = '42';")
        sig, typ, hsh, ident = core._iframe_sig(html)
        self.assertEqual(sig["domain"], "kodikplayer.com")
        self.assertEqual((typ, hsh, ident), ("season", "abc", "42"))


class SelectGroupTests(unittest.TestCase):
    """Явно выбранная озвучка не подменяется другой."""

    GROUPS = {"AniStar": {1: "a"}, "AniLibria": {1: "b"}}

    def test_unknown_override_returns_none(self):
        self.assertIsNone(core.select_group(self.GROUPS, {}, dub_override="Нет такой"))

    def test_override_matches_by_substring(self):
        self.assertEqual(core.select_group(self.GROUPS, {}, dub_override="star"),
                         "AniStar")


class UrlopenCloseTests(unittest.TestCase):
    """Ответ urllib закрывается, а не ждёт GC."""

    def test_response_closed(self):
        class Resp:
            closed = False

            def read(self):
                return b"ok"

            def close(self):
                self.closed = True

        resp = Resp()
        with mock.patch.object(core.urllib.request, "urlopen", return_value=resp):
            self.assertEqual(core._urlopen_once(mock.Mock(), 5, True), b"ok")
        self.assertTrue(resp.closed)


class PlaylistPolicyTests(unittest.TestCase):
    """Что уводит серию на прямой ffmpeg."""

    def test_aes_after_none_is_detected(self):
        text = ("#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\na.ts\n"
                "#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n#EXTINF:4,\nb.ts")
        self.assertIn("AES-128", seg_dl.playlist_needs_fallback(text) or "")

    def test_none_only_is_not_encrypted(self):
        self.assertIsNone(
            seg_dl.playlist_needs_fallback("#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\na.ts"))

    def test_session_key_is_detected(self):
        self.assertIsNotNone(
            seg_dl.playlist_needs_fallback('#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="k"'))

    def test_map_and_discontinuity_still_fallback(self):
        self.assertIsNotNone(seg_dl.playlist_needs_fallback("#EXT-X-MAP:URI=\"i.mp4\""))
        self.assertIsNotNone(seg_dl.playlist_needs_fallback("#EXT-X-DISCONTINUITY"))

    def test_duration_sums_extinf(self):
        self.assertAlmostEqual(
            seg_dl.playlist_duration("#EXTINF:4.5,\na\n#EXTINF:3,\nb"), 7.5)
        self.assertEqual(seg_dl.playlist_duration(""), 0.0)


class SegmentPayloadTests(unittest.TestCase):
    """HTML-заглушка CDN не должна считаться скачанным сегментом."""

    def test_html_detected(self):
        self.assertTrue(seg_dl._looks_like_error_page(b"<!DOCTYPE html><html>"))
        self.assertTrue(seg_dl._looks_like_error_page(b"  <html><body>403"))
        self.assertTrue(seg_dl._looks_like_error_page(b'{"error":"blocked"}'))

    def test_media_not_detected(self):
        self.assertFalse(seg_dl._looks_like_error_page(b"\x47\x40\x11\x10"))
        self.assertFalse(seg_dl._looks_like_error_page(b""))


class ConcatErrorTests(unittest.TestCase):
    """Причина склейки — у каждого вызова своя (err_box), без гонки."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yd-concat-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_empty_list_reports_reason(self):
        err = []
        self.assertFalse(seg_dl.concat_copy([], os.path.join(self.tmp, "o.mp4"),
                                            err_box=err))
        self.assertTrue(err and "нет сегментов" in err[0])

    def test_non_ts_segment_reports_reason(self):
        p = os.path.join(self.tmp, "seg-00000.ts")
        with open(p, "wb") as f:
            f.write(b"<html>not a segment</html>")
        err = []
        self.assertFalse(seg_dl.concat_copy([p], os.path.join(self.tmp, "o.mp4"),
                                            err_box=err))
        self.assertTrue(err and "MPEG-TS" in err[0])


class StopAndPoolTests(unittest.TestCase):
    """Остановка проверяется до сети; пул httpx рассчитан на все серии."""

    def test_download_one_stops_before_network(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(seg_dl.Stopped):
            seg_dl.download_one("https://example.invalid/seg.ts", is_stopped=ev)

    def test_httpx_pool_covers_parallel_episodes(self):
        class FakeLimits:
            def __init__(self, max_connections, max_keepalive_connections):
                self.max_connections = max_connections

        class FakeClient:
            def __init__(self, http2, **kw):
                self.limits = kw.get("limits")

        class FakeHttpx:
            Limits = FakeLimits
            Client = FakeClient

        client, _ = seg_dl._build_httpx_client(FakeHttpx, 8)
        self.assertGreaterEqual(client.limits.max_connections,
                                seg_dl._HTTPX_TOTAL_CONN)


class CliFlagTests(unittest.TestCase):
    """Флаги CLI разбираются так же, как раньше."""

    def test_int_and_str_flags(self):
        import yummy_dl
        workers, rest = yummy_dl._pop_int_flag(
            ["--workers", "6", "https://x"], "--workers")
        self.assertEqual((workers, rest), (6, ["https://x"]))
        backend, rest = yummy_dl._pop_str_flag(
            ["--backend", "httpx", "--segs", "0", "https://x"], "--backend")
        self.assertEqual((backend, rest), ("httpx", ["--segs", "0", "https://x"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
