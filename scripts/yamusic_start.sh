#!/bin/bash

# --- CONFIGURATION ---
# Use relative paths or environment variables for portability
CONFIG_DIR="$HOME/.config/yamusic"
PY_PATH="$CONFIG_DIR/venv/bin/python"
SCRIPT_PATH="$CONFIG_DIR/yamusic_mpd.py"
PLAYER_CLASS="music_player"

# Check if dependencies are installed
command -v mpc >/dev/null 2>&1 || { echo "Error: mpc is not installed."; exit 1; }
command -v kitty >/dev/null 2>&1 || { echo "Error: kitty is not installed."; exit 1; }
command -v rmpc >/dev/null 2>&1 || { echo "Error: rmpc is not installed."; exit 1; }

# --- LOGIC ---

if pgrep -f "rmpc" > /dev/null; then
    # --- SHUTDOWN ---
    echo "Stopping Yandex Music session..."
    mpc stop
    mpc clear
    pkill -f "mpd-mpris"
    pkill -f "rmpc"
    # Optional: notify-send "Yandex Music" "Session closed"
else
    # --- STARTUP ---

    # 1. Start MPD if not running
    if ! pgrep -x "mpd" > /dev/null; then
        echo "Starting MPD..."
        mpd &
        sleep 0.5
    fi

    # 2. Sync tracks (using EOF to automate python script interaction)
    if [ -f "$SCRIPT_PATH" ]; then
        echo "Synchronizing Yandex Music library..."
        "$PY_PATH" "$SCRIPT_PATH" <<EOF
1
500
EOF
    else
        echo "Error: Python script not found at $SCRIPT_PATH"
        exit 1
    fi

    # 3. Start MPRIS bridge for media keys support (if installed)
    if command -v mpd-mpris >/dev/null 2>&1; then
        pkill -f "mpd-mpris"
        mpd-mpris -host 127.0.0.1 -port 6600 &
    fi

    # 4. Open RMPC TUI in Kitty
    echo "Launching RMPC..."
    kitty --class "$PLAYER_CLASS" -e rmpc &

    # 5. Finalize playback
    sleep 1.5
    mpc update > /dev/null
    mpc play
fi
