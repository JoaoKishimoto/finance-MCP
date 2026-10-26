"""Environment-based settings. Every value comes from the .env file (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # Pluggy
    pluggy_client_id: str
    pluggy_client_secret: str
    pluggy_item_ids: list[str]
    pluggy_base_url: str = "https://api.pluggy.ai"

    # Market data
    brapi_token: str = ""

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"

    # Auth: "github" (production) or "none" (local testing only, loopback host required)
    auth_mode: str = "github"
    github_client_id: str = ""
    github_client_secret: str = ""
    allowed_github_users: list[str] = field(default_factory=list)
    jwt_signing_key: str = ""

    # Storage
    data_dir: Path = Path("/data")
    timezone: str = "America/Sao_Paulo"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.yaml"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def public_host(self) -> str:
        return self.public_base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


def _load_dotenv(path: Path) -> None:
    """For local runs: read ./.env if present. Real env vars win. (In Docker, compose's env_file is used.)"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_settings() -> Settings:
    _load_dotenv(Path(".env"))
    env = os.environ
    missing = [k for k in ("PLUGGY_CLIENT_ID", "PLUGGY_CLIENT_SECRET") if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        pluggy_client_id=env["PLUGGY_CLIENT_ID"],
        pluggy_client_secret=env["PLUGGY_CLIENT_SECRET"],
        pluggy_item_ids=_list(env.get("PLUGGY_ITEM_IDS")),
        pluggy_base_url=env.get("PLUGGY_BASE_URL", "https://api.pluggy.ai").rstrip("/"),
        brapi_token=env.get("BRAPI_TOKEN", ""),
        host=env.get("HOST", "0.0.0.0"),
        port=int(env.get("PORT", "8000")),
        public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        auth_mode=env.get("AUTH_MODE", "github").lower(),
        github_client_id=env.get("GITHUB_CLIENT_ID", ""),
        github_client_secret=env.get("GITHUB_CLIENT_SECRET", ""),
        allowed_github_users=[u.lower() for u in _list(env.get("ALLOWED_GITHUB_USERS"))],
        jwt_signing_key=env.get("JWT_SIGNING_KEY", ""),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        timezone=env.get("TZ", "America/Sao_Paulo"),
    )
