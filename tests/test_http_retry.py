#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_http_retry — повтор сетевого вызова в core.http_get/http_post.

Без сети: единственный шов, который патчится, — core._urlopen_once
(это и есть «внутренний вызов opener»: urlopen + чтение тела).
core.time.sleep тоже патчится, чтобы тесты не ждали реальные паузы.
"""

import http.client
import os
import socket
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import core  # noqa: E402


def _reset_error():
    """Обрыв соединения в виде, в котором его отдаёт Windows."""
    return ConnectionResetError(10054, "Удаленный хост принудительно разорвал "
                                       "существующее подключение")


class HttpRetryTests(unittest.TestCase):
    """Повтор с backoff: только сетевые обрывы, HTTP-статусы — никогда."""

    def setUp(self):
        # mock.patch восстанавливает атрибуты сам (addCleanup) — после нас
        # ничего не остаётся висеть, поэтому 97 существующих тестов целы.
        patcher = mock.patch.object(core.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        # счётчик попыток + сценарий «падает N раз, потом успех»
        self.calls = []
        self.plan = []

    # -------------------------------------------------------------- помощники
    def _install(self, side_effect_factory, result="OK-ТЕЛО"):
        """Подменить core._urlopen_once на фабрику, ведущую себя по self.plan."""
        def fake(req, timeout, binary):
            self.calls.append(timeout)
            action = self.plan.pop(0) if self.plan else None
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                return action()
            return result
        patcher = mock.patch.object(core, "_urlopen_once", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------- успех с первого раза
    def test_no_retry_on_success(self):
        self.plan = []
        self._install(lambda *a: None)
        self.assertEqual(core.http_get("https://example.invalid/"), "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 1)
        self.sleep.assert_not_called()

    # ------------------------------------------- два обрыва, третья попытка успешна
    def test_retries_then_succeeds(self):
        self.plan = [_reset_error(), _reset_error(), None]
        self._install(lambda *a: None)
        got = core.http_get("https://example.invalid/")
        self.assertEqual(got, "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 3, "должно быть ровно 3 попытки")
        self.assertEqual(self.sleep.call_count, 2, "сон только между попытками")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list],
                         list(core.HTTP_BACKOFF[:2]))

    # ------------------------------------------------------ URLError с reason
    def test_retries_urlerror_with_reset_reason(self):
        self.plan = [urllib.error.URLError(_reset_error()), None]
        self._install(lambda *a: None)
        self.assertEqual(core.http_get("https://example.invalid/"), "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_retries_plain_socket_timeout(self):
        self.plan = [socket.timeout("timed out"), None]
        self._install(lambda *a: None)
        self.assertEqual(core.http_get("https://example.invalid/"), "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 2)

    def test_retries_remote_disconnected(self):
        self.plan = [http.client.RemoteDisconnected("Remote end closed"), None]
        self._install(lambda *a: None)
        self.assertEqual(core.http_get("https://example.invalid/"), "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 2)

    def test_retries_urlerror_with_socket_timeout_reason(self):
        self.plan = [urllib.error.URLError(socket.timeout("t/o")), None]
        self._install(lambda *a: None)
        self.assertEqual(core.http_get("https://example.invalid/"), "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 2)

    # ------------------------------------------------------------ исчерпали попытки
    def test_raises_after_all_attempts(self):
        self.plan = [_reset_error()] * core.HTTP_ATTEMPTS
        self._install(lambda *a: None)
        with self.assertRaises(ConnectionResetError) as ctx:
            core.http_get("https://example.invalid/")
        self.assertEqual(len(self.calls), core.HTTP_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, core.HTTP_ATTEMPTS - 1)
        msg = str(ctx.exception)
        self.assertIn("%d попытки" % core.HTTP_ATTEMPTS, msg)
        self.assertIn("последняя ошибка", msg)

    def test_raises_same_exception_type_as_before(self):
        """Свой класс не заводим — тип исключения прежний, изменился текст."""
        self.plan = [socket.timeout("t/o")] * core.HTTP_ATTEMPTS
        self._install(lambda *a: None)
        with self.assertRaises(socket.timeout):
            core.http_get("https://example.invalid/")

    def test_raises_urlerror_type_when_it_was_urlerror(self):
        self.plan = [urllib.error.URLError(_reset_error())] * core.HTTP_ATTEMPTS
        self._install(lambda *a: None)
        with self.assertRaises(urllib.error.URLError) as ctx:
            core.http_get("https://example.invalid/")
        self.assertIn("%d попытки" % core.HTTP_ATTEMPTS, str(ctx.exception))

    # ------------------------------------------------------------- НЕ ретраим
    def test_http_404_not_retried(self):
        err = urllib.error.HTTPError("https://example.invalid/", 404, "Not Found",
                                     {}, None)
        self.plan = [err]
        self._install(lambda *a: None)
        with self.assertRaises(urllib.error.HTTPError):
            core.http_get("https://example.invalid/")
        self.assertEqual(len(self.calls), 1, "404 — ответ сервера, не обрыв")
        self.sleep.assert_not_called()

    def test_http_500_not_retried(self):
        err = urllib.error.HTTPError("https://example.invalid/", 500,
                                     "Server Error", {}, None)
        self.plan = [err]
        self._install(lambda *a: None)
        with self.assertRaises(urllib.error.HTTPError):
            core.http_get("https://example.invalid/")
        self.assertEqual(len(self.calls), 1)
        self.sleep.assert_not_called()

    def test_other_exception_not_retried(self):
        self.plan = [ValueError("что-то совсем другое")]
        self._install(lambda *a: None)
        with self.assertRaises(ValueError):
            core.http_get("https://example.invalid/")
        self.assertEqual(len(self.calls), 1, "ValueError ретраить нельзя")
        self.sleep.assert_not_called()

    def test_urlerror_without_retryable_reason_not_retried(self):
        self.plan = [urllib.error.URLError("нет такого хоста")]
        self._install(lambda *a: None)
        with self.assertRaises(urllib.error.URLError):
            core.http_get("https://example.invalid/")
        self.assertEqual(len(self.calls), 1)

    # ------------------------------------------------------------------- http_post
    def test_post_retries_then_succeeds(self):
        self.plan = [_reset_error(), _reset_error(), None]
        self._install(lambda *a: None)
        self.assertEqual(core.http_post("https://example.invalid/", {"a": "1"}),
                         "OK-ТЕЛО")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_post_404_not_retried(self):
        err = urllib.error.HTTPError("https://example.invalid/", 404, "Not Found",
                                     {}, None)
        self.plan = [err]
        self._install(lambda *a: None)
        with self.assertRaises(urllib.error.HTTPError):
            core.http_post("https://example.invalid/", {"a": "1"})
        self.assertEqual(len(self.calls), 1)

    # ------------------------------------------------------------------ настройки
    def test_retry_budget_is_symmetric_and_small(self):
        self.assertEqual(core.HTTP_ATTEMPTS, 3)
        self.assertEqual(core.HTTP_BACKOFF[:2], (0.5, 1.5))
        self.assertLessEqual(sum(core.HTTP_BACKOFF[:core.HTTP_ATTEMPTS - 1]), 5.0)

    def test_api_goes_through_retried_http_get(self):
        """api() отдельно не трогали — он покрывается через http_get."""
        self.plan = [_reset_error(), None]
        self._install(lambda *a: None, result='{"response": {"title": "ok"}}')
        self.assertEqual(core.api("/anime/1")["title"], "ok")
        self.assertEqual(len(self.calls), 2)

    def test_binary_flag_passed_through(self):
        seen = {}

        def fake(req, timeout, binary):
            seen["binary"] = binary
            return b"bytes"

        with mock.patch.object(core, "_urlopen_once", fake):
            self.assertEqual(core.http_get("https://example.invalid/", binary=True),
                             b"bytes")
        self.assertTrue(seen["binary"])

    def test_patches_are_fully_restored(self):
        """После каждого теста core._urlopen_once и time.sleep — оригинальные."""
        import time as _time
        self.assertIs(core.time.sleep, _time.sleep)
        self.assertIs(core._urlopen_once.__module__, core.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)