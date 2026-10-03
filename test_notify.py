import io
import os
import unittest
from unittest.mock import AsyncMock, patch

import notify


class SanitizeTests(unittest.TestCase):
    def test_strips_ansi_and_control_characters(self):
        raw = "\x1b[31mred\x1b[0m \x1b[1mbold\x1b[0m\r\nsecond\x07 line\x00"
        self.assertEqual(notify.sanitize(raw), "red bold\nsecond line")

    def test_keeps_newlines_and_tabs(self):
        self.assertEqual(notify.sanitize("a\n\tb"), "a\n\tb")


class ValidateTests(unittest.TestCase):
    def test_empty_rejected(self):
        ok, reason = notify.validate("")
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_whitespace_rejected(self):
        ok, reason = notify.validate("  \t\n\r  ")
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_escape_only_rejected(self):
        ok, reason = notify.validate("\x1b[31m\x1b[0m   \x00")
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_oversized_rejected(self):
        ok, reason = notify.validate("a" * 4001)
        self.assertFalse(ok)
        self.assertIn("limit", reason)

    def test_limit_boundary_accepted(self):
        ok, message = notify.validate("a" * 4000)
        self.assertTrue(ok)
        self.assertEqual(len(message), 4000)

    def test_control_characters_do_not_inflate_length(self):
        ok, message = notify.validate("\x1b[31m" + "a" * 4000 + "\x1b[0m")
        self.assertTrue(ok)
        self.assertEqual(message, "a" * 4000)

    def test_plain_message_accepted(self):
        ok, message = notify.validate("  hello world  ")
        self.assertTrue(ok)
        self.assertEqual(message, "hello world")


class ReadMessageTests(unittest.TestCase):
    def test_positional_argument(self):
        ok, message = notify.read_message(["hello"], io.StringIO("ignored"))
        self.assertTrue(ok)
        self.assertEqual(message, "hello")

    def test_stdin_dash(self):
        ok, message = notify.read_message(["-"], io.StringIO("from stdin\n"))
        self.assertTrue(ok)
        self.assertEqual(message, "from stdin")

    def test_stdin_empty_rejected(self):
        ok, reason = notify.read_message(["-"], io.StringIO(""))
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_missing_argument_reports_usage(self):
        ok, reason = notify.read_message([], io.StringIO(""))
        self.assertFalse(ok)
        self.assertIn("usage", reason)

    def test_extra_arguments_rejected(self):
        ok, reason = notify.read_message(["a", "b"], io.StringIO(""))
        self.assertFalse(ok)
        self.assertIn("usage", reason)


class CredentialTests(unittest.TestCase):
    def test_missing_token(self):
        with patch.dict(os.environ, {"ALLOWED_USER_ID": "123"}, clear=True):
            ok, detail, chat_id = notify.credentials()
        self.assertFalse(ok)
        self.assertIn("TELEGRAM_BOT_TOKEN", detail)
        self.assertIsNone(chat_id)

    def test_missing_chat_id(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t"}, clear=True):
            ok, detail, chat_id = notify.credentials()
        self.assertFalse(ok)
        self.assertIn("ALLOWED_USER_ID", detail)
        self.assertIsNone(chat_id)

    def test_non_decimal_chat_id(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_ID": "12a"}, clear=True):
            ok, detail, chat_id = notify.credentials()
        self.assertFalse(ok)
        self.assertIn("decimal", detail)
        self.assertIsNone(chat_id)

    def test_present_credentials(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_ID": "123"}, clear=True):
            ok, token, chat_id = notify.credentials()
        self.assertTrue(ok)
        self.assertEqual(token, "t")
        self.assertEqual(chat_id, 123)


class MainTests(unittest.TestCase):
    def test_success_sends_once_and_exits_zero(self):
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USER_ID": "123"}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock) as deliver,
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            code = notify.main(["notify.py", "hello"])
        self.assertEqual(code, 0)
        deliver.assert_awaited_once_with("tok", 123, "hello")
        self.assertEqual(out.getvalue().strip(), "notification sent")

    def test_usage_error_exits_two_without_sending(self):
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USER_ID": "123"}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock) as deliver,
        ):
            code = notify.main(["notify.py"])
        self.assertEqual(code, notify.EXIT_USAGE)
        deliver.assert_not_called()

    def test_empty_message_exits_three_without_sending(self):
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USER_ID": "123"}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock) as deliver,
        ):
            code = notify.main(["notify.py", "   "])
        self.assertEqual(code, notify.EXIT_MESSAGE)
        deliver.assert_not_called()

    def test_oversized_message_exits_three_without_sending(self):
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USER_ID": "123"}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock) as deliver,
        ):
            code = notify.main(["notify.py", "x" * 4001])
        self.assertEqual(code, notify.EXIT_MESSAGE)
        deliver.assert_not_called()

    def test_missing_credentials_exit_four_without_sending(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock) as deliver,
        ):
            code = notify.main(["notify.py", "hello"])
        self.assertEqual(code, notify.EXIT_CREDENTIALS)
        deliver.assert_not_called()

    def test_delivery_failure_exits_five(self):
        from telegram.error import TelegramError

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USER_ID": "123"}, clear=True),
            patch.object(notify, "deliver", new_callable=AsyncMock, side_effect=TelegramError("chat not found")),
        ):
            code = notify.main(["notify.py", "hello"])
        self.assertEqual(code, notify.EXIT_SEND)


    def test_redacts_secrets(self):
        self.assertEqual(
            notify.redact("auth zz-secret failed for 998877", ["zz-secret", "998877"]),
            "auth [redacted] failed for [redacted]",
        )

    def test_ignores_empty_secrets(self):
        self.assertEqual(notify.redact("unchanged", ["", None]), "unchanged")


if __name__ == "__main__":
    unittest.main()
