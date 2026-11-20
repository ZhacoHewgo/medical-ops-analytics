import os
from dataclasses import dataclass, field
from pathlib import Path

from app.outpatient import DEFAULT_DATA_PATH


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    outpatient_data_path: Path
    auth_enabled: bool = False
    jwt_secret_key: str = field(default="", repr=False)
    jwt_expire_minutes: str = "480"


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        auth_enabled=os.environ.get("AUTH_ENABLED") == "1",
        jwt_secret_key=os.environ.get("JWT_SECRET_KEY", ""),
        jwt_expire_minutes=os.environ.get("JWT_EXPIRE_MINUTES", "480"),
        outpatient_data_path=Path(os.environ.get("OUTPATIENT_DATA_PATH", DEFAULT_DATA_PATH)),
    )
