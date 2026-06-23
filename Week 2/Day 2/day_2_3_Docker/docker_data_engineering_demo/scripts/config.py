"""Container-friendly paths and settings for the ingestion pipelines."""

from __future__ import annotations

import os
from pathlib import Path


# All pipeline code reads paths from this one file.
#
# In plain Python, DATA_DIR defaults to ./data relative to the current project.
# In Docker, the lesson bind-mounts the host data folder into /app/data, and the
# same relative path still works because Dockerfile.pipeline sets WORKDIR /app.
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
CLEAN_DIR = DATA_DIR / "clean"
DB_PATH = DATA_DIR / "basic_ingestion.duckdb"

# These URLs can be overridden from docker run or Docker Compose with
# environment variables. The code stays the same; runtime configuration changes.
TAXI_API_URL = os.getenv(
    "TAXI_API_URL",
    "https://api.data.gov.sg/v1/transport/taxi-availability",
)
RAINFALL_API_URL = os.getenv(
    "RAINFALL_API_URL",
    "https://api-open.data.gov.sg/v2/real-time/api/rainfall",
)
GEOJSON_PATH = Path(os.getenv("GEOJSON_PATH", "reference/planning-areas.geojson"))


def ensure_dirs() -> None:
    """Create the folders that may not exist in a fresh container."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
