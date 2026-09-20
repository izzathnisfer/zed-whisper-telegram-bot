import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("USAGE_DB_PATH", "/tmp/zed-whisper-core-tests.sqlite3")

import bot  # noqa: E402


class CoreBehaviorTests(unittest.TestCase):
    def test_authorization_restricts_to_configured_owner(self):
        owner = SimpleNamespace(effective_user=SimpleNamespace(id=42))
        stranger = SimpleNamespace(effective_user=SimpleNamespace(id=99))
        with patch.object(bot, "ALLOWED_USER_ID", "42"):
            self.assertTrue(bot.is_authorized(owner))
            self.assertFalse(bot.is_authorized(stranger))

    def test_authorization_is_open_only_when_owner_is_not_configured(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=99))
        with patch.object(bot, "ALLOWED_USER_ID", None):
            self.assertTrue(bot.is_authorized(update))

    def test_media_details_preserve_audio_extension(self):
        audio = SimpleNamespace(file_name="meeting.m4a")
        message = SimpleNamespace(
            voice=None, audio=audio, video=None, video_note=None
        )
        media, suffix, label = bot.get_media_details(message)
        self.assertIs(media, audio)
        self.assertEqual(suffix, ".m4a")
        self.assertEqual(label, "audio file")

    def test_unknown_media_is_rejected(self):
        message = SimpleNamespace(
            voice=None, audio=None, video=None, video_note=None
        )
        with self.assertRaises(ValueError):
            bot.get_media_details(message)

    def test_non_transient_groq_error_is_not_retried(self):
        error = SimpleNamespace(status_code=400)
        self.assertIsNone(bot.groq_retry_delay(error, 1))

    def test_retry_after_is_bounded(self):
        class RateLimitError(Exception):
            status_code = 429
            response = SimpleNamespace(headers={"retry-after": "9999"})

        delay = bot.groq_retry_delay(RateLimitError(), 1)
        self.assertEqual(delay, bot.MAX_AUTOMATIC_LIMIT_WAIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
