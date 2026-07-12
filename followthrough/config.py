from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / ".config" / "followthrough" / "secrets.env")
load_dotenv(Path.home() / ".config" / "hai" / ".env")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, env_prefix="FOLLOWTHROUGH_", extra="ignore", populate_by_name=True
    )

    public_url: str = "http://127.0.0.1:18765"
    host: str = "0.0.0.0"
    port: int = 18765
    db_path: Path = ROOT / "data" / "followthrough.db"
    archive_db_path: Path = ROOT / "data" / "archive" / "archive.db"
    jobs_dir: Path = ROOT / "data" / "jobs"
    runner_dir: Path = ROOT / "data" / "runner"
    runner_receipts_dir: Path = ROOT / "data" / "runner" / "receipts"
    effects_dir: Path = ROOT / "data" / "effects"
    effect_policy_file: Path = Path.home() / ".config" / "followthrough" / "effect-policy.json"
    google_token_file: Path = Path.home() / ".hermes" / "user" / "google-workspace" / "google_token.json"
    google_client_secret_file: Path = Path.home() / ".hermes" / "user" / "google-workspace" / "google_client_secret.json"
    audio_dir: Path = ROOT / "data" / "archive" / "audio"
    max_transcript_bytes: int = 65_536
    max_audio_chunk_bytes: int = 8_388_608
    # Upper bound on a single event's audio chunk index. Bounds the manifest's
    # dense-range continuity scan so a sparse sequence cannot force a huge
    # allocation.
    max_audio_sequence: int = 100_000
    discord_target: str = "discord:1510104161612730378"
    auto_send: bool = True
    hermes_bin: str = "hermes"
    hermes_timeout_seconds: int = 55
    kanban_enabled: bool = True
    kanban_board: str = "followthrough"
    kanban_poll_seconds: float = 5.0
    kanban_cli_timeout_seconds: int = 30
    emergency_control_rto_seconds: int = 10
    hermes_python: Path = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    h_api_key: str = Field("", validation_alias="HAI_API_KEY")
    h_api_base: str = "https://agp.eu.hcompany.ai/api/v2"
    h_agent: str = "h/web-surfer-flash"
    h_poll_seconds: float = 1.0
    h_max_steps: int = 30
    h_max_time_seconds: int = 300
    orgo_api_key: str = Field("", validation_alias="ORGO_API_KEY")
    orgo_default_computer_id: str = Field("", validation_alias="ORGO_DEFAULT_COMPUTER_ID")
    orgo_api_base: str = "https://www.orgo.ai/api"
    orgo_local_base: str = "http://127.0.0.1:8080"
    orgo_desktop_api_token: str = Field("", validation_alias="ORGO_DESKTOP_API_TOKEN")
    vnc_password: str = Field("", validation_alias="VNC_PASSWORD")
    orgo_action_timeout_seconds: int = 60

    def model_post_init(self, __context: object) -> None:
        self.db_path = Path(self.db_path).expanduser().resolve()
        self.archive_db_path = Path(self.archive_db_path).expanduser().resolve()
        self.jobs_dir = Path(self.jobs_dir).expanduser().resolve()
        self.runner_dir = Path(self.runner_dir).expanduser().resolve()
        self.runner_receipts_dir = Path(self.runner_receipts_dir).expanduser().resolve()
        self.effects_dir = Path(self.effects_dir).expanduser().resolve()
        self.effect_policy_file = Path(self.effect_policy_file).expanduser().resolve()
        self.google_token_file = Path(self.google_token_file).expanduser().resolve()
        self.google_client_secret_file = Path(self.google_client_secret_file).expanduser().resolve()
        self.audio_dir = Path(self.audio_dir).expanduser().resolve()
        self.hermes_python = Path(self.hermes_python).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.runner_dir.mkdir(parents=True, exist_ok=True)
        self.runner_receipts_dir.mkdir(parents=True, exist_ok=True)
        self.effects_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)

settings = Settings()
