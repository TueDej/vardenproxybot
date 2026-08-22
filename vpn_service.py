import uuid
from datetime import datetime, timedelta, timezone

from config import config


class VPNPanelService:
    """
    Stub VPN panel service. Replace methods with real API calls
    to 3x-ui, Marzban, or any other panel when going to production.
    """

    @staticmethod
    def create_user_config(user_id: int, duration_days: int) -> str:
        """Generate a mock VLESS configuration link."""
        mock_uuid = str(uuid.uuid4())
        expiry = datetime.now(timezone.utc) + timedelta(days=duration_days)
        expiry_str = expiry.strftime("%Y-%m-%d")
        label = f"VardenProxy-{user_id}-{expiry_str}"
        return f"vless://{mock_uuid}@{config.vpn_server_host}:443?encryption=none&type=tcp#{label}"

    @staticmethod
    def revoke_user_config(vpn_config: str) -> bool:
        """Stub: revoke a config in a real panel. Always succeeds here."""
        return True

    @staticmethod
    def get_server_status() -> dict:
        """Stub: return mock server health info."""
        return {
            "server": config.vpn_server_host,
            "status": "online",
            "active_users": 0,
            "max_users": 100,
        }
