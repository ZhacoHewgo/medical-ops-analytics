import os
from dataclasses import dataclass, field
from pathlib import Path

from app.outpatient import DEFAULT_DATA_PATH


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    outpatient_data_path: Path


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        outpatient_data_path=Path(os.environ.get("OUTPATIENT_DATA_PATH", DEFAULT_DATA_PATH)),
    )
