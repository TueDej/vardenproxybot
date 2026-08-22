import os
from dataclasses import dataclass, field
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)
    database_url: str = "sqlite+aiosqlite:///vardenproxy.db"
    auto_approve: bool = False

    # Mock payment details
    mock_card_number: str = "4242 4242 4242 4242"
    mock_card_holder: str = "TEST USER"
    mock_crypto_wallet: str = "0xMockWalletAddressForDemoPurposes1234"

    # VPN Node
    vpn_server_host: str = "vardenproxy.example.com"

    # SOCKS5 Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080
    proxy_user: str = ""
    proxy_pass: str = ""

    def __post_init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "")
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        if admin_ids_str:
            self.admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
        self.database_url = os.getenv("DATABASE_URL", self.database_url)
        self.auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"

        # Proxy settings
        self.proxy_host = os.getenv("PROXY_HOST", self.proxy_host)
        self.proxy_port = int(os.getenv("PROXY_PORT", self.proxy_port))
        self.proxy_user = os.getenv("PROXY_USER", self.proxy_user)
        self.proxy_pass = os.getenv("PROXY_PASS", self.proxy_pass)

    @property
    def proxy_url(self) -> str | None:
        """Build authenticated SOCKS5 proxy URL for httpx, or None if no credentials."""
        if not self.proxy_user and not self.proxy_pass:
            return f"socks5://{self.proxy_host}:{self.proxy_port}"
        return f"socks5://{quote(self.proxy_user)}:{quote(self.proxy_pass)}@{self.proxy_host}:{self.proxy_port}"


config = Config()
