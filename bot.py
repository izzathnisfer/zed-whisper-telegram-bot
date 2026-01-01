import os
import logging
import asyncio
import tempfile
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from groq import Groq

# 1. Setup & Configuration ----------------------------------------------------
# Load environment variables (api keys)
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")

# Configure logging (important for debugging on server)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Validate Keys
if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("Error: Bot token or Groq API key missing in .env")
    exit(1)

# Initialize Groq Client
client = Groq(api_key=GROQ_API_KEY)

# 2. Security & Helper Functions ----------------------------------------------
def is_authorized(update: Update) -> bool:
    """Check if the sender is the owner of the bot."""
    if not ALLOWED_USER_ID:
        return True # If no ID set, allow everyone (NOT RECOMMENDED)
    
    user_id = update.effective_user.id
    return str(user_id) == str(ALLOWED_USER_ID)

async def cleanup_file(path: str):
    """Securely remove the temporary audio file."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Deleted temporary file: {path}")
    except Exception as e:
        logger.error(f"Failed to delete file {path}: {e}")

# 3. Bot Command Handlers -----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to /start command."""
    if not is_authorized(update):
        return # Silent ignore for unauthorized users
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="👋 I am ready! Send me a voice note, and I will transcribe it instantly using Groq."
    )

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main function: Receives voice -> Transcribes -> Replies."""
    if not is_authorized(update):
        logger.warning(f"Unauthorized access attempt from User ID: {update.effective_user.id}")
        return

    # User feedback: "Typing..." status implies working
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    voice_file_path = None
    
    try:
        # A. Download Voice File
        voice = update.message.voice
        file_id = voice.file_id
        new_file = await context.bot.get_file(file_id)
        
        # Create a temporary file to save the audio
        # We use .ogg as Telegram voice notes are usually OGG Opus
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_file:
            voice_file_path = temp_file.name
            
        await new_file.download_to_drive(voice_file_path)
        logger.info(f"Downloaded voice note from {update.effective_user.first_name}")

        # B. Transcribe with Groq
        # Open file in binary read mode
        with open(voice_file_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=(voice_file_path, file.read()),
                model="whisper-large-v3-turbo",
                response_format="json",
                language=None, # Auto-detect
                temperature=0.0 # Strict accuracy
            )

        text_result = transcription.text
        
        # C. Send Result Back
        if not text_result:
            await update.message.reply_text("⚠️ Could not hear any speech.")
        else:
            # Send as a code block (monospaced) for easy copying
            await update.message.reply_text(f"```\n{text_result}\n```", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error processing voice: {e}")
        await update.message.reply_text("❌ An error occurred during transcription.")
        
    finally:
        # D. Privacy Cleanup (The most important part!)
        if voice_file_path:
            await cleanup_file(voice_file_path)

# 4. Main Execution -----------------------------------------------------------
if __name__ == '__main__':
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    # Filter for VOICE messages only
    voice_handler = MessageHandler(filters.VOICE, handle_voice)
    
    application.add_handler(start_handler)
    application.add_handler(voice_handler)
    
    logger.info("Bot is polling...")
    application.run_polling()
