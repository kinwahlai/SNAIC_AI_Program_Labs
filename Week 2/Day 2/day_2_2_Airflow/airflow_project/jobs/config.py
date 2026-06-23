from pathlib import Path
import os

# Base path inside the Airflow container. The environment variable is useful
# when testing these functions outside Docker.
BASE_DIR = Path(os.getenv("AIRFLOW_PROJECT_DIR", "/opt/airflow"))

DATA_DIR = BASE_DIR / "data"
STAGING_DIR = DATA_DIR / "staging"
INTERMEDIATE_DIR = DATA_DIR / "intermediate"
MARTS_DIR = DATA_DIR / "marts"
DASHBOARD_DIR = BASE_DIR / "dashboard"

DB_PATH = DATA_DIR / "basic_ingestion.duckdb"
GEOJSON_FILE_NAME = "MasterPlan2019SubzoneBoundaryNoSeaGEOJSON.geojson"


def resolve_geojson_path() -> Path:
    configured_path = os.getenv("PLANNING_AREA_GEOJSON_PATH")
    if configured_path:
        return Path(configured_path)

    candidates = [
        BASE_DIR / "dashboard" / "shared_assets" / GEOJSON_FILE_NAME,
        BASE_DIR / "jobs" / "shared_assets" / GEOJSON_FILE_NAME,
        Path(__file__).resolve().parent / "shared_assets" / GEOJSON_FILE_NAME,
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


GEOJSON_PATH = resolve_geojson_path()

TAXI_API_URL = os.getenv(
    "TAXI_API_URL",
    "https://api.data.gov.sg/v1/transport/taxi-availability",
)
RAINFALL_API_URL = os.getenv(
    "RAINFALL_API_URL",
    "https://api-open.data.gov.sg/v2/real-time/api/rainfall",
)


def ensure_dirs():
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    INTERMEDIATE_DIR.mkdir(parents=True, exist_ok=True)
    MARTS_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
