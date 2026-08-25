#!/usr/bin/env bash
set -euo pipefail

# VardenProxy Bot — Systemd Install Script
# Usage: sudo ./install.sh
#
# Paths can be overridden for testing:
#   VARDEN_INSTALL_DIR / VARDEN_ENV_FILE / VARDEN_SERVICE_FILE
#   VARDEN_FORCE_INTERACTIVE=1  — treat piped stdin as interactive (tests)

INSTALL_DIR="${VARDEN_INSTALL_DIR:-/opt/vardenproxybot}"
ENV_FILE="${VARDEN_ENV_FILE:-/etc/vardenproxybot.conf}"
SERVICE_FILE="${VARDEN_SERVICE_FILE:-/etc/systemd/system/vardenproxybot.service}"
SERVICE_NAME="vardenproxybot"
LOG_FILE="/tmp/vardenproxybot_install.log"

# ─── Output helpers ──────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

log()      { echo -e "${GREEN}✓${NC} $*"; }
warn()     { echo -e "${YELLOW}⚠${NC} $*"; }
err()      { echo -e "${RED}✗${NC} $*" >&2; }
step()     { local title="$*"; local w=$((72 - ${#title})); [[ $w -lt 1 ]] && w=1; printf "\n${BOLD}─── %s " "$title"; printf '─%.0s' $(seq 1 $w); echo -e "───${NC}"; }
info()     { echo -e "  ${DIM}$*${NC}"; }

# ─── Environment variable specification ──────────────────────────────────
# Every setting the bot understands, in prompt order:
#   NAME|Prompt text|Default (empty = required-style ask)|required(true/false)

VAR_SPEC=(
    "BOT_TOKEN|Telegram Bot Token||true"
    "ADMIN_IDS|Admin Telegram IDs (comma-sep)||true"
    "PROXY_HOST|SOCKS5 Proxy Host|127.0.0.1|false"
    "PROXY_PORT|SOCKS5 Proxy Port|1080|false"
    "PROXY_USER|SOCKS5 Proxy Username (optional)||false"
    "PROXY_PASS|SOCKS5 Proxy Password (optional)||false"
    "DATABASE_URL|Database URL|sqlite+aiosqlite:///vardenproxy.db|false"
    "PANEL_URL|Panel URL incl. webBasePath||true"
    "PANEL_API_TOKEN|Panel API Token||true"
    "XUI_INBOUND_ID|Inbound ID||true"
    "VPN_LIMIT_IP|Devices per subscription|2|false"
    "PANEL_VERIFY_SSL|Verify TLS cert? (true/false)|true|false"
    "ZARINPAL_ACCESS_TOKEN|Zarinpal Merchant UUID (blank = mock payments)||false"
    "ZARINPAL_SANDBOX|Zarinpal sandbox mode? (true/false)|false|false"
    "ZARINPAL_CALLBACK_URL|Public payment callback URL (https://pay.example/zarinpal/callback)||false"
    "ZARINPAL_BIND_HOST|Payment callback bind host|127.0.0.1|false"
    "ZARINPAL_BIND_PORT|Payment callback bind port|8099|false"
)

VAR_ORDER=()
declare -A VAR_TEXT VAR_DEFAULT VAR_REQUIRED
load_var_spec() {
    VAR_ORDER=()
    local entry name text default required
    for entry in "${VAR_SPEC[@]}"; do
        IFS='|' read -r name text default required <<< "$entry"
        VAR_ORDER+=("$name")
        VAR_TEXT[$name]="$text"; VAR_DEFAULT[$name]="$default"; VAR_REQUIRED[$name]="$required"
    done
}

prompt_var() {
    local name="$1"
    local text="${VAR_TEXT[$name]}" default="${VAR_DEFAULT[$name]}" required="${VAR_REQUIRED[$name]}"
    local input eof_seen=""
    while true; do
        if [[ -n "$default" ]]; then
            read -rp "  $text [$default]: " input || { input=""; eof_seen=1; }
            input="${input:-$default}"
        else
            read -rp "  $text: " input || { input=""; eof_seen=1; }
        fi
        # Trim surrounding whitespace without mangling the value (no xargs)
        input="${input#"${input%%[![:space:]]*}"}"
        input="${input%"${input##*[![:space:]]}"}"
        if [[ "$input" =~ [^A-Za-z0-9\ ._,:/+\@=\~-] ]]; then
            err "Value contains characters that are unsafe for the env file."
            continue
        fi
        if [[ "$required" == "true" ]] && [[ -z "$input" ]]; then
            if [[ -n "$eof_seen" ]]; then
                err "Input stream closed before '$name' was provided."
                return 1
            fi
            err "Required field."
            continue
        fi
        printf -v "$name" '%s' "$input"
        break
    done
}

is_interactive() {
    [[ -t 0 || -n "${VARDEN_FORCE_INTERACTIVE:-}" ]]
}

# Collect values for every configured var that is still unset.
# Expects the env file (if any) to have been sourced already.
# Populates TO_WRITE with the variable names needing prompts/prompts them.
collect_missing_vars() {
    TO_WRITE=()
    local v
    for v in "${VAR_ORDER[@]}"; do
        [[ -z "${!v:-}" ]] && TO_WRITE+=("$v")
    done

    if [[ ${#TO_WRITE[@]} -eq 0 ]]; then
        return 0
    fi

    if ! is_interactive; then
        err "Missing configuration in $ENV_FILE: ${TO_WRITE[*]}"
        err "Add the variables above or re-run interactively."
        exit 1
    fi

    echo ""
    echo -e "${BOLD}  Missing Configuration${NC}"
    echo -e "  ${DIM}Needs: ${TO_WRITE[*]}${NC}"
    echo ""

    for v in "${TO_WRITE[@]}"; do
        prompt_var "$v"
    done
}

# Write the env file: preserve existing content (minus lines for rewritten
# vars), then append the newly collected assignments.
write_env_file() {
    local had_existing=false
    [[ -f "$ENV_FILE" ]] && had_existing=true

    local kept tmp_final
    kept="$(mktemp)"
    tmp_final="$(mktemp)"

    if [[ "$had_existing" == true && ${#TO_WRITE[@]} -gt 0 ]]; then
        local regex
        regex=$(printf '%s|' "${TO_WRITE[@]}")
        regex="^(${regex%|})="
        grep -vE "$regex" "$ENV_FILE" > "$kept" || true
    elif [[ "$had_existing" == true ]]; then
        cp "$ENV_FILE" "$kept"
    fi

    cat "$kept" > "$tmp_final"
    if [[ "$had_existing" == false ]]; then
        cat >> "$tmp_final" <<EOF
# VardenProxy Bot environment — sourced by systemd
# Generated $(date -Iseconds)
EOF
    else
        echo "" >> "$tmp_final"
        echo "# Updated $(date -Iseconds)" >> "$tmp_final"
    fi
    local v
    for v in "${TO_WRITE[@]}"; do
        echo "$v=\"${!v}\"" >> "$tmp_final"
    done

    mv "$tmp_final" "$ENV_FILE"
    rm -f "$kept"
    chmod 600 "$ENV_FILE"
    local owner="${SUDO_USER:-root}" group
    group="$(id -gn "$owner" 2>/dev/null || echo "$owner")"
    chown "$owner:$group" "$ENV_FILE" 2>/dev/null || true
}

configure_environment() {
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$ENV_FILE"
    fi

    load_var_spec
    collect_missing_vars

    if [[ ${#TO_WRITE[@]} -eq 0 ]]; then
        log "Environment file complete: $ENV_FILE"
        return
    fi

    write_env_file
    log "Environment file written: $ENV_FILE (${#TO_WRITE[@]} new value(s))"
}

# ─── 1. Pre-flight ───────────────────────────────────────────────────────

pre_flight() {
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
}

# ─── 3. Deploy source ────────────────────────────────────────────────────

deploy_source() {
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
}

# ─── 4. Dependencies ─────────────────────────────────────────────────────

install_dependencies() {
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

    if "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import aiosqlite; import aiohttp" >> "$LOG_FILE" 2>&1; then
        log "Import check passed."
    else
        err "Package verification failed. See $LOG_FILE"
        exit 1
    fi
}

# ─── 5. Systemd ──────────────────────────────────────────────────────────

setup_service() {
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
}

# ─── 6. Summary ──────────────────────────────────────────────────────────

show_summary() {
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
}

main() {
    pre_flight
    configure_environment
    deploy_source
    install_dependencies
    setup_service
    show_summary
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
