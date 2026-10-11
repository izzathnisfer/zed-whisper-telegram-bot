import os
import unittest
from datetime import datetime
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

    def test_transcription_filename_preserves_uploaded_filename(self):
        media = SimpleNamespace(file_name="Project Update.mp3")
        self.assertEqual(
            bot.transcription_filename(media, ".mp3", "audio file"),
            "Project Update.mp3.txt",
        )

    def test_transcription_filename_uses_safe_basename(self):
        media = SimpleNamespace(file_name="folder/meeting.m4a")
        self.assertEqual(
            bot.transcription_filename(media, ".m4a", "audio file"),
            "meeting.m4a.txt",
        )

    def test_voice_note_transcription_filename_has_clear_fallback(self):
        media = SimpleNamespace()
        self.assertEqual(
            bot.transcription_filename(
                media,
                ".ogg",
                "voice note",
                now=datetime(2026, 9, 20, 12, 34, 56),
            ),
            "voice_note_20260920_123456.ogg.txt",
        )

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


    def test_default_language_is_auto(self):
        with patch.object(bot, "LANGUAGE_STATE_PATH", "/tmp/non-existent-lang.txt"):
            self.assertEqual(bot.get_active_language(), "auto")

    def test_language_persistence_and_reset(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_path = os.path.join(tmp_dir, "selected_language.txt")
            with patch.object(bot, "LANGUAGE_STATE_PATH", temp_path):
                bot.set_active_language("ta")
                self.assertEqual(bot.get_active_language(), "ta")
                bot.set_active_language("auto")
                self.assertEqual(bot.get_active_language(), "auto")


    def test_version_and_changelog_retrieval(self):
        self.assertEqual(bot.BOT_VERSION, "2.0.1")
        self.assertEqual(bot.__version__, "2.0.1")
        changelog = bot.get_version_changelog("2.0.1")
        self.assertIn("language", changelog.lower())
        self.assertIn("version", changelog.lower())


if __name__ == "__main__":
    unittest.main()
