from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / ".config" / "followthrough" / "secrets.env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    public_url: str = "http://127.0.0.1:18765"
    host: str = "127.0.0.1"
    port: int = 18765
    db_path: Path = ROOT / "data" / "followthrough.db"
    reports_dir: Path = ROOT / "data" / "reports"
    webhook_token: str = ""
    discord_target: str = "discord:1510104161612730378"
    auto_send: bool = False
    hermes_bin: str = "hermes"
    hermes_timeout_seconds: int = 55

    convex_url: str = ""
    convex_deploy_key: str = ""
    linkup_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    dodo_payments_api_key: str = ""
    dodo_product_id: str = ""
    dodo_payments_environment: str = "test_mode"
    dodo_payments_webhook_key: str = ""

    def model_post_init(self, __context: object) -> None:
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.reports_dir = Path(self.reports_dir).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
