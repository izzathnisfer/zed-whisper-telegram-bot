import asyncio
import logging
import math
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from dotenv import load_dotenv
from groq import AsyncGroq
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
GROQ_MAX_ATTEMPTS = 4
MEDIA_JOB_CONCURRENCY = 2
UPDATE_CONCURRENCY = 8

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
TELEGRAM_LOCAL_DATA_DIR = Path(
    os.getenv("TELEGRAM_LOCAL_DATA_DIR", "/var/lib/telegram-bot-api")
).resolve()
LOCAL_CACHE_DELETE_HELPER = os.getenv(
    "LOCAL_CACHE_DELETE_HELPER", "/usr/local/libexec/zed-whisper-delete-cache"
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

# Hidden SDK retries make local request accounting inaccurate. Every retry is
# performed explicitly below and atomically recorded before its HTTP request.
client = AsyncGroq(api_key=GROQ_API_KEY, max_retries=0)
usage_tracker = UsageTracker(USAGE_DB_PATH)
media_job_semaphore = asyncio.Semaphore(MEDIA_JOB_CONCURRENCY)


# 2. Security & Helper Functions ----------------------------------------------
class UsageLimitUnavailable(Exception):
    def __init__(self, wait_seconds: float):
        super().__init__(f"Groq capacity becomes available in {wait_seconds:.0f}s")
        self.wait_seconds = wait_seconds


class PrivacyCleanupError(Exception):
    """Raised when a Local Bot API media cache file cannot be removed."""


def is_authorized(update: Update) -> bool:
    """Check if the sender is the owner of the bot."""
    if not ALLOWED_USER_ID:
        return True
    user_id = update.effective_user.id
    return str(user_id) == str(ALLOWED_USER_ID)


def get_local_cache_path(file_path: object) -> Optional[str]:
    """Return an approved Local Bot API cache path without exposing its name."""
    if not TELEGRAM_LOCAL_MODE or not file_path:
        return None
    candidate = Path(str(file_path))
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(TELEGRAM_LOCAL_DATA_DIR)
    except ValueError:
        return None
    return str(resolved)


async def cleanup_file(path: Optional[str], label: str = "temporary media") -> bool:
    """Immediately unlink a media file without logging its sensitive path."""
    if not path:
        return True
    try:
        Path(path).unlink(missing_ok=True)
        logger.info("Deleted %s", label)
        return True
    except OSError as exc:
        logger.error("Failed to delete %s (%s)", label, type(exc).__name__)
        return False


async def cleanup_local_cache(path: Optional[str]) -> bool:
    """Delete one validated Local Bot API cache file via the root helper."""
    if not path:
        return True
    process = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        LOCAL_CACHE_DELETE_HELPER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.communicate(f"{path}\n".encode())
    if process.returncode == 0:
        logger.info("Deleted Local Bot API cached media")
        return True
    logger.error(
        "Failed to delete Local Bot API cached media (helper status=%s)",
        process.returncode,
    )
    return False


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


def transcription_filename(
    media: object,
    file_ext: str,
    media_label: str,
    now: Optional[datetime] = None,
) -> str:
    """Keep an uploaded media filename and append .txt to the full name."""
    original_name = getattr(media, "file_name", None)
    if original_name and str(original_name).strip():
        safe_name = Path(str(original_name).replace("\\", "/")).name
        if safe_name:
            return f"{safe_name}.txt"

    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    fallback_label = media_label.replace(" ", "_")
    return f"{fallback_label}_{timestamp}{file_ext}.txt"


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
    # Re-encode instead of segmenting the original container. Groq has returned
    # HTTP 500 for segmented FLAC files even when FFprobe can read them; a mono,
    # constant-bitrate MP3 is both small and broadly accepted by the endpoint.
    output_pattern = str(Path(output_directory) / "chunk_%04d.mp3")
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
        "libmp3lame",
        "-b:a",
        "32k",
        "-map_metadata",
        "-1",
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

    chunks = sorted(str(path) for path in Path(output_directory).glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce any audio chunks")
    return chunks


async def _groq_transcription(filename: str, audio_bytes: bytes):
    return await client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=GROQ_MODEL,
        response_format="json",
        language=None,
        temperature=0.0,
    )


async def transcribe_file(
    path: str,
    audio_seconds: float,
    wait_callback: Optional[Callable[[float], Awaitable[None]]] = None,
    retry_callback: Optional[Callable[[int, int, float], Awaitable[None]]] = None,
) -> str:
    """Atomically reserve, count, and send every real Groq HTTP attempt."""
    filename = os.path.basename(path)
    # Chunks are normalized to small 32 kbps MP3 files. Reading them directly
    # avoids keeping a blocking worker occupied across asynchronous retries.
    audio_bytes = Path(path).read_bytes()
    for attempt in range(1, GROQ_MAX_ATTEMPTS + 1):
        while True:
            wait_seconds = usage_tracker.reserve_attempt(audio_seconds)
            if wait_seconds <= 0:
                break
            if wait_seconds > MAX_AUTOMATIC_LIMIT_WAIT_SECONDS:
                raise UsageLimitUnavailable(wait_seconds)
            if wait_callback:
                await wait_callback(wait_seconds)
            await asyncio.sleep(wait_seconds + 0.25)

        try:
            transcription = await _groq_transcription(filename, audio_bytes)
            return transcription.text or ""
        except Exception as exc:
            retry_delay = groq_retry_delay(exc, attempt)
            if retry_delay is None or attempt >= GROQ_MAX_ATTEMPTS:
                raise
            status_code = getattr(exc, "status_code", None)
            logger.warning(
                "Groq transcription attempt %s/%s failed (status=%s); retrying",
                attempt,
                GROQ_MAX_ATTEMPTS,
                status_code or type(exc).__name__,
            )
            if retry_callback:
                await retry_callback(attempt + 1, GROQ_MAX_ATTEMPTS, retry_delay)
            await asyncio.sleep(retry_delay)

    raise RuntimeError("Groq transcription attempts exhausted")


def groq_retry_delay(exc: Exception, attempt: int) -> Optional[float]:
    """Return a bounded retry delay for transient Groq failures."""
    status_code = getattr(exc, "status_code", None)
    exception_name = type(exc).__name__
    retryable = (
        status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
        or exception_name in {"APIConnectionError", "APITimeoutError"}
    )
    if not retryable:
        return None

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    try:
        retry_after = float(headers.get("retry-after", 0))
    except (TypeError, ValueError):
        retry_after = 0
    fallback_delays = (2.0, 5.0, 10.0)
    fallback = fallback_delays[min(max(0, attempt - 1), len(fallback_delays) - 1)]
    return min(MAX_AUTOMATIC_LIMIT_WAIT_SECONDS, max(retry_after, fallback))


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
    snapshot = usage_tracker.snapshot()
    await update.message.reply_text(
        "🎧 Groq hourly audio usage\n\n"
        + audio_metric_text("Main limit: 2 hours per rolling hour", snapshot.audio_hour)
        + "\n\nUse /usage_detailed to see all four limits."
    )


async def usage_detailed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    snapshot = usage_tracker.snapshot()
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
    """Run up to two media jobs concurrently without blocking commands."""
    if not is_authorized(update):
        logger.warning(
            "Unauthorized access attempt from User ID: %s", update.effective_user.id
        )
        return

    queued_message = None
    if media_job_semaphore.locked():
        queued_message = await update.message.reply_text(
            "⏳ Two recordings are already being processed. This one is queued."
        )
    async with media_job_semaphore:
        if queued_message:
            await queued_message.edit_text("▶️ Starting this queued recording now…")
        await process_media(update, context)


async def process_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive voice/audio/video, transcribe it, and return the result."""

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    downloaded_path = None
    local_cache_path = None
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
        local_cache_path = get_local_cache_path(new_file.file_path)
        if TELEGRAM_LOCAL_MODE and not local_cache_path:
            raise PrivacyCleanupError(
                "Local Bot API returned a media path outside its private data directory"
            )
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
            downloaded_path = temp_file.name
        if local_cache_path:
            # PTB uses synchronous shutil.copyfile for Local Bot API paths.
            # Offload it so /usage remains responsive during large copies.
            await asyncio.to_thread(shutil.copyfile, local_cache_path, downloaded_path)
        else:
            await new_file.download_to_drive(downloaded_path)

        duration = await probe_duration(downloaded_path, fallback=declared_duration)

        if duration <= CHUNK_SECONDS:
            # Keep the proven short-audio path unchanged: transcribe the
            # downloaded media directly and return a normal copyable message.
            # Both server-side copies are removed by the handler's finally
            # block immediately after the reply is sent.
            async def keep_typing(
                _attempt: int, _maximum: int, _delay: float
            ) -> None:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action="typing"
                )

            text_result = await transcribe_file(
                downloaded_path,
                duration,
                retry_callback=keep_typing,
            )
            if not text_result:
                await update.message.reply_text("⚠️ Could not hear any speech.")
            else:
                # Preserve the original short-media behavior: a copyable message.
                await update.message.reply_text(f"`{text_result}`", parse_mode="Markdown")
            if progress_message:
                await progress_message.delete()
            return

        if local_cache_path:
            if not await cleanup_local_cache(local_cache_path):
                raise PrivacyCleanupError(
                    "Could not delete Telegram's cached long-media copy"
                )
            local_cache_path = None

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
            capacity_wait = usage_tracker.seconds_until_available(
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
            if not await cleanup_file(downloaded_path, "source working media"):
                raise PrivacyCleanupError(
                    "Could not delete source media after creating private chunks"
                )
            downloaded_path = None
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

                async def show_retry(
                    attempt: int,
                    maximum: int,
                    delay: float,
                    part=index,
                ) -> None:
                    await progress_message.edit_text(
                        f"🔄 Groq temporary error on part {part}/{len(chunks)}. "
                        f"Retry {attempt}/{maximum} in {format_duration(delay)}…"
                    )

                snapshot = usage_tracker.snapshot()
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
                try:
                    part_text = await transcribe_file(
                        chunk_path,
                        chunk_duration,
                        show_wait,
                        show_retry,
                    )
                finally:
                    chunk_deleted = await cleanup_file(
                        chunk_path, "processed audio chunk"
                    )
                    if not chunk_deleted:
                        raise PrivacyCleanupError(
                            "Could not delete a processed audio chunk"
                        )
                transcribed_parts.append(part_text)
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
            filename = transcription_filename(media, file_ext, media_label)
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
    except PrivacyCleanupError:
        logger.error("Privacy cleanup failed; transcription was aborted")
        message = (
            "❌ Privacy protection stopped this job because an audio file could not "
            "be deleted immediately. Processing has stopped."
        )
        if progress_message:
            await progress_message.edit_text(message)
        else:
            await update.message.reply_text(message)
    except Exception as exc:
        logger.error(
            "Media processing failed (%s, status=%s)",
            type(exc).__name__,
            getattr(exc, "status_code", "not-applicable"),
        )
        error_text = str(exc).lower()
        if "file is too big" in error_text or "file_too_big" in error_text:
            message = (
                "❌ Telegram could not deliver this file because it exceeds the hosted "
                "Bot API’s 20 MB download limit. Send compressed audio or enable the "
                "Local Bot API server."
            )
        elif getattr(exc, "status_code", None) == 429:
            message = (
                "⏳ Groq is rate-limiting the account. Every attempt was counted; "
                "please check /usage and try again after the shown reset time."
            )
        elif isinstance(getattr(exc, "status_code", None), int) and getattr(
            exc, "status_code"
        ) >= 500:
            message = (
                "❌ Groq is temporarily failing to process this audio even after "
                "retries. The audio has been deleted; please resend it shortly."
            )
        else:
            message = "❌ An error occurred during transcription."
        if progress_message:
            await progress_message.edit_text(message)
        else:
            await update.message.reply_text(message)
    finally:
        await cleanup_file(downloaded_path, "working media copy")
        await cleanup_local_cache(local_cache_path)


# 4. Main Execution -----------------------------------------------------------
def build_application():
    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(UPDATE_CONCURRENCY)
    )
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
