# 🎙️ Private Groq-Powered Telegram Bot

A high-performance, privacy-focused speech-to-text bot that runs on AWS Free Tier.
It uses **Groq's API** (`whisper-large-v3-turbo`) for instant transcription and **AWS EC2** (`t2.micro`) for hosting.

## 🚀 Features
*   **Instant Transcription**: Responses in < 2 seconds.
*   **Privacy First**: Audio files are deleted immediately after processing.
*   **Zero Cost**: Runs on AWS Free Tier + Groq Free Developer Tier.
*   **Secure**: Includes an authorization check so ONLY YOU can use it.

## 🛠️ Setup Guide (AWS EC2)

### Prerequisites
1.  **Telegram Bot Token**: Get it from [@BotFather](https://t.me/BotFather).
2.  **Groq API Key**: Get it from [Groq Console](https://console.groq.com/keys).
3.  **Telegram User ID**: Get it from [@userinfobot](https://t.me/userinfobot) (to lock the bot to your account).

### 1. Launch AWS Instance
1.  Go to AWS Console -> EC2 -> Launch Instance.
2.  Name: `WhisperBot`.
3.  OS: **Ubuntu** (important for the script).
4.  Instance Type: `t2.micro` (Free Tier).
5.  Key Pair: Create a new one and download it (e.g., `my-key.pem`).
6.  Security: Allow SSH traffic.
7.  **Launch Instance**.

### 2. Connect to Server
On your specific computer (Windows/Mac), open your terminal:

```bash
# Set permissions for your key (Mac/Linux only)
chmod 400 my-key.pem

# SSH into the server (replace IP with your instance's Public IP)
ssh -i "path/to/my-key.pem" ubuntu@12.34.56.78
```

### 3. Deploy Code (One-Step)
Once inside the AWS terminal, run these commands:

```bash
# 1. Clone the code (or upload files) - Assuming you created the files via SCP or manually
# If creating manually:
mkdir whisper-bot
cd whisper-bot
nano setup.sh # Paste the content of setup.sh
nano bot.py # Paste content
nano requirements.txt # Paste content
nano .env.example # Paste content

# 2. Run the Auto-Installer
chmod +x setup.sh
./setup.sh
```

### 4. Configure & Start
The installer will tell you to edit the `.env` file.

```bash
# 1. Edit config
nano .env

# Paste your keys:
# TELEGRAM_BOT_TOKEN=...
# GROQ_API_KEY=...
# ALLOWED_USER_ID=...
# (Ctrl+X, then Y, then Enter to save)

# 2. Start the bot
sudo systemctl start telegram-whisper

# 3. Check if it's running
sudo systemctl status telegram-whisper
```

Your bot is now live! 🟢

## 📜 Maintenance

*   **View Logs** (if something goes wrong):
    ```bash
    journalctl -u telegram-whisper -f
    ```
*   **Restart Bot**:
    ```bash
    sudo systemctl restart telegram-whisper
    ```
