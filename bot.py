import asyncio
import logging
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from dotenv import load_dotenv
from groq import Groq
from telegram import InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from usage_tracker import LimitUsage, UsageTracker


# 1. Setup & Configuration ----------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
GROQ_MODEL = "whisper-large-v3-turbo"

CHUNK_SECONDS = 5 * 60
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20_000_000
MAX_AUTOMATIC_LIMIT_WAIT_SECONDS = 70

PROJECT_DIR = Path(__file__).resolve().parent
USAGE_DB_PATH = os.getenv(
    "USAGE_DB_PATH", str(PROJECT_DIR / "data" / "groq_usage.sqlite3")
)

# These optional settings make the bot ready for a separately installed local
# Telegram Bot API server, which removes Telegram's hosted 20 MB download cap.
TELEGRAM_LOCAL_MODE = os.getenv("TELEGRAM_LOCAL_MODE", "false").lower() in {
    "1",
    "true",
    "yes",
}
TELEGRAM_BASE_URL = os.getenv("TELEGRAM_BASE_URL", "http://127.0.0.1:8081/bot")
TELEGRAM_BASE_FILE_URL = os.getenv(
    "TELEGRAM_BASE_FILE_URL", "http://127.0.0.1:8081/file/bot"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# HTTPX logs Telegram API URLs at INFO level, and those URLs contain the bot
# token. Keep request URLs out of application logs without hiding bot errors.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("Error: Bot token or Groq API key missing in .env")
    raise SystemExit(1)

client = Groq(api_key=GROQ_API_KEY)
usage_tracker = UsageTracker(USAGE_DB_PATH)


# 2. Security & Helper Functions ----------------------------------------------
class UsageLimitUnavailable(Exception):
    def __init__(self, wait_seconds: float):
        super().__init__(f"Groq capacity becomes available in {wait_seconds:.0f}s")
        self.wait_seconds = wait_seconds


def is_authorized(update: Update) -> bool:
    """Check if the sender is the owner of the bot."""
    if not ALLOWED_USER_ID:
        return True
    user_id = update.effective_user.id
    return str(user_id) == str(ALLOWED_USER_ID)


async def cleanup_file(path: Optional[str]) -> None:
    """Securely remove a temporary media file."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info("Deleted temporary file: %s", path)
    except OSError as exc:
        logger.error("Failed to delete file %s: %s", path, exc)


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def progress_bar(used: float, limit: float, width: int = 12) -> str:
    ratio = 0.0 if limit <= 0 else min(1.0, max(0.0, used / limit))
    filled = min(width, int(math.floor(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def reset_text(metric: LimitUsage) -> str:
    if metric.next_expiry_seconds is None:
        return "available now"
    return f"next capacity in {format_duration(metric.next_expiry_seconds)}"


def audio_metric_text(label: str, metric: LimitUsage) -> str:
    return (
        f"{label}\n"
        f"[{progress_bar(metric.used, metric.limit)}] "
        f"{format_duration(metric.used)} / {format_duration(metric.limit)}\n"
        f"Remaining: {format_duration(metric.remaining)} · {reset_text(metric)}"
    )


def request_metric_text(label: str, metric: LimitUsage) -> str:
    used = int(metric.used)
    limit = int(metric.limit)
    return (
        f"{label}\n"
        f"[{progress_bar(used, limit)}] {used:,} / {limit:,}\n"
        f"Remaining: {int(metric.remaining):,} · {reset_text(metric)}"
    )


def get_media_details(message) -> Tuple[object, str, str]:
    if message.voice:
        return message.voice, ".ogg", "voice note"
    if message.audio:
        suffix = Path(message.audio.file_name or "audio.mp3").suffix or ".mp3"
        return message.audio, suffix, "audio file"
    if message.video:
        suffix = Path(message.video.file_name or "video.mp4").suffix or ".mp4"
        return message.video, suffix, "video"
    if message.video_note:
        return message.video_note, ".mp4", "video note"
    raise ValueError("Unknown media type")


async def probe_duration(path: str, fallback: Optional[float] = None) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode == 0:
        try:
            duration = float(stdout.decode().strip())
            if duration > 0:
                return duration
        except ValueError:
            pass
    if fallback and fallback > 0:
        logger.warning("ffprobe failed; using Telegram duration: %s", stderr.decode())
        return float(fallback)
    raise RuntimeError(f"Could not determine media duration: {stderr.decode().strip()}")


async def split_media(input_path: str, output_directory: str) -> list[str]:
    output_pattern = str(Path(output_directory) / "chunk_%04d.flac")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        "-f",
        "segment",
        "-segment_time",
        str(CHUNK_SECONDS),
        "-reset_timestamps",
        "1",
        output_pattern,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg could not split the media: {stderr.decode().strip()}")

    chunks = sorted(str(path) for path in Path(output_directory).glob("chunk_*.flac"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce any audio chunks")
    return chunks


def _groq_transcription(path: str):
    with open(path, "rb") as audio_file:
        return client.audio.transcriptions.create(
            file=(os.path.basename(path), audio_file.read()),
            model=GROQ_MODEL,
            response_format="json",
            language=None,
            temperature=0.0,
        )


async def transcribe_file(
    path: str,
    audio_seconds: float,
    wait_callback: Optional[Callable[[float], Awaitable[None]]] = None,
) -> str:
    """Respect locally tracked limits, record the attempt, then call Groq."""
    while True:
        wait_seconds = await asyncio.to_thread(
            usage_tracker.seconds_until_available, audio_seconds
        )
        if wait_seconds <= 0:
            break
        if wait_seconds > MAX_AUTOMATIC_LIMIT_WAIT_SECONDS:
            raise UsageLimitUnavailable(wait_seconds)
        if wait_callback:
            await wait_callback(wait_seconds)
        await asyncio.sleep(wait_seconds + 0.25)

    await asyncio.to_thread(usage_tracker.record_attempt, audio_seconds)
    transcription = await asyncio.to_thread(_groq_transcription, path)
    return transcription.text or ""


# 3. Bot Command Handlers -----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "👋 I am ready! Send me a voice note, audio file, or video and I will "
            "transcribe it using Groq.\n\nUse /usage for the main hourly allowance "
            "or /usage_detailed for all limits."
        ),
    )


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    snapshot = await asyncio.to_thread(usage_tracker.snapshot)
    await update.message.reply_text(
        "🎧 Groq hourly audio usage\n\n"
        + audio_metric_text("Main limit: 2 hours per rolling hour", snapshot.audio_hour)
        + "\n\nUse /usage_detailed to see all four limits."
    )


async def usage_detailed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    snapshot = await asyncio.to_thread(usage_tracker.snapshot)
    tracking_since = datetime.fromtimestamp(snapshot.tracking_since).astimezone().strftime(
        "%Y-%m-%d %H:%M %Z"
    )
    sections = [
        "📊 Groq free-plan usage",
        audio_metric_text("🎧 Audio / hour (main)", snapshot.audio_hour),
        audio_metric_text("🗓 Audio / day", snapshot.audio_day),
        request_metric_text("⚡ Requests / minute", snapshot.requests_minute),
        request_metric_text("📨 Requests / day", snapshot.requests_day),
        (
            "Rolling windows update automatically as old requests expire.\n"
            f"Local tracking started: {tracking_since}"
        ),
    ]
    await update.message.reply_text("\n\n".join(sections))


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive voice/audio/video, transcribe it, and return the result."""
    if not is_authorized(update):
        logger.warning(
            "Unauthorized access attempt from User ID: %s", update.effective_user.id
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    downloaded_path = None
    progress_message = None

    try:
        media, file_ext, media_label = get_media_details(update.message)
        declared_duration = float(getattr(media, "duration", 0) or 0)
        file_size = int(getattr(media, "file_size", 0) or 0)
        logger.info(
            "Processing %s from %s (duration=%ss, size=%s)",
            media_label,
            update.effective_user.first_name,
            declared_duration,
            file_size,
        )

        if (
            not TELEGRAM_LOCAL_MODE
            and file_size
            and file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES
        ):
            await update.message.reply_text(
                "❌ Telegram will not let this bot download files larger than 20 MB. "
                "Please send it as a compressed audio file/voice note, reduce the video "
                "size, or enable the Local Bot API server."
            )
            return

        if declared_duration > CHUNK_SECONDS:
            progress_message = await update.message.reply_text(
                "⬇️ Downloading long media…"
            )

        new_file = await context.bot.get_file(media.file_id)
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
            downloaded_path = temp_file.name
        await new_file.download_to_drive(downloaded_path)

        duration = await probe_duration(downloaded_path, fallback=declared_duration)

        if duration <= CHUNK_SECONDS:
            text_result = await transcribe_file(downloaded_path, duration)
            if not text_result:
                await update.message.reply_text("⚠️ Could not hear any speech.")
            else:
                # Preserve the original short-media behavior: a copyable message.
                await update.message.reply_text(f"`{text_result}`", parse_mode="Markdown")
            if progress_message:
                await progress_message.delete()
            return

        if progress_message is None:
            progress_message = await update.message.reply_text(
                "✂️ Preparing long media…"
            )
        else:
            await progress_message.edit_text("✂️ Preparing long media…")

        if duration > UsageTracker.AUDIO_SECONDS_PER_HOUR:
            await progress_message.edit_text(
                "❌ This recording is longer than the bot’s two-hour per-job limit. "
                "Please send it as separate recordings no longer than two hours each."
            )
            return

        estimated_parts = math.ceil(duration / CHUNK_SECONDS)

        # Reserve enough room conceptually before beginning so a long job never
        # transcribes only some chunks and then discovers the hourly/daily audio
        # or daily request allowance is exhausted. The per-minute request limit
        # is intentionally handled between chunks with short automatic pauses.
        while True:
            capacity_wait = await asyncio.to_thread(
                usage_tracker.seconds_until_available,
                duration,
                None,
                estimated_parts,
                False,
            )
            if capacity_wait <= 0:
                break
            if capacity_wait > MAX_AUTOMATIC_LIMIT_WAIT_SECONDS:
                raise UsageLimitUnavailable(capacity_wait)
            await progress_message.edit_text(
                "⏸ Waiting for enough hourly Groq capacity to process the complete "
                f"recording: {format_duration(capacity_wait)}\n"
                "The bot will continue automatically."
            )
            await asyncio.sleep(capacity_wait + 0.25)

        with tempfile.TemporaryDirectory(prefix="whisper_chunks_") as chunk_directory:
            await progress_message.edit_text(
                f"✂️ Splitting {format_duration(duration)} into "
                f"{estimated_parts} ordered parts…"
            )
            chunks = await split_media(downloaded_path, chunk_directory)
            transcribed_parts = []

            for index, chunk_path in enumerate(chunks, start=1):
                # Some segmented FLAC containers expose missing or inherited
                # duration metadata. Derive each part from the already-probed
                # source duration so quota accounting stays correct.
                chunk_duration = min(
                    CHUNK_SECONDS,
                    max(1, duration - (index - 1) * CHUNK_SECONDS),
                )

                async def show_wait(wait_seconds: float, part=index) -> None:
                    await progress_message.edit_text(
                        f"⏸ Rate-limit pause before part {part}/{len(chunks)}: "
                        f"{format_duration(wait_seconds)}\n"
                        "The bot will continue automatically."
                    )

                snapshot = await asyncio.to_thread(usage_tracker.snapshot)
                projected = min(
                    snapshot.audio_hour.limit,
                    snapshot.audio_hour.used + max(10.0, chunk_duration),
                )
                await progress_message.edit_text(
                    f"🎙 Transcribing part {index}/{len(chunks)}…\n"
                    f"Completed: {index - 1}/{len(chunks)}\n"
                    f"Hourly audio: [{progress_bar(projected, snapshot.audio_hour.limit)}] "
                    f"{format_duration(projected)} / 2h"
                )
                transcribed_parts.append(
                    await transcribe_file(chunk_path, chunk_duration, show_wait)
                )
                await progress_message.edit_text(
                    f"✅ Transcribed part {index}/{len(chunks)}\n"
                    f"Completed: {index}/{len(chunks)}"
                )

            text_result = "\n\n".join(
                part.strip() for part in transcribed_parts if part.strip()
            )
            if not text_result:
                await progress_message.edit_text("⚠️ Could not hear any speech.")
                return

            output_path = Path(chunk_directory) / "transcription.txt"
            output_path.write_text(text_result + "\n", encoding="utf-8")
            filename = datetime.now().strftime("transcription_%Y%m%d_%H%M%S.txt")
            with output_path.open("rb") as output_file:
                await update.message.reply_document(
                    document=InputFile(output_file, filename=filename),
                    caption=(
                        f"✅ Transcription complete: {len(chunks)} ordered parts, "
                        f"{format_duration(duration)} total."
                    ),
                )
            await progress_message.edit_text(
                f"✅ Completed {len(chunks)}/{len(chunks)} parts and sent the text file."
            )

    except UsageLimitUnavailable as exc:
        message = (
            "⏳ Groq’s rolling audio allowance does not currently have enough "
            f"capacity. Try again in about {format_duration(exc.wait_seconds)}. "
            "Use /usage for the live meter."
        )
        if progress_message:
            await progress_message.edit_text(message)
        else:
            await update.message.reply_text(message)
    except Exception as exc:
        logger.exception("Error processing media: %s", exc)
        error_text = str(exc).lower()
        if "file is too big" in error_text or "file_too_big" in error_text:
            message = (
                "❌ Telegram could not deliver this file because it exceeds the hosted "
                "Bot API’s 20 MB download limit. Send compressed audio or enable the "
                "Local Bot API server."
            )
        else:
            message = "❌ An error occurred during transcription."
        if progress_message:
            await progress_message.edit_text(message)
        else:
            await update.message.reply_text(message)
    finally:
        await cleanup_file(downloaded_path)


# 4. Main Execution -----------------------------------------------------------
def build_application():
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if TELEGRAM_LOCAL_MODE:
        builder = (
            builder.base_url(TELEGRAM_BASE_URL)
            .base_file_url(TELEGRAM_BASE_FILE_URL)
            .local_mode(True)
        )
    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("usage", usage))
    application.add_handler(CommandHandler("usage_detailed", usage_detailed))
    media_filter = filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE
    application.add_handler(MessageHandler(media_filter, handle_media))
    return application


if __name__ == "__main__":
    logger.info("Bot is polling...")
    build_application().run_polling()
