#!/usr/bin/env bash
set -euo pipefail

# VardenProxy Bot — Systemd Install Script
# Usage: sudo ./install.sh

INSTALL_DIR="/opt/vardenproxybot"
ENV_FILE="/etc/vardenproxybot.conf"
SERVICE_FILE="/etc/systemd/system/vardenproxybot.service"
SERVICE_NAME="vardenproxybot"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ─── 1. Pre-flight Checks ────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (use sudo)."
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    log_error "python3 is not installed."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
    log_error "Python 3.11+ required, found $PY_VERSION."
    exit 1
fi
log_info "Python $PY_VERSION detected."

if ! command -v systemctl &>/dev/null; then
    log_error "systemctl not found. Is systemd installed?"
    exit 1
fi

# ─── 2. Determine Source Directory ───────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"

if [[ ! -f "$SOURCE_DIR/main.py" ]]; then
    log_error "main.py not found in $SOURCE_DIR. Run this script from the project root."
    exit 1
fi
log_info "Source directory: $SOURCE_DIR"

# Detect current user (the one who invoked sudo)
CURRENT_USER="${SUDO_USER:-root}"
CURRENT_GROUP="$(id -gn "$CURRENT_USER" 2>/dev/null || echo "$CURRENT_USER")"
log_info "Service will run as: $CURRENT_USER:$CURRENT_GROUP"

# ─── 3. Prompt for Environment Variables ─────────────────────────────

if [[ -f "$ENV_FILE" ]]; then
    log_info "Environment file $ENV_FILE already exists. Skipping prompts."
    # shellcheck disable=SC1090
    source "$ENV_FILE"
else
    if [[ ! -t 0 ]]; then
        log_error "No TTY available and $ENV_FILE not found. Create it manually or run interactively."
        exit 1
    fi

    echo ""
    echo "═══════════════════════════════════════════════"
    echo "  VardenProxy Bot — Environment Configuration"
    echo "═══════════════════════════════════════════════"
    echo ""

    # Helper: prompt with default
    prompt_var() {
        local var_name="$1" prompt_text="$2" default_val="$3" is_required="$4"
        local input
        while true; do
            if [[ -n "$default_val" ]]; then
                read -rp "$prompt_text [$default_val]: " input
                input="${input:-$default_val}"
            else
                read -rp "$prompt_text: " input
            fi
            input="$(echo "$input" | xargs)"
            if [[ "$is_required" == "true" ]] && [[ -z "$input" ]]; then
                log_error "This field is required."
                continue
            fi
            printf -v "$var_name" '%s' "$input"
            break
        done
    }

    prompt_var BOT_TOKEN        "Enter Telegram Bot Token"              ""       true
    prompt_var ADMIN_IDS        "Enter Admin Telegram IDs (comma-sep)"   ""       true
    prompt_var AUTO_APPROVE     "Auto-approve payments? (true/false)"    "false"  false
    prompt_var PROXY_HOST       "SOCKS5 Proxy Host"                      "127.0.0.1" false
    prompt_var PROXY_PORT       "SOCKS5 Proxy Port"                      "1080"  false
    prompt_var PROXY_USER       "SOCKS5 Proxy Username (leave empty)"    ""       false
    prompt_var PROXY_PASS       "SOCKS5 Proxy Password (leave empty)"    ""       false
    prompt_var DATABASE_URL     "Database URL"                           "sqlite+aiosqlite:///vardenproxy.db" false

    # ─── 4. Write Environment File ───────────────────────────────────

    log_info "Writing environment file: $ENV_FILE"
    cat > "$ENV_FILE" <<EOF
# VardenProxy Bot environment — sourced by systemd
# Generated on $(date -Iseconds)
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
AUTO_APPROVE=$AUTO_APPROVE
PROXY_HOST=$PROXY_HOST
PROXY_PORT=$PROXY_PORT
PROXY_USER=$PROXY_USER
PROXY_PASS=$PROXY_PASS
DATABASE_URL=$DATABASE_URL
EOF
    chmod 600 "$ENV_FILE"
    chown "$CURRENT_USER:$CURRENT_GROUP" "$ENV_FILE"
    log_info "Environment file written (permissions: 600)."
fi

# ─── 5. Copy Source to Install Path ──────────────────────────────────

# Back up existing database files before overwriting
DB_FILES=()
if [[ -d "$INSTALL_DIR" ]]; then
    while IFS= read -r -d '' db; do
        DB_FILES+=("$db")
    done < <(find "$INSTALL_DIR" -maxdepth 1 -type f \( -name "*.db" -o -name "*.sqlite3" -o -name "*.sqlite" \) -print0 2>/dev/null)
fi

if [[ ${#DB_FILES[@]} -gt 0 ]]; then
    BACKUP_DIR="/tmp/vardenproxybot_db_backup_$(date +%Y%m%d%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    for db in "${DB_FILES[@]}"; do
        cp "$db" "$BACKUP_DIR/"
        log_info "Backed up: $(basename "$db") → $BACKUP_DIR"
    done
fi

log_info "Copying source to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r "$SOURCE_DIR"/* "$INSTALL_DIR/"

# Remove dev artifacts
rm -rf "$INSTALL_DIR/.git" "$INSTALL_DIR/venv" "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.example"
rm -rf "$INSTALL_DIR"/__pycache__ "$INSTALL_DIR"/handlers/__pycache__
rm -f "$INSTALL_DIR"/*.pyc "$INSTALL_DIR"/handlers/*.pyc 2>/dev/null || true
rm -f "$INSTALL_DIR"/install.sh 2>/dev/null || true
rm -f "$INSTALL_DIR"/vardenproxy.db 2>/dev/null || true

# Restore backed-up database files
if [[ ${#DB_FILES[@]} -gt 0 ]]; then
    for db in "${DB_FILES[@]}"; do
        cp "$BACKUP_DIR/$(basename "$db")" "$INSTALL_DIR/"
        log_info "Restored: $(basename "$db")"
    done
    rm -rf "$BACKUP_DIR"
fi

chown -R "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR"
log_info "Source copied and cleaned."

# ─── 6. Create Virtual Environment & Install Dependencies ────────────

log_info "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
chown -R "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR/venv"

log_info "Upgrading pip..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip

log_info "Installing Python dependencies..."
if ! "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"; then
    log_error "pip install failed. Check $INSTALL_DIR/requirements.txt and network connectivity."
    exit 1
fi

# Verify key packages are importable
log_info "Verifying package imports..."
if ! "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import aiosqlite; print('telegram', telegram.__version__)"; then
    log_error "Package verification failed. The venv may be broken."
    exit 1
fi
log_info "Dependencies installed and verified."

# ─── 7. Write Systemd Unit ───────────────────────────────────────────

log_info "Writing systemd unit: $SERVICE_FILE"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=VardenProxy Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SERVICE_FILE"
log_info "Systemd unit written."

# ─── 8. Enable & Start Service ───────────────────────────────────────

log_info "Reloading systemd daemon..."
systemctl daemon-reload

# Stop any existing service and kill leftover processes
log_info "Stopping any existing service..."
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sleep 1

# Kill any lingering python processes running main.py
PIDS=$(pgrep -f "venv/bin/python main.py" 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
    log_warn "Killing leftover bot processes: $PIDS"
    kill -9 $PIDS 2>/dev/null || true
    sleep 1
fi

systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

sleep 2
SERVICE_STATUS=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "unknown")

# ─── 9. Post-install Summary ────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════"
echo "  VardenProxy Bot — Installation Complete"
echo "═══════════════════════════════════════════════"
echo ""

if [[ "$SERVICE_STATUS" == "active" ]]; then
    log_info "Service status: ${GREEN}ACTIVE${NC}"
else
    log_warn "Service status: ${YELLOW}$SERVICE_STATUS${NC}"
    echo "  Check logs with: journalctl -u $SERVICE_NAME -n 50"
fi

echo ""
echo "  📁 Install dir:   $INSTALL_DIR"
echo "  🔐 Env file:      $ENV_FILE"
echo "  📋 Service file:  $SERVICE_FILE"
echo ""
echo "  📜 View logs:     journalctl -u $SERVICE_NAME -f"
echo "  🔄 Restart:       systemctl restart $SERVICE_NAME"
echo "  ⏹  Stop:          systemctl stop $SERVICE_NAME"
echo ""
