#!/usr/bin/env bash
set -euo pipefail

# VardenProxy Bot — Systemd Install Script
# Usage: sudo ./install.sh

INSTALL_DIR="/opt/vardenproxybot"
ENV_FILE="/etc/vardenproxybot.conf"
SERVICE_FILE="/etc/systemd/system/vardenproxybot.service"
SERVICE_NAME="vardenproxybot"
LOG_FILE="/tmp/vardenproxybot_install.log"

# ─── Output helpers ──────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

log()      { echo -e "${GREEN}✓${NC} $*"; }
warn()     { echo -e "${YELLOW}⚠${NC} $*"; }
err()      { echo -e "${RED}✗${NC} $*" >&2; }
step()     { local title="$*"; local w=$((72 - ${#title})); [[ $w -lt 1 ]] && w=1; printf "\n${BOLD}─── %s " "$title"; printf '─%.0s' $(seq 1 $w); echo -e "───${NC}"; }
info()     { echo -e "  ${DIM}$*${NC}"; }

# ─── 1. Pre-flight ───────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo ./install.sh"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    err "python3 not installed."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 11 ]]; then
    err "Python 3.11+ required, found $PY_VERSION."
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    err "systemctl not found."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/main.py" ]]; then
    err "main.py not found in $SCRIPT_DIR."
    exit 1
fi

CURRENT_USER="${SUDO_USER:-root}"
CURRENT_GROUP="$(id -gn "$CURRENT_USER" 2>/dev/null || echo "$CURRENT_USER")"

echo ""
echo -e "${BOLD}  VardenProxy Bot — Installer${NC}"
echo -e "  ${DIM}Python $PY_VERSION | User: $CURRENT_USER${NC}"

# ─── 2. Environment variables ────────────────────────────────────────────

prompt_var() {
    local var_name="$1" prompt_text="$2" default_val="$3" is_required="$4"
    local input
    while true; do
        if [[ -n "$default_val" ]]; then
            read -rp "  $prompt_text [$default_val]: " input
            input="${input:-$default_val}"
        else
            read -rp "  $prompt_text: " input
        fi
        # Trim surrounding whitespace without mangling the value (no xargs)
        input="${input#"${input%%[![:space:]]*}"}"
        input="${input%"${input##*[![:space:]]}"}"
        if [[ "$input" =~ [^A-Za-z0-9\ ._,:/+\@=\~-] ]]; then
            err "Value contains characters that are unsafe for the env file."
            continue
        fi
        if [[ "$is_required" == "true" ]] && [[ -z "$input" ]]; then
            err "Required field."
            continue
        fi
        printf -v "$var_name" '%s' "$input"
        break
    done
}

PANEL_VARS=(PANEL_URL PANEL_API_TOKEN XUI_INBOUND_ID VPN_LIMIT_IP PANEL_VERIFY_SSL)

if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    MISSING_PANEL=()
    for v in "${PANEL_VARS[@]}"; do
        [[ -z "${!v:-}" ]] && MISSING_PANEL+=("$v")
    done

    if [[ ${#MISSING_PANEL[@]} -eq 0 ]]; then
        log "Environment file found: $ENV_FILE"
    elif [[ ! -t 0 ]]; then
        err "$ENV_FILE missing: ${MISSING_PANEL[*]}. Run interactively."
        exit 1
    else
        echo ""
        echo -e "${BOLD}  Panel Configuration${NC}"
        echo -e "  ${DIM}Missing: ${MISSING_PANEL[*]}${NC}"
        echo ""

        prompt_var PANEL_URL              "Panel URL incl. webBasePath"         "${PANEL_URL:-}"  true
        prompt_var PANEL_API_TOKEN        "API Token"                            "${PANEL_API_TOKEN:-}"  true
        prompt_var XUI_INBOUND_ID         "Inbound ID"                           "${XUI_INBOUND_ID:-}"  true
        prompt_var VPN_LIMIT_IP           "Devices per subscription"             "${VPN_LIMIT_IP:-2}"  false
        prompt_var PANEL_VERIFY_SSL       "Verify TLS cert? (true/false)"        "${PANEL_VERIFY_SSL:-true}"  false

        local_tmp="$(mktemp)"
        panel_regex='^(PANEL_URL|PANEL_API_TOKEN|XUI_INBOUND_ID|VPN_LIMIT_IP|PANEL_VERIFY_SSL)='
        grep -vE "$panel_regex" "$ENV_FILE" > "$local_tmp" || true
        cat >> "$local_tmp" <<EOF

# 3x-ui Panel — updated $(date -Iseconds)
PANEL_URL="$PANEL_URL"
PANEL_API_TOKEN="$PANEL_API_TOKEN"
XUI_INBOUND_ID="$XUI_INBOUND_ID"
VPN_LIMIT_IP="${VPN_LIMIT_IP:-2}"
PANEL_VERIFY_SSL="${PANEL_VERIFY_SSL:-true}"
EOF
        mv "$local_tmp" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        chown "$CURRENT_USER:$CURRENT_GROUP" "$ENV_FILE" 2>/dev/null || true
        log "Panel config updated."
    fi
else
    if [[ ! -t 0 ]]; then
        err "No TTY and $ENV_FILE not found. Run interactively."
        exit 1
    fi

    echo ""
    echo -e "${BOLD}  Environment Configuration${NC}"
    echo ""

    prompt_var BOT_TOKEN        "Telegram Bot Token"                ""       true
    prompt_var ADMIN_IDS        "Admin Telegram IDs (comma-sep)"     ""       true
    prompt_var AUTO_APPROVE     "Auto-approve payments?"             "false"  false
    prompt_var PROXY_HOST       "SOCKS5 Proxy Host"                  "127.0.0.1" false
    prompt_var PROXY_PORT       "SOCKS5 Proxy Port"                  "1080"  false
    prompt_var PROXY_USER       "SOCKS5 Proxy Username (optional)"   ""       false
    prompt_var PROXY_PASS       "SOCKS5 Proxy Password (optional)"   ""       false
    prompt_var DATABASE_URL     "Database URL"                       "sqlite+aiosqlite:///vardenproxy.db" false

    echo ""
    echo -e "${BOLD}  3x-ui Panel${NC}"
    echo ""

    prompt_var PANEL_URL         "Panel URL incl. webBasePath"       ""    true
    prompt_var PANEL_API_TOKEN   "API Token"                         ""    true
    prompt_var XUI_INBOUND_ID    "Inbound ID"                        ""    true
    prompt_var VPN_LIMIT_IP      "Devices per subscription"          "2"   false
    prompt_var PANEL_VERIFY_SSL  "Verify TLS cert? (true/false)"     "true" false

    cat > "$ENV_FILE" <<EOF
# VardenProxy Bot environment — sourced by systemd
# Generated $(date -Iseconds)
BOT_TOKEN="$BOT_TOKEN"
ADMIN_IDS="$ADMIN_IDS"
AUTO_APPROVE="$AUTO_APPROVE"
PROXY_HOST="$PROXY_HOST"
PROXY_PORT="$PROXY_PORT"
PROXY_USER="$PROXY_USER"
PROXY_PASS="$PROXY_PASS"
DATABASE_URL="$DATABASE_URL"
PANEL_URL="$PANEL_URL"
PANEL_API_TOKEN="$PANEL_API_TOKEN"
XUI_INBOUND_ID="$XUI_INBOUND_ID"
VPN_LIMIT_IP="$VPN_LIMIT_IP"
PANEL_VERIFY_SSL="$PANEL_VERIFY_SSL"
EOF
    chmod 600 "$ENV_FILE"
    chown "$CURRENT_USER:$CURRENT_GROUP" "$ENV_FILE"
    log "Environment file written: $ENV_FILE"
fi

# ─── 3. Deploy source ────────────────────────────────────────────────────

step "Deploying"

# Back up existing database files
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
    done
    info "Backed up ${#DB_FILES[@]} database file(s)."
fi

mkdir -p "$INSTALL_DIR"
# Copy source with excludes (avoids dragging venv/.git/db files through cp)
tar -C "$SCRIPT_DIR" \
    --exclude='./venv' --exclude='./.git' --exclude='./.kilo' \
    --exclude='./.env' --exclude='./.env.example' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.db' --exclude='*.sqlite3' --exclude='*.sqlite' \
    --exclude='./install.sh' \
    -cf - . | tar -C "$INSTALL_DIR" -xf -
rm -rf "$INSTALL_DIR/handlers/__pycache__" 2>/dev/null || true

# Restore databases
if [[ ${#DB_FILES[@]} -gt 0 ]]; then
    for db in "${DB_FILES[@]}"; do
        cp "$BACKUP_DIR/$(basename "$db")" "$INSTALL_DIR/"
    done
    rm -rf "$BACKUP_DIR"
    info "Restored database file(s)."
fi

chown -R "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR"
log "Source deployed to $INSTALL_DIR"

# ─── 4. Dependencies ────────────────────────────────────────────────────

step "Dependencies"

python3 -m venv "$INSTALL_DIR/venv" >> "$LOG_FILE" 2>&1
chown -R "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR/venv"
log "Virtual environment created."

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip >> "$LOG_FILE" 2>&1
log "pip upgraded."

if "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" >> "$LOG_FILE" 2>&1; then
    log "Packages installed."
else
    err "pip install failed. See $LOG_FILE"
    exit 1
fi

if "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import aiosqlite" >> "$LOG_FILE" 2>&1; then
    log "Import check passed."
else
    err "Package verification failed. See $LOG_FILE"
    exit 1
fi

# ─── 5. Systemd ─────────────────────────────────────────────────────────

step "Service"

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

systemctl daemon-reload >> "$LOG_FILE" 2>&1
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
pkill -9 -f "^${INSTALL_DIR}/venv/bin/python main[.]py$" 2>/dev/null || true
rm -f /tmp/vardenproxybot.lock
systemctl enable "$SERVICE_NAME" >> "$LOG_FILE" 2>&1
systemctl start "$SERVICE_NAME" >> "$LOG_FILE" 2>&1

# Wait briefly for the service to come up before reporting status.
SERVICE_STATUS="unknown"
for _ in 1 2 3; do
    sleep 2
    SERVICE_STATUS=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "unknown")
    [[ "$SERVICE_STATUS" == "active" ]] && break
done

# ─── 6. Summary ─────────────────────────────────────────────────────────

echo ""

if [[ "$SERVICE_STATUS" == "active" ]]; then
    log "Service:     $SERVICE_STATUS"
else
    warn "Service:     $SERVICE_STATUS"
    info "Check logs: journalctl -u $SERVICE_NAME -n 50"
fi

echo ""
info "Install dir: $INSTALL_DIR"
info "Env file:    $ENV_FILE"
info "Service:     $SERVICE_FILE"
echo ""
info "Logs:        journalctl -u $SERVICE_NAME -f"
info "Restart:     systemctl restart $SERVICE_NAME"
info "Stop:        systemctl stop $SERVICE_NAME"
echo ""
