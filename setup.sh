#!/bin/bash
set -e  # Exit immediately if a command exits with a non-zero status

# Deployment Script for Groq Whisper Bot on Ubuntu/Debian EC2

echo "Starting automated setup..."

# 1. Update System & Install Dependencies
echo "1. Installing System Dependencies (Python, FFmpeg)..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv ffmpeg

# 2. Setup Python Virtual Environment
echo "2. Setting up Python Virtual Environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "   Virtual environment created."
else
    echo "   Virtual environment already exists."
fi

# 3. Install Python Libraries
echo "3. Installing Python requirements..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Configuration Check
echo "4. Checking Configuration..."
if [ ! -f ".env" ]; then
    echo "   WARNING: .env file not found!"
    echo "   Creating one from .env.example..."
    cp .env.example .env
    echo "   IMPORTANT: You must edit .env and add your API keys before running the bot."
    echo "   Run: nano .env"
fi

# 5. Setup Systemd Service (Background Service)
# This allows the bot to run 24/7 and restart if the server reboots
echo "5. Creating Systemd Service..."

SERVICE_FILE="/etc/systemd/system/telegram-whisper.service"
CURRENT_DIR=$(pwd)
USER_NAME=$(whoami)
PYTHON_PATH="$CURRENT_DIR/venv/bin/python"
SCRIPT_PATH="$CURRENT_DIR/bot.py"

# Write the service file (requires sudo)
sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=Telegram Whisper Bot (Groq)
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$CURRENT_DIR
ExecStart=$PYTHON_PATH $SCRIPT_PATH
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "   Service file created at $SERVICE_FILE"

# 6. Enable and Start Service
echo "6. Activating Service..."
sudo systemctl daemon-reload
sudo systemctl enable telegram-whisper.service
# We don't start it yet because the user needs to edit the .env file first

echo "----------------------------------------------------------------"
echo "✅ Setup Complete!"
echo "----------------------------------------------------------------"
echo "NEXT STEPS:"
echo "1. Edit your configuration:  nano .env"
echo "2. Start the bot:            sudo systemctl start telegram-whisper"
echo "3. Check status:             sudo systemctl status telegram-whisper"
echo "4. View logs:                journalctl -u telegram-whisper -f"
echo "----------------------------------------------------------------"
