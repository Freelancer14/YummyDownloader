#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_http_backend — выбор HTTP-бэкенда seg_dl и httpx-путь.

Без сети: подменяется сам httpx-клиент (core._urlopen_once не при чём — тут
сегменты). Старая цепочка urllib -> curl не трогается тестами вообще.
"""

import hashlib
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import seg_dl  # noqa: E402
import yummy_dl  # noqa: E402


# ------------------------------------------------------- подставной httpx
class FakeResponse:
    def __init__(self, content=b"", chunks=(), status=200):
        self.content = content
        self._chunks = list(chunks) or [content]
        self.status_code = status
        self.http_version = "HTTP/1.1"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


class FakeStreamCtx:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self.resp

    def __exit__(self, *a):
        return False


class FakeClient:
    """Минимальный httpx.Client: get() и stream(), запоминает аргументы."""

    def __init__(self, payload=b"payload", chunks=(), status=200, http2=False):
        self.payload = payload
        self.chunks = chunks
        self.status = status
        self.kwargs = {}
        self.gets = []
        self.streams = []

    def _resp(self):
        return FakeResponse(self.payload, self.chunks, self.status)

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return self._resp()

    def stream(self, method, url, headers=None, timeout=None):
        self.streams.append({"method": method, "url": url,
                             "headers": headers, "timeout": timeout})
        return FakeStreamCtx(self._resp())

    def close(self):
        pass


class FakeIterBytes:
    """resp.iter_bytes(chunk) -> итератор кусков заданного размера."""

    def __init__(self, resp):
        self.resp = resp

    def __call__(self, chunk):
        data = self.resp.content
        return [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]


def make_resp(content, chunks=(), status=200, http_version="HTTP/1.1"):
    r = FakeResponse(content, chunks, status)
    r.http_version = http_version
    r.iter_bytes = FakeIterBytes(r)
    return r


class BackendSelectionTests(unittest.TestCase):
    """auto / httpx / urllib — как выбирается и что при отсутствии пакета."""

    def setUp(self):
        seg_dl.set_backend(None)      # сброс override от прошлого теста
        self.addCleanup(seg_dl.set_backend, None)
        self.addCleanup(seg_dl._reset_httpx_client)

    def test_auto_picks_httpx_when_installed(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: object()):
            self.assertEqual(seg_dl.effective_backend(), "httpx")

    def test_auto_falls_back_to_urllib_when_no_httpx(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: None):
            self.assertEqual(seg_dl.effective_backend(), "urllib")

    def test_explicit_urllib_ignores_installed_httpx(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: object()):
            self.assertEqual(seg_dl.set_backend("urllib"), "urllib")
            self.assertEqual(seg_dl.effective_backend(), "urllib")

    def test_explicit_httpx_without_package_raises_readable_error(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: None):
            with self.assertRaises(seg_dl.SegError) as ctx:
                seg_dl.set_backend("httpx")
            msg = str(ctx.exception)
            self.assertIn("httpx", msg)
            self.assertIn("pip install", msg)   # должно быть понятно, что делать

    def test_explicit_httpx_with_package_ok(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: object()):
            self.assertEqual(seg_dl.set_backend("httpx"), "httpx")

    def test_unknown_backend_rejected(self):
        with self.assertRaises(seg_dl.SegError) as ctx:
            seg_dl.set_backend("wget")
        self.assertIn("auto|httpx|urllib", str(ctx.exception))

    def test_backend_read_from_core_cfg(self):
        import core
        saved = core.CFG.get("http_backend")
        try:
            core.CFG["http_backend"] = "urllib"
            self.assertEqual(seg_dl.effective_backend(), "urllib")
            core.CFG["http_backend"] = "auto"
            with mock.patch.object(seg_dl, "_import_httpx", lambda: None):
                self.assertEqual(seg_dl.effective_backend(), "urllib")
        finally:
            if saved is None:
                core.CFG.pop("http_backend", None)
            else:
                core.CFG["http_backend"] = saved

    def test_garbage_cfg_value_falls_back_to_auto(self):
        import core
        saved = core.CFG.get("http_backend")
        try:
            core.CFG["http_backend"] = "какая-то дичь"
            with mock.patch.object(seg_dl, "_import_httpx", lambda: object()):
                self.assertEqual(seg_dl.effective_backend(), "httpx")
        finally:
            if saved is None:
                core.CFG.pop("http_backend", None)
            else:
                core.CFG["http_backend"] = saved

    def test_httpx_available_reflects_package(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: object()):
            self.assertTrue(seg_dl.httpx_available())
        with mock.patch.object(seg_dl, "_import_httpx", lambda: None):
            self.assertFalse(seg_dl.httpx_available())

    def test_backend_is_reverted_after_test(self):
        self.assertIsNone(seg_dl.get_backend_override())


class HttpxPathTests(unittest.TestCase):
    """Запросы через httpx-клиент: заголовки, байты, redirect'ы, стриминг."""

    def setUp(self):
        seg_dl.set_backend("httpx")
        seg_dl._reset_httpx_client()
        self.addCleanup(seg_dl.set_backend, None)
        self.addCleanup(seg_dl._reset_httpx_client)
        self.payload = b"\x47" + os.urandom(4096) + b"\x47"
        self.client = FakeClient(payload=self.payload)
        patcher = mock.patch.object(seg_dl, "get_httpx_client",
                                    lambda max_conn=None: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetch_bytes_returns_payload(self):
        got = seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        self.assertEqual(got, self.payload)
        self.assertEqual(len(self.client.gets), 1)

    def test_fetch_bytes_sends_ua_and_referer(self):
        seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        hdrs = self.client.gets[0]["headers"]
        self.assertEqual(hdrs["User-Agent"], seg_dl.UA)
        self.assertEqual(hdrs["Referer"], seg_dl.REF)

    def test_fetch_bytes_honours_explicit_referer(self):
        seg_dl.fetch_bytes("https://cdn.example/seg.ts", ref="https://kodik/x")
        self.assertEqual(self.client.gets[0]["headers"]["Referer"],
                         "https://kodik/x")

    def test_fetch_bytes_wraps_errors_into_segerror(self):
        self.client.status = 403
        with self.assertRaises(seg_dl.SegError) as ctx:
            seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        self.assertIn("httpx", str(ctx.exception))

    def test_fetch_bytes_http_error_mentions_code(self):
        self.client.status = 404
        with self.assertRaises(seg_dl.SegError) as ctx:
            seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        self.assertIn("404", str(ctx.exception))

    def test_streaming_writes_file_matching_source_hash(self):
        tmp = os.path.join(os.environ.get("TEMP", "."), "seg_dl_stream_test.ts")
        self.addCleanup(lambda: os.path.exists(tmp) and os.remove(tmp))
        try:
            os.remove(tmp)
        except OSError:
            pass
        self.client._resp = lambda: make_resp(self.payload)
        size = seg_dl._stream_to_file_httpx("https://cdn.example/seg.ts", tmp)
        self.assertEqual(size, len(self.payload))
        with open(tmp, "rb") as f:
            on_disk = f.read()
        self.assertEqual(hashlib.sha256(on_disk).hexdigest(),
                         hashlib.sha256(self.payload).hexdigest(),
                         "записанный файл должен совпасть с исходными байтами")
        self.assertEqual(len(self.client.streams), 1)

    def test_streaming_uses_chunks_not_one_blob(self):
        tmp = os.path.join(os.environ.get("TEMP", "."), "seg_dl_chunk_test.ts")
        self.addCleanup(lambda: os.path.exists(tmp) and os.remove(tmp))
        seen_chunks = []
        resp = make_resp(self.payload)
        real_iter = resp.iter_bytes

        def spy(chunk):
            seen_chunks.append(chunk)
            return real_iter(chunk)

        resp.iter_bytes = spy
        self.client._resp = lambda: resp
        seg_dl._stream_to_file_httpx("https://cdn.example/seg.ts", tmp)
        self.assertEqual(seen_chunks, [seg_dl._HTTPX_CHUNK])

    def test_download_one_streams_to_out_path(self):
        tmp = os.path.join(os.environ.get("TEMP", "."), "seg_dl_do_test.ts")
        self.addCleanup(lambda: os.path.exists(tmp) and os.remove(tmp))
        try:
            os.remove(tmp)
        except OSError:
            pass
        self.client._resp = lambda: make_resp(self.payload)
        got = seg_dl.download_one("https://cdn.example/seg.ts", out_path=tmp)
        self.assertEqual(got, len(self.payload))   # вернулся размер, не байты
        with open(tmp, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(len(self.client.streams), 1)
        self.assertEqual(len(self.client.gets), 0, "в память не буферизовали")

    def test_download_one_without_out_path_returns_bytes(self):
        self.client._resp = lambda: make_resp(self.payload)
        got = seg_dl.download_one("https://cdn.example/seg.ts")
        self.assertEqual(got, self.payload)
        self.assertEqual(len(self.client.gets), 1)

    def test_download_one_keeps_retries(self):
        """Логика 3 попыток на httpx-пути не сломана."""
        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("connection reset")

        with mock.patch.object(seg_dl, "_fetch_bytes_httpx", boom), \
                mock.patch.object(seg_dl.time, "sleep"):
            with self.assertRaises(seg_dl.SegError):
                seg_dl.download_one("https://cdn.example/seg.ts", retries=3)
        self.assertEqual(calls["n"], 3)

    def test_download_one_retries_then_succeeds(self):
        calls = {"n": 0}
        real = seg_dl._fetch_bytes_httpx

        def flaky(url, ref=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("reset")
            return b"ok"

        with mock.patch.object(seg_dl, "_fetch_bytes_httpx", flaky), \
                mock.patch.object(seg_dl.time, "sleep"):
            self.assertEqual(
                seg_dl.download_one("https://cdn.example/seg.ts", retries=3),
                b"ok")
        self.assertEqual(calls["n"], 2)


class HttpxClientBuildTests(unittest.TestCase):
    """Постройка клиента: redirect'ы, лимиты, деградация h2 -> 1.1.

    Отдельный класс: здесь get_httpx_client НЕ замокан — мы проверяем именно его.
    """

    def setUp(self):
        seg_dl._reset_httpx_client()
        self.addCleanup(seg_dl._reset_httpx_client)

    def test_client_is_built_with_redirects_and_limits(self):
        """Клиент поднимается с follow_redirects и лимитом >= числа потоков."""
        seen = {}

        class FakeModule:
            class Limits:
                def __init__(self, max_connections=None,
                             max_keepalive_connections=None):
                    seen["max_connections"] = max_connections
                    seen["max_keepalive"] = max_keepalive_connections

            @staticmethod
            def Client(**kwargs):
                seen.update(kwargs)
                return FakeClient()

        with mock.patch.object(seg_dl, "_import_httpx", lambda: FakeModule):
            client = seg_dl.get_httpx_client(max_conn=8)
        self.assertIsNotNone(client)
        self.assertTrue(seen["follow_redirects"],
                        "302-redirect'ы сегментов должны идти следом")
        self.assertGreaterEqual(seen["max_connections"], 8)
        self.assertGreaterEqual(seen["max_connections"], seg_dl._HTTPX_MIN_CONN)
        self.assertEqual(seen["headers"]["User-Agent"], seg_dl.UA)
        self.assertEqual(seen["headers"]["Referer"], seg_dl.REF)
        self.assertEqual(seen["timeout"], seg_dl._TIMEOUT)

    def test_client_is_reused(self):
        """Один клиент на модуль, а не на каждый сегмент."""
        made = []

        class FakeModule:
            class Limits:
                def __init__(self, **kw):
                    pass

            @staticmethod
            def Client(**kwargs):
                made.append(1)
                return FakeClient()

        with mock.patch.object(seg_dl, "_import_httpx", lambda: FakeModule):
            a = seg_dl.get_httpx_client(max_conn=4)
            b = seg_dl.get_httpx_client(max_conn=4)
            c = seg_dl.get_httpx_client(max_conn=4)
        self.assertIs(a, b)
        self.assertIs(b, c)
        self.assertEqual(len(made), 1, "клиент должен создаваться один раз")

    def test_client_grows_when_more_threads_asked(self):
        """Лимит соединений не меньше числа потоков: клиент пересоздаётся."""
        seen = []

        class FakeModule:
            class Limits:
                def __init__(self, max_connections=None, **kw):
                    seen.append(max_connections)

            @staticmethod
            def Client(**kwargs):
                return FakeClient()

        with mock.patch.object(seg_dl, "_import_httpx", lambda: FakeModule):
            seg_dl.get_httpx_client(max_conn=4)
            seg_dl.get_httpx_client(max_conn=4)     # без роста
            seg_dl.get_httpx_client(max_conn=32)    # вырос
        self.assertEqual(seen, [max(4, seg_dl._HTTPX_MIN_CONN),
                               max(32, seg_dl._HTTPX_MIN_CONN)])

    def test_client_http2_downgrades_when_h2_missing(self):
        """Без пакета h2 клиент должен подняться на HTTP/1.1, а не упасть."""
        calls = []

        class NoH2Module:
            class Limits:
                def __init__(self, **kw):
                    pass

            @staticmethod
            def Client(**kwargs):
                calls.append(kwargs.get("http2"))
                if kwargs.get("http2"):
                    raise ImportError("Using http2=True, but the 'h2' package "
                                      "is not installed.")
                return FakeClient()

        with mock.patch.object(seg_dl, "_import_httpx", lambda: NoH2Module):
            seg_dl.get_httpx_client(max_conn=4)
        self.assertEqual(calls, [True, False])       # сначала h2, потом 1.1
        self.assertFalse(seg_dl.httpx_http2_enabled())

    def test_client_http2_flagged_when_h2_present(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: object()), \
                mock.patch.object(seg_dl, "_build_httpx_client",
                                  lambda m, c: (FakeClient(), True)):
            seg_dl.get_httpx_client(max_conn=4)
        self.assertTrue(seg_dl.httpx_http2_enabled())

    def test_missing_package_raises_clear_segerror(self):
        with mock.patch.object(seg_dl, "_import_httpx", lambda: None):
            with self.assertRaises(seg_dl.SegError) as ctx:
                seg_dl.get_httpx_client(max_conn=4)
        self.assertIn("httpx", str(ctx.exception))
        self.assertIn("pip install", str(ctx.exception))


class UrllibPathUnchangedTests(unittest.TestCase):
    """Старая цепочка urllib -> curl не должна поехать от нового кода."""

    def setUp(self):
        seg_dl.set_backend("urllib")
        self.addCleanup(seg_dl.set_backend, None)

    def test_urllib_backend_never_touches_httpx(self):
        with mock.patch.object(seg_dl, "_fetch_bytes_httpx") as httpx_call, \
                mock.patch.object(seg_dl, "_stream_to_file_httpx") as stream_call, \
                mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.read.return_value = b"bytes"
            got = seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        self.assertEqual(got, b"bytes")
        httpx_call.assert_not_called()
        stream_call.assert_not_called()

    def test_download_one_urllib_path_untouched(self):
        with mock.patch.object(seg_dl, "_fetch_bytes_httpx") as httpx_call, \
                mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.read.return_value = b"seg"
            got = seg_dl.download_one("https://cdn.example/seg.ts")
        self.assertEqual(got, b"seg")
        httpx_call.assert_not_called()

    def test_urllib_still_falls_back_to_curl(self):
        """Поведение, ради которого весь хак и писался, осталось."""
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("SSLEOF")), \
                mock.patch.object(seg_dl, "_curl_bin", lambda: "curl.exe"), \
                mock.patch.object(seg_dl.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"curl-bytes",
                                         stderr=b"")
            got = seg_dl.fetch_bytes("https://cdn.example/seg.ts")
        self.assertEqual(got, b"curl-bytes")


class CliFlagTests(unittest.TestCase):
    """--backend разбирается так же, как --workers/--segs."""

    def test_pop_str_flag_extracts_value(self):
        val, rest = yummy_dl._pop_str_flag(
            ["--backend", "httpx", "https://x"], "--backend")
        self.assertEqual(val, "httpx")
        self.assertEqual(rest, ["https://x"])

    def test_pop_str_flag_without_value(self):
        val, rest = yummy_dl._pop_str_flag(["https://x"], "--backend")
        self.assertIsNone(val)
        self.assertEqual(rest, ["https://x"])

    def test_pop_str_flag_does_not_eat_other_flags(self):
        val, rest = yummy_dl._pop_str_flag(
            ["--workers", "4", "https://x"], "--backend")
        self.assertIsNone(val)
        self.assertEqual(rest, ["--workers", "4", "https://x"])

    def test_pop_str_flag_last_wins(self):
        val, rest = yummy_dl._pop_str_flag(
            ["--backend", "urllib", "--backend", "httpx", "u"], "--backend")
        self.assertEqual(val, "httpx")
        self.assertEqual(rest, ["u"])

    def test_pop_str_flag_keeps_dash_value_out(self):
        val, rest = yummy_dl._pop_str_flag(["--backend", "--no-subs", "u"],
                                           "--backend")
        self.assertIsNone(val)
        self.assertEqual(rest, ["--backend", "--no-subs", "u"])

    def test_pop_int_flag_still_works(self):
        val, rest = yummy_dl._pop_int_flag(
            ["--workers", "4", "--segs", "0", "u"], "--workers")
        self.assertEqual(val, 4)
        self.assertEqual(rest, ["--segs", "0", "u"])

    def test_cli_passes_backend_to_seg_dl(self):
        """Значение флага реально доходит до seg_dl.set_backend."""
        with mock.patch.object(seg_dl, "set_backend", return_value="httpx") as sb:
            val, _ = yummy_dl._pop_str_flag(["--backend", "httpx", "u"],
                                            "--backend")
            seg_dl.set_backend(val)
        sb.assert_called_once_with("httpx")


if __name__ == "__main__":
    unittest.main(verbosity=2)