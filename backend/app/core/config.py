import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from app.outpatient import DEFAULT_DATA_PATH

_JWT_SECRET_FILE = Path(__file__).resolve().parents[3] / ".runtime" / "jwt-secret"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    outpatient_data_path: Path
    auth_enabled: bool = False
    jwt_secret_key: str = field(default="", repr=False)
    jwt_expire_minutes: str = "480"


def _jwt_secret_key() -> str:
    configured = os.environ.get("JWT_SECRET_KEY")
    if configured is not None:
        return configured
    try:
        directory = os.open(_JWT_SECRET_FILE.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(_JWT_SECRET_FILE.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_nlink != 1):
                return ""
            raw = stream.read(1025)
        return raw.decode("utf-8").rstrip("\r\n") if len(raw) <= 1024 else ""
    except (OSError, UnicodeError):
        return ""


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        auth_enabled=os.environ.get("AUTH_ENABLED") == "1",
        jwt_secret_key=_jwt_secret_key(),
        jwt_expire_minutes=os.environ.get("JWT_EXPIRE_MINUTES", "").strip() or "480",
        outpatient_data_path=Path(os.environ.get("OUTPATIENT_DATA_PATH", DEFAULT_DATA_PATH)),
    )
