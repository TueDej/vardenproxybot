import os
from dataclasses import dataclass, field

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

    def __post_init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "")
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        if admin_ids_str:
            self.admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
        self.database_url = os.getenv("DATABASE_URL", self.database_url)
        self.auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"


config = Config()
