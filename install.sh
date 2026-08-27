#!/usr/bin/env bash
set -euo pipefail

# VardenProxy Bot — Systemd Install Script
# Usage: sudo ./install.sh
#
# Paths can be overridden for testing:
#   VARDEN_INSTALL_DIR / VARDEN_ENV_FILE / VARDEN_SERVICE_FILE
#   VARDEN_FORCE_INTERACTIVE=1  — treat piped stdin as interactive (tests)
#   VARDEN_REUSE_VENV=1         — reuse $INSTALL_DIR/venv if exists (skip pip, ~1s)
#   VARDEN_SHARED_VENV=/path    — symlink a shared venv into $INSTALL_DIR/venv (instant across ephemeral INSTALL_DIRs)

INSTALL_DIR="${VARDEN_INSTALL_DIR:-/opt/vardenproxybot}"
ENV_FILE="${VARDEN_ENV_FILE:-/etc/vardenproxybot.conf}"
SERVICE_FILE="${VARDEN_SERVICE_FILE:-/etc/systemd/system/vardenproxybot.service}"
SERVICE_NAME="vardenproxybot"
LOG_FILE="$(mktemp /tmp/vardenproxybot_install.XXXXXX.log)"
# mktemp creates 600, ensure and keep for debugging (secure, no symlink race)
chmod 600 "$LOG_FILE" 2>/dev/null || true

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
    "PROXY_DISABLED|Disable SOCKS5 proxy? (true/false)|false|false"
    "PROXY_HOST|SOCKS5 Proxy Host|127.0.0.1|false"
    "PROXY_PORT|SOCKS5 Proxy Port|1080|false"
    "PROXY_USER|SOCKS5 Proxy Username (optional)||false"
    "PROXY_PASS|SOCKS5 Proxy Password (optional)||false"
    "DATABASE_URL|Database URL|postgresql+psycopg://varden:varden_pass@localhost:5432/vardenproxy|false"
    "PANEL_URL|Panel URL incl. webBasePath||true"
    "PANEL_API_TOKEN|Panel API Token||true"
    "XUI_INBOUND_ID|Inbound ID||true"
    "VPN_LIMIT_IP|Devices per subscription|2|false"
    "PANEL_VERIFY_SSL|Verify TLS cert? (true/false)|true|false"
    "ZARINPAL_ACCESS_TOKEN|Zarinpal Merchant UUID (blank = mock payments)||false"
    "ZARINPAL_SANDBOX|Zarinpal sandbox mode? (true/false)|false|false"
    "ZARINPAL_CALLBACK_URL|Public payment callback URL (https://pay.example/zarinpal/callback)||false"
    "ZARINPAL_ZARINGATE|Bypass checkout page direct to bank? (true/false)|true|false"
    "ZARINPAL_BIND_HOST|Payment callback bind host|127.0.0.1|false"
    "ZARINPAL_BIND_PORT|Payment callback bind port|8099|false"
    "ADMIN_PANEL_USER|Admin panel username|admin|false"
    "ADMIN_PANEL_PASS|Admin panel password (BasicAuth)||false"
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
        # Block newlines and control chars; other chars are safely escaped via %q on write, so allow URL-safe and common values.
        if [[ "$input" == *$'\n'* || "$input" == *$'\r'* ]]; then
            err "Value must not contain newlines."
            continue
        fi
        if [[ "$input" == *$'\x00'* ]]; then
            err "Value contains null bytes."
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
        # Use %q to safely escape any value for bash source (handles ", ', $, \, etc.)
        printf '%s=%q\n' "$v" "${!v}" >> "$tmp_final"
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
        if [[ ! -r "$ENV_FILE" ]]; then
            err "Cannot read $ENV_FILE"
            exit 1
        fi
        # Validate file contains only safe assignments/comments before sourcing (defense-in-depth)
        if grep -qvE '^[[:space:]]*(#.*|[A-Z_][A-Z0-9_]*=.*)?[[:space:]]*$' "$ENV_FILE" 2>/dev/null; then
            err "Env file $ENV_FILE contains invalid lines — refusing to source."
            grep -n -vE '^[[:space:]]*(#.*|[A-Z_][A-Z0-9_]*=.*)?[[:space:]]*$' "$ENV_FILE" >&2 || true
            exit 1
        fi
        # shellcheck disable=SC1090
        set -a
        # shellcheck disable=SC1091
        source "$ENV_FILE"
        set +a
    fi

    load_var_spec
    collect_missing_vars

    # Auto-generate ADMIN_PANEL_PASS if still empty (secure default)
    if [[ -z "${ADMIN_PANEL_PASS:-}" ]]; then
        if command -v openssl &>/dev/null; then
            ADMIN_PANEL_PASS="$(openssl rand -base64 32 | tr -d '\n' | tr -d '\r')"
        else
            ADMIN_PANEL_PASS="$(head -c 24 /dev/urandom | base64 | tr -d '\n')"
        fi
        # Ensure it's tracked for writing if not already
        if [[ ! " ${TO_WRITE[*]} " =~ " ADMIN_PANEL_PASS " ]]; then
            TO_WRITE+=("ADMIN_PANEL_PASS")
        fi
        info "Generated ADMIN_PANEL_PASS (BasicAuth)"
    fi

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

prompt_db_wipe() {
    # Offer to wipe existing DB files with double confirmation. Returns 0 if wiped, 1 if kept.
    if [[ ${#DB_FILES[@]} -eq 0 ]]; then
        return 1
    fi
    # Allow non-interactive opt-in via env: VARDEN_WIPE_DB=1
    if [[ "${VARDEN_WIPE_DB:-}" == "1" || "${VARDEN_WIPE_DB:-}" == "true" ]]; then
        warn "VARDEN_WIPE_DB is set — removing ${#DB_FILES[@]} database file(s) without prompt."
        return 0
    fi
    if ! is_interactive; then
        info "Keeping existing database file(s) (${#DB_FILES[@]} found) — non-interactive mode."
        return 1
    fi
    echo ""
    echo -e "${YELLOW}${BOLD}  Existing database found:${NC}"
    for db in "${DB_FILES[@]}"; do
        size=$(du -h "$db" 2>/dev/null | cut -f1)
        echo -e "    ${DIM}$db (${size:-?})${NC}"
    done
    echo ""
    local ans1 ans2
    read -rp "  Remove all current DB files and start fresh? (yes/no) [no]: " ans1 || ans1=""
    ans1=$(echo "$ans1" | tr '[:upper:]' '[:lower:]' | xargs 2>/dev/null || echo "$ans1")
    if [[ "$ans1" != "yes" && "$ans1" != "y" ]]; then
        info "Keeping existing database."
        return 1
    fi
    echo -e "${RED}${BOLD}  This will PERMANENTLY delete the database(s) listed above!${NC}"
    read -rp "  Type DELETE to confirm: " ans2 || ans2=""
    if [[ "$ans2" != "DELETE" ]]; then
        info "Confirmation did not match — keeping database."
        return 1
    fi
    return 0
}

deploy_source() {
    step "Deploying"

    # Back up existing database files
    DB_FILES=()
    if [[ -d "$INSTALL_DIR" ]]; then
        while IFS= read -r -d '' db; do
            DB_FILES+=("$db")
        done < <(find "$INSTALL_DIR" -maxdepth 1 -type f \( -name "*.db" -o -name "*.sqlite3" -o -name "*.sqlite" \) -print0 2>/dev/null)
    fi
    # Also handle DATABASE_URL pointing outside INSTALL_DIR (e.g. /var/lib/farmstore/db.sqlite)
    if [[ -n "${DATABASE_URL:-}" && "$DATABASE_URL" == sqlite* ]]; then
        _db_path="${DATABASE_URL#*:///}"
        _db_path="${_db_path%%\?*}"
        # Normalize: handle :memory: and empty
        if [[ -n "$_db_path" && "$_db_path" != :memory:* ]]; then
            # Resolve relative paths against INSTALL_DIR / SCRIPT_DIR / cwd
            if [[ "$_db_path" != /* ]]; then
                for _base in "$INSTALL_DIR" "$SCRIPT_DIR" "$(pwd)"; do
                    _cand="$_base/$_db_path"
                    if [[ -f "$_cand" ]]; then
                        _db_path="$_cand"
                        break
                    fi
                done
            fi
            if [[ -f "$_db_path" ]]; then
                _already=false
                for _existing in "${DB_FILES[@]}"; do
                    [[ "$_existing" == "$_db_path" ]] && _already=true && break
                done
                if [[ "$_already" == false ]]; then
                    DB_FILES+=("$_db_path")
                    info "Found DB via DATABASE_URL: $_db_path"
                fi
            fi
        fi
    fi

    WIPE_DB=false
    if prompt_db_wipe; then
        WIPE_DB=true
        # Remove DB files now; skip backup/restore
        for db in "${DB_FILES[@]}"; do
            rm -f "$db" 2>/dev/null || true
            # Also remove WAL/SHM sidecars if present
            rm -f "${db}-wal" "${db}-shm" 2>/dev/null || true
        done
        log "Removed ${#DB_FILES[@]} database file(s) — fresh start."
        DB_FILES=()
    elif [[ ${#DB_FILES[@]} -gt 0 ]]; then
        BACKUP_DIR="$(mktemp -d /tmp/vardenproxybot_db_backup_XXXXXX)"
        chmod 700 "$BACKUP_DIR" 2>/dev/null || true
        for db in "${DB_FILES[@]}"; do
            cp "$db" "$BACKUP_DIR/"
        done
        info "Backed up ${#DB_FILES[@]} database file(s)."
    fi

    # Preserve admin_settings.json (package config) if exists
    ADMIN_SETTINGS_SRC="$INSTALL_DIR/admin_settings.json"
    ADMIN_SETTINGS_TMP=""
    if [[ -f "$ADMIN_SETTINGS_SRC" && "$WIPE_DB" != "true" ]]; then
        ADMIN_SETTINGS_TMP="$(mktemp /tmp/varden_admin_settings.XXXXXX.json)"
        cp "$ADMIN_SETTINGS_SRC" "$ADMIN_SETTINGS_TMP" 2>/dev/null || true
        chmod 600 "$ADMIN_SETTINGS_TMP" 2>/dev/null || true
    fi

    mkdir -p "$INSTALL_DIR"
    chmod 750 "$INSTALL_DIR" 2>/dev/null || true
    # Copy source with excludes (avoids dragging venv/.git/db files through cp)
    tar -C "$SCRIPT_DIR" \
        --exclude='./venv' --exclude='./.git' --exclude='./.kilo' \
        --exclude='./.env' --exclude='./.env.example' \
        --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='*.db' --exclude='*.sqlite3' --exclude='*.sqlite' \
        --exclude='admin_settings.json' \
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

    # Restore admin_settings.json
    if [[ -n "$ADMIN_SETTINGS_TMP" && -f "$ADMIN_SETTINGS_TMP" ]]; then
        cp "$ADMIN_SETTINGS_TMP" "$INSTALL_DIR/admin_settings.json" 2>/dev/null || true
        chmod 600 "$INSTALL_DIR/admin_settings.json" 2>/dev/null || true
        rm -f "$ADMIN_SETTINGS_TMP"
        info "Restored admin_settings.json"
    fi

    chown -R "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR"
    # B2 fix: ensure DB and settings are 600 (not 644) and dir is 750
    find "$INSTALL_DIR" -maxdepth 1 -type f \( -name "*.db" -o -name "*.sqlite3" -o -name "*.sqlite" -o -name "admin_settings.json" \) -exec chmod 600 {} \; 2>/dev/null || true
    find "$INSTALL_DIR" -maxdepth 1 -type f \( -name "*.db-wal" -o -name "*.db-shm" \) -exec chmod 600 {} \; 2>/dev/null || true
    chmod 750 "$INSTALL_DIR" 2>/dev/null || true
    log "Source deployed to $INSTALL_DIR"
}

# ─── 4. Dependencies ─────────────────────────────────────────────────────

install_dependencies() {
    step "Dependencies"

    # Testing shortcut: share a single venv across install cycles.
    #   VARDEN_REUSE_VENV=1         — keep $INSTALL_DIR/venv if it already exists (skip recreate)
    #   VARDEN_SHARED_VENV=/path    — symlink / copy a shared venv into $INSTALL_DIR/venv
    # Both avoid the ~30-60s venv+pip path when iterating on code (not for production).
    if [[ -n "${VARDEN_SHARED_VENV:-}" ]]; then
        if [[ -d "$VARDEN_SHARED_VENV/bin" && -x "$VARDEN_SHARED_VENV/bin/python" ]]; then
            rm -rf "$INSTALL_DIR/venv" 2>/dev/null || true
            # Prefer symlink (instant, shared) — fallback to copy if symlink fails
            if ln -sfn "$VARDEN_SHARED_VENV" "$INSTALL_DIR/venv" 2>/dev/null; then
                log "Shared venv symlinked: $VARDEN_SHARED_VENV -> $INSTALL_DIR/venv"
            else
                cp -a "$VARDEN_SHARED_VENV" "$INSTALL_DIR/venv" >> "$LOG_FILE" 2>&1
                log "Shared venv copied: $VARDEN_SHARED_VENV -> $INSTALL_DIR/venv"
            fi
            chown -h "$CURRENT_USER:$CURRENT_GROUP" "$INSTALL_DIR/venv" 2>/dev/null || true
            if "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import psycopg; import aiosqlite; import aiohttp" >> "$LOG_FILE" 2>&1; then
                log "Import check passed (shared venv)."
                return 0
            else
                warn "Shared venv failed import check — falling back to fresh venv."
            fi
        else
            warn "VARDEN_SHARED_VENV=$VARDEN_SHARED_VENV not a valid venv — ignoring."
        fi
    fi

    if [[ "${VARDEN_REUSE_VENV:-}" == "1" || "${VARDEN_REUSE_VENV:-}" == "true" ]]; then
        if [[ -d "$INSTALL_DIR/venv" && -x "$INSTALL_DIR/venv/bin/python" ]]; then
            if "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import psycopg; import aiosqlite; import aiohttp" >> "$LOG_FILE" 2>&1; then
                log "Reusing existing venv at $INSTALL_DIR/venv (VARDEN_REUSE_VENV=1, 0s)"
                return 0
            fi
            warn "Existing venv failed import check — recreating."
        else
            info "VARDEN_REUSE_VENV=1 but no venv at $INSTALL_DIR/venv — creating fresh."
        fi
    fi

    # Recreate venv cleanly to avoid stale packages (backup DB already done)
    if [[ -d "$INSTALL_DIR/venv" && ! -L "$INSTALL_DIR/venv" ]]; then
        rm -rf "$INSTALL_DIR/venv" 2>/dev/null || true
    elif [[ -L "$INSTALL_DIR/venv" ]]; then
        rm -f "$INSTALL_DIR/venv" 2>/dev/null || true
    fi
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

    if "$INSTALL_DIR/venv/bin/python" -c "import telegram; import sqlalchemy; import psycopg; import aiosqlite; import aiohttp" >> "$LOG_FILE" 2>&1; then
        log "Import check passed."
    else
        err "Package verification failed. See $LOG_FILE"
        exit 1
    fi

    # If shared-venv mode, populate the shared location for next runs
    if [[ -n "${VARDEN_SHARED_VENV:-}" && ! -e "$VARDEN_SHARED_VENV" ]]; then
        mkdir -p "$(dirname "$VARDEN_SHARED_VENV")" 2>/dev/null || true
        cp -a "$INSTALL_DIR/venv" "$VARDEN_SHARED_VENV" >> "$LOG_FILE" 2>&1 || true
        chown -R "$CURRENT_USER:$CURRENT_GROUP" "$VARDEN_SHARED_VENV" 2>/dev/null || true
        info "Populated shared venv at $VARDEN_SHARED_VENV for reuse."
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
    # Graceful stop only — avoid SIGKILL which can corrupt SQLite WAL.
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    # Only kill stale processes if systemd stop failed; use exact match without regex injection.
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        # Service still active after stop — do not pkill; let systemd handle it.
        warn "Service still active after stop; not forcing pkill to avoid DB corruption."
    else
        # Clean up stale lock if no process holds it (flock is advisory, safe to remove)
        rm -f /tmp/vardenproxybot.lock 2>/dev/null || true
    fi
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
