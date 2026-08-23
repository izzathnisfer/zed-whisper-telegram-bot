import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("USAGE_DB_PATH", "/tmp/zed-whisper-bot-tests.sqlite3")

import bot  # noqa: E402  (environment must be configured before this import)


class BotHelperTests(unittest.TestCase):
    def test_application_registers_expected_commands(self):
        application = bot.build_application()
        commands = {
            command
            for handler in application.handlers[0]
            if hasattr(handler, "commands")
            for command in handler.commands
        }

        self.assertEqual(commands, {"start", "usage", "usage_detailed"})

    def test_progress_bar_and_duration_format(self):
        self.assertEqual(bot.progress_bar(3_600, 7_200), "██████░░░░░░")
        self.assertEqual(bot.format_duration(7_200), "2h 0m")
        self.assertEqual(bot.format_duration(305), "5m 5s")

    def test_http_client_does_not_log_token_bearing_urls(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg and FFprobe are required",
    )
    def test_media_is_split_into_ordered_chunks(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.ogg"
                chunks_directory = Path(directory) / "chunks"
                chunks_directory.mkdir()
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=5",
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "12k",
                        str(source),
                    ],
                    check=True,
                )

                original_chunk_seconds = bot.CHUNK_SECONDS
                bot.CHUNK_SECONDS = 2
                try:
                    chunks = await bot.split_media(str(source), str(chunks_directory))
                finally:
                    bot.CHUNK_SECONDS = original_chunk_seconds

                self.assertEqual([Path(path).name for path in chunks], [
                    "chunk_0000.mp3",
                    "chunk_0001.mp3",
                    "chunk_0002.mp3",
                ])
                for chunk in chunks:
                    codec = subprocess.run(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "a:0",
                            "-show_entries",
                            "stream=codec_name,channels,sample_rate",
                            "-of",
                            "default=noprint_wrappers=1",
                            chunk,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    self.assertIn("codec_name=mp3", codec)
                    self.assertIn("sample_rate=16000", codec)
                    self.assertIn("channels=1", codec)

        asyncio.run(run_test())

    def test_cleanup_immediately_unlinks_media(self):
        async def run_test():
            with tempfile.NamedTemporaryFile(delete=False) as media_file:
                path = media_file.name

            deleted = await bot.cleanup_file(path, "test media")

            self.assertTrue(deleted)
            self.assertFalse(Path(path).exists())

        asyncio.run(run_test())

    def test_local_cache_path_must_stay_inside_private_data_directory(self):
        original_mode = bot.TELEGRAM_LOCAL_MODE
        original_directory = bot.TELEGRAM_LOCAL_DATA_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                bot.TELEGRAM_LOCAL_MODE = True
                bot.TELEGRAM_LOCAL_DATA_DIR = Path(directory).resolve()
                approved = Path(directory) / "bot-cache" / "audio.mp3"

                self.assertEqual(
                    bot.get_local_cache_path(str(approved)),
                    str(approved.resolve()),
                )
                self.assertIsNone(bot.get_local_cache_path("/tmp/outside.mp3"))
                self.assertIsNone(bot.get_local_cache_path("relative.mp3"))
        finally:
            bot.TELEGRAM_LOCAL_MODE = original_mode
            bot.TELEGRAM_LOCAL_DATA_DIR = original_directory

    def test_application_processes_updates_concurrently(self):
        application = bot.build_application()

        self.assertEqual(
            application.update_processor.max_concurrent_updates,
            bot.UPDATE_CONCURRENCY,
        )

    def test_every_explicit_groq_retry_is_atomically_counted(self):
        class TemporaryGroqError(Exception):
            status_code = 500

        async def run_test():
            groq_results = [TemporaryGroqError(), SimpleNamespace(text="finished")]

            async def fake_groq_transcription(_filename, _audio_bytes):
                result = groq_results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

            with tempfile.TemporaryDirectory() as directory:
                tracker = bot.UsageTracker(str(Path(directory) / "usage.sqlite3"))
                source = str(Path(directory) / "test.mp3")
                Path(source).write_bytes(b"test audio")
                original_tracker = bot.usage_tracker
                bot.usage_tracker = tracker
                try:
                    with (
                        patch.object(
                            bot,
                            "_groq_transcription",
                            new=fake_groq_transcription,
                        ),
                        patch.object(bot, "groq_retry_delay", return_value=0.0),
                    ):
                        result = await asyncio.wait_for(
                            bot.transcribe_file(source, 30), timeout=5
                        )
                finally:
                    bot.usage_tracker = original_tracker

                snapshot = tracker.snapshot()
                self.assertEqual(result, "finished")
                self.assertEqual(snapshot.requests_minute.used, 2)
                self.assertEqual(snapshot.audio_hour.used, 60)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
