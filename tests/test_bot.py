import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


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
                    "chunk_0000.flac",
                    "chunk_0001.flac",
                    "chunk_0002.flac",
                ])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
