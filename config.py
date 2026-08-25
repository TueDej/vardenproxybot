import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var; warn and fall back to the default on garbage input."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("Invalid %s=%r is not an integer — using %s instead.", name, raw, default)
        return default


@dataclass
class Config:
    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)
    database_url: str = "sqlite+aiosqlite:///vardenproxy.db"

    # 3x-ui Panel
    panel_url: str = ""
    panel_api_token: str = ""
    xui_inbound_id: int = 0
    vpn_limit_ip: int = 2
    panel_verify_ssl: bool = True

    # SOCKS5 Proxy (enabled by default; set PROXY_DISABLED=true to disable)
    proxy_disabled: bool = False
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080
    proxy_user: str = ""
    proxy_pass: str = ""

    # Zarinpal payment gateway
    zarinpal_access_token: str = ""  # merchant/terminal UUID
    zarinpal_callback_url: str = ""
    zarinpal_sandbox: bool = False
    zarinpal_bind_host: str = "127.0.0.1"
    zarinpal_bind_port: int = 8099

    # Admin panel (BasicAuth, served on same HTTP server)
    admin_panel_user: str = "admin"
    admin_panel_pass: str = ""

    def __post_init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "")
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        if admin_ids_str:
            self.admin_ids = [
                int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()
            ]
        self.database_url = os.getenv("DATABASE_URL", self.database_url)

        # Proxy settings
        self.proxy_disabled = os.getenv(
            "PROXY_DISABLED", str(self.proxy_disabled)
        ).strip().lower() in (
            "true",
            "1",
            "yes",
            "on",
        )
        self.proxy_host = os.getenv("PROXY_HOST", self.proxy_host)
        self.proxy_port = _int_env("PROXY_PORT", self.proxy_port)
        self.proxy_user = os.getenv("PROXY_USER", self.proxy_user)
        self.proxy_pass = os.getenv("PROXY_PASS", self.proxy_pass)

        # Zarinpal payment gateway
        self.zarinpal_access_token = os.getenv("ZARINPAL_ACCESS_TOKEN", "")
        self.zarinpal_callback_url = os.getenv("ZARINPAL_CALLBACK_URL", "").rstrip("/")
        self.zarinpal_sandbox = os.getenv("ZARINPAL_SANDBOX", "false").strip().lower() == "true"
        self.zarinpal_bind_host = os.getenv("ZARINPAL_BIND_HOST", self.zarinpal_bind_host)
        self.zarinpal_bind_port = _int_env("ZARINPAL_BIND_PORT", self.zarinpal_bind_port)

        # Admin panel
        self.admin_panel_user = os.getenv("ADMIN_PANEL_USER", self.admin_panel_user)
        self.admin_panel_pass = os.getenv("ADMIN_PANEL_PASS", self.admin_panel_pass)

        # 3x-ui panel settings
        self.panel_url = os.getenv("PANEL_URL", "").rstrip("/")
        self.panel_api_token = os.getenv("PANEL_API_TOKEN", "")
        self.xui_inbound_id = _int_env("XUI_INBOUND_ID", 0)
        self.vpn_limit_ip = _int_env("VPN_LIMIT_IP", self.vpn_limit_ip)
        self.panel_verify_ssl = os.getenv("PANEL_VERIFY_SSL", "true").strip().lower() == "true"

    @property
    def panel_configured(self) -> bool:
        return bool(self.panel_url and self.panel_api_token and self.xui_inbound_id)

    @property
    def zarinpal_configured(self) -> bool:
        return bool(self.zarinpal_access_token and self.zarinpal_callback_url)

    @property
    def zarinpal_base_url(self) -> str:
        if self.zarinpal_sandbox:
            return "https://sandbox.zarinpal.com"
        return "https://payment.zarinpal.com"

    @property
    def proxy_url(self) -> str | None:
        """Build authenticated SOCKS5 proxy URL for httpx, or None if disabled."""
        if self.proxy_disabled:
            return None
        if not self.proxy_host:
            return None
        # Handle partial credentials (only one set) — quote safely, omit empty part.
        if not self.proxy_user and not self.proxy_pass:
            return f"socks5://{self.proxy_host}:{self.proxy_port}"
        user = quote(self.proxy_user, safe="")
        pwd = quote(self.proxy_pass, safe="")
        if self.proxy_user and self.proxy_pass:
            return f"socks5://{user}:{pwd}@{self.proxy_host}:{self.proxy_port}"
        if self.proxy_user:
            return f"socks5://{user}@{self.proxy_host}:{self.proxy_port}"
        return f"socks5://:{pwd}@{self.proxy_host}:{self.proxy_port}"

    @property
    def proxy_url_redacted(self) -> str | None:
        """Proxy URL safe for logs — credentials masked."""
        url = self.proxy_url
        if not url:
            return None
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{netloc}"
        return parts._replace(netloc=netloc).geturl()

    @property
    def admin_panel_enabled(self) -> bool:
        return bool(self.admin_panel_user and self.admin_panel_pass)


config = Config()
