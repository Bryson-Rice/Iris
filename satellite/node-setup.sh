#!/bin/bash
# Usage:
#   ./node-setup.sh
#   ./node-setup.sh --room kitchen

set -euo pipefail

# Address of the machine running the Iris server and the MQTT broker 
# Edit this first
IRIS_SERVER="10.0.0.142"

# Where model.zip and piper.zip are served from; see "Files for the satellite" in the README.
FILE_SERVER="http://${IRIS_SERVER}:80"
MQTT_BROKER="${IRIS_SERVER}"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
INSTALL_DIR="$HOME/iris-satellite"
VENV_DIR="${INSTALL_DIR}/.venv"
SERVICE_NAME="iris-satellite"

RED="\033[91m"
GREEN="\033[92m"
YELLOW="\033[93m"
CYAN="\033[96m"
BOLD="\033[1m"
RESET="\033[0m"

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

step() {
    echo ""
    echo -e "${BOLD}${CYAN}━━━  $*  ━━━${RESET}"
}

# Copies a sound from the repo, keeps one already in place, or downloads it. Downloads
# go to a temp name because wget -O empties its target even when the download fails.
install_sound() {
    local name="$1" fallback="$2"
    local source="${SCRIPT_DIR}/${name}" dest="${INSTALL_DIR}/${name}"

    if [[ -f "${source}" ]]; then
        cp "${source}" "${dest}"
        success "${name} copied from the script directory."
    elif [[ -f "${dest}" ]]; then
        info "${name} already in ${INSTALL_DIR}."
    elif wget -q "${FILE_SERVER}/${name}" -O "${dest}.part"; then
        mv "${dest}.part" "${dest}"
        success "${name} downloaded."
    else
        rm -f "${dest}.part"
        warn "${name} not found. ${fallback}"
    fi
}

ROOM_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --room=*) ROOM_ARG="${1#--room=}" ;;
        --room)   ROOM_ARG="${2:-}"; shift ;;
    esac
    shift
done

clear
echo -e "${BOLD}"
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║     Iris Satellite Node — Setup Script        ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo -e "${RESET}"
echo -e "  Iris server  : ${CYAN}${IRIS_SERVER}${RESET}"
echo -e "  File server  : ${CYAN}${FILE_SERVER}${RESET}"
echo -e "  Install dir  : ${CYAN}${INSTALL_DIR}${RESET}"
echo ""

if [[ -n "$ROOM_ARG" ]]; then
    ROOM_NAME="$ROOM_ARG"
else
    read -rp "  Enter the room name for this Pi (e.g. bedroom): " ROOM_NAME
fi
ROOM_NAME="${ROOM_NAME// /_}"
[[ -z "$ROOM_NAME" ]] && die "Room name cannot be empty."
echo -e "  Room name    : ${CYAN}${ROOM_NAME}${RESET}"
echo ""
read -rp "  Press Enter to start setup…"

step "1 / 8  System packages"

info "Updating package list…"
sudo apt-get update -qq

info "Installing system dependencies…"
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    pipx \
    portaudio19-dev \
    libasound2-dev \
    libsndfile1-dev \
    alsa-utils \
    espeak-ng \
    mosquitto-clients \
    wget \
    unzip \
    curl \
    libgomp1

pipx ensurepath 2>/dev/null || true
export PATH="$PATH:$HOME/.local/bin"

success "System packages installed."

step "2 / 8  Install directory"

mkdir -p "${INSTALL_DIR}"
success "Directory ready: ${INSTALL_DIR}"

step "3 / 8  Python virtual environment"

info "Creating venv at ${VENV_DIR}…"
python3 -m venv "${VENV_DIR}"

PIP="${VENV_DIR}/bin/pip"

info "Upgrading pip inside venv…"
"${PIP}" install --upgrade pip --quiet

info "Installing Python packages…"
"${PIP}" install --quiet \
    vosk \
    sounddevice \
    paho-mqtt \
    numpy \
    samplerate

success "Python environment ready."

step "4 / 8  Vosk speech model"

MODEL_DIR="${INSTALL_DIR}/model"
MODEL_ZIP="${INSTALL_DIR}/model.zip"

if [[ -d "${MODEL_DIR}" ]]; then
    warn "Vosk model already exists at ${MODEL_DIR} — skipping download."
else
    info "Downloading model.zip from ${FILE_SERVER}…"
    wget -q --show-progress \
        "${FILE_SERVER}/model.zip" \
        -O "${MODEL_ZIP}" \
        || die "Failed to download model.zip. Is the file server running on ${FILE_SERVER}?"

    info "Extracting model.zip…"
    unzip -q "${MODEL_ZIP}" -d "${INSTALL_DIR}/model_tmp"

    # Flatten if the zip wraps everything in one top-level folder.
    EXTRACTED=$(find "${INSTALL_DIR}/model_tmp" -maxdepth 1 -mindepth 1 -type d | head -1)
    if [[ -n "${EXTRACTED}" ]]; then
        mv "${EXTRACTED}" "${MODEL_DIR}"
        rmdir "${INSTALL_DIR}/model_tmp" 2>/dev/null || true
    else
        mv "${INSTALL_DIR}/model_tmp" "${MODEL_DIR}"
    fi

    rm -f "${MODEL_ZIP}"
    success "Vosk model installed at ${MODEL_DIR}"
fi

step "5 / 8  Piper TTS"

PIPER_DIR="${INSTALL_DIR}/piper"
PIPER_ZIP="${INSTALL_DIR}/piper.zip"

if [[ -f "${PIPER_DIR}/piper" ]]; then
    warn "Piper already exists at ${PIPER_DIR} — skipping download."
elif wget -q --show-progress "${FILE_SERVER}/piper.zip" -O "${PIPER_ZIP}"; then
    info "Extracting piper.zip…"
    mkdir -p "${PIPER_DIR}"
    unzip -q "${PIPER_ZIP}" -d "${PIPER_DIR}_tmp"

    # Flatten if the zip wraps everything in one top-level folder.
    EXTRACTED=$(find "${PIPER_DIR}_tmp" -maxdepth 1 -mindepth 1 -type d | head -1)
    if [[ -n "${EXTRACTED}" ]]; then
        cp -r "${EXTRACTED}/." "${PIPER_DIR}/"
        rm -rf "${PIPER_DIR}_tmp"
    else
        cp -r "${PIPER_DIR}_tmp/." "${PIPER_DIR}/"
        rm -rf "${PIPER_DIR}_tmp"
    fi

    [[ -f "${PIPER_DIR}/piper" ]] && chmod +x "${PIPER_DIR}/piper"

    rm -f "${PIPER_ZIP}"
    success "Piper installed at ${PIPER_DIR}"
else
    rm -f "${PIPER_ZIP}"
    warn "Could not download piper.zip from ${FILE_SERVER}. Continuing without Piper;"
    warn "the satellite will speak with espeak-ng instead."
fi

step "6 / 8  ALSA audio configuration"

EMEET_CARD=$(aplay -l 2>/dev/null | grep -i "emeet\|M0\|OfficeCore" | grep -oP "card \K[0-9]+" | head -1 || true)

if [[ -z "${EMEET_CARD}" ]]; then
    warn "EMEET microphone not detected in aplay -l."
    warn "Listing all available playback devices:"
    aplay -l 2>/dev/null || true
    read -rp "  Enter the card number to use (check aplay -l): " EMEET_CARD
fi

[[ -z "${EMEET_CARD}" ]] && die "Could not determine audio card number."

# A USB card's number can change when it is replugged; its name doesn't.
CARD_ID="$(cat "/proc/asound/card${EMEET_CARD}/id" 2>/dev/null || true)"
if [[ -n "${CARD_ID}" ]]; then
    ALSA_DEVICE="hw:CARD=${CARD_ID},DEV=0"
    ALSA_CTL_CARD="\"${CARD_ID}\""
    info "Using audio card ${EMEET_CARD} (${CARD_ID}), selected by name."
else
    ALSA_DEVICE="hw:${EMEET_CARD},0"
    ALSA_CTL_CARD="${EMEET_CARD}"
    warn "Could not read the card's name, so it is selected by number. The satellite"
    warn "may not find it again if the USB device is unplugged and replugged."
fi

info "Writing /etc/asound.conf…"
sudo tee /etc/asound.conf > /dev/null << EOF
pcm.!default {
    type asym
    playback.pcm "iris_out"
    capture.pcm  "iris_in"
}

pcm.iris_out {
    type plug
    slave.pcm "${ALSA_DEVICE}"
}

pcm.iris_in {
    type plug
    slave.pcm "${ALSA_DEVICE}"
}

ctl.!default {
    type hw
    card ${ALSA_CTL_CARD}
}
EOF

info "Testing audio output (you should hear a beep)…"
if aplay -q /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null; then
    success "Audio output working."
else
    warn "Audio test sound not found — trying speaker-test for 1 second…"
    timeout 2 speaker-test -t sine -l 1 -q 2>/dev/null || true
    warn "If you heard a tone, audio is fine. If not, check 'aplay -l' and re-run setup."
fi

step "7 / 8  Satellite script and sounds"

if [[ -f "${SCRIPT_DIR}/satellite_node.py" ]]; then
    cp "${SCRIPT_DIR}/satellite_node.py" "${INSTALL_DIR}/satellite_node.py"
    info "Copied satellite_node.py from script directory."
else
    info "satellite_node.py not found locally — downloading from file server…"
    wget -q "${FILE_SERVER}/satellite_node.py" \
        -O "${INSTALL_DIR}/satellite_node.py" \
        || die "Could not find satellite_node.py. Place it next to setup.sh or on the file server."
fi

info "Patching room name → '${ROOM_NAME}' and broker → '${MQTT_BROKER}'…"
SATELLITE="${INSTALL_DIR}/satellite_node.py"
patch_setting() { sed -i "s|^$1[[:space:]]*=.*|$1 = \"$2\"|" "${SATELLITE}"; }

patch_setting ROOM_NAME    "${ROOM_NAME}"
patch_setting MQTT_BROKER  "${MQTT_BROKER}"
patch_setting MODEL_PATH   "${MODEL_DIR}"
patch_setting PIPER_BINARY "${PIPER_DIR}/piper"
patch_setting PIPER_MODEL  "${PIPER_DIR}/voice.onnx"

success "satellite_node.py installed and configured."

install_sound wake.wav "The activation chime will fall back to espeak-ng."
install_sound timer.wav "The timer alarm will fall back to spoken beeps."

step "8 / 8  Systemd service"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

info "Writing ${SERVICE_FILE}…"
sudo tee "${SERVICE_FILE}" > /dev/null << EOF
[Unit]
Description=Iris Satellite Node (${ROOM_NAME})
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/satellite_node.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
ExecStartPre=/bin/sleep 5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

success "Service installed and enabled."

echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${BOLD}${GREEN}  Setup complete!${RESET}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "  Room         : ${CYAN}${ROOM_NAME}${RESET}"
echo -e "  Install dir  : ${CYAN}${INSTALL_DIR}${RESET}"
echo -e "  Venv         : ${CYAN}${VENV_DIR}${RESET}"
echo -e "  Vosk model   : ${CYAN}${MODEL_DIR}${RESET}"
echo -e "  Piper        : ${CYAN}${PIPER_DIR}${RESET}"
echo -e "  Service      : ${CYAN}${SERVICE_NAME}${RESET}"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "  1. Start the service:  ${YELLOW}sudo systemctl start ${SERVICE_NAME}${RESET}"
echo -e "  2. Watch the logs:     ${YELLOW}journalctl -fu ${SERVICE_NAME}${RESET}"
echo -e "  3. Test with:          ${YELLOW}mosquitto_pub -h ${IRIS_SERVER} -t \"iris/voice/${ROOM_NAME}\" \\"
echo -e "                           -m '{\"room\":\"${ROOM_NAME}\",\"text\":\"turn on the lights\",\"timestamp\":0}'${RESET}"
echo ""