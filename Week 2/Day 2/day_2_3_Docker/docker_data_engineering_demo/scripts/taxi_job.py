"""Fetch, clean, and load taxi availability data.

This is adapted from the Airflow lesson's jobs/taxi_job.py. The business logic
is intentionally familiar: the Docker lesson focuses on packaging and running
an existing pipeline, not inventing a new one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pandas as pd
import requests

from scripts.config import CLEAN_DIR, DB_PATH, RAW_DIR, TAXI_API_URL, ensure_dirs


def singapore_timestamp(value: str) -> str:
    """Normalize an API timestamp before storing it in DuckDB."""
    return (
        pd.to_datetime(value, utc=True)
        .tz_convert("Asia/Singapore")
        .tz_localize(None)
        .isoformat()
    )


def fetch_taxi_data() -> str:
    """Fetch raw taxi availability data and save one JSON snapshot."""
    # A container starts with a clean filesystem. Always create output folders
    # before writing files, especially when a bind mount may be empty.
    ensure_dirs()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # This is ordinary Python networking code. Docker only changes where the
    # code runs; it does not change how requests talks to an external API.
    response = requests.get(TAXI_API_URL, timeout=10)
    response.raise_for_status()
    data = response.json()

    raw_path = RAW_DIR / f"taxi_raw_{timestamp}.json"
    raw_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved raw taxi JSON: {raw_path}")
    return str(raw_path)


def clean_taxi_data(raw_path: str) -> str:
    """Convert the raw API response into a simple tabular CSV."""
    # The Airflow DAG passes raw_path from the previous task with XCom.
    # In the Docker-only lesson, run_taxi_pipeline passes the same string directly.
    data = json.loads(open(raw_path, encoding="utf-8").read())
    api_timestamp = singapore_timestamp(
        data["features"][0]["properties"]["timestamp"]
    )
    coordinates = data["features"][0]["geometry"]["coordinates"]

    dataframe = pd.DataFrame(coordinates, columns=["longitude", "latitude"])
    dataframe.insert(0, "api_timestamp", api_timestamp)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_path = CLEAN_DIR / f"taxi_clean_{timestamp}.csv"
    dataframe.to_csv(clean_path, index=False)
    print(f"Saved clean taxi CSV: {clean_path} ({len(dataframe)} rows)")
    return str(clean_path)


def load_taxi_to_duckdb(clean_path: str) -> None:
    """Append the cleaned taxi coordinates to DuckDB."""
    dataframe = pd.read_csv(clean_path)

    # DuckDB is just a file here: data/basic_ingestion.duckdb. Because data/ is
    # bind-mounted, the API container can read the same file later.
    with duckdb.connect(str(DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS taxi_availability (
                api_timestamp TIMESTAMP,
                longitude DOUBLE,
                latitude DOUBLE
            )
            """
        )
        connection.register("taxi_df", dataframe)
        connection.execute(
            """
            INSERT INTO taxi_availability
            SELECT
                CAST(api_timestamp AS TIMESTAMP),
                longitude,
                latitude
            FROM taxi_df
            """
        )
        row_count = connection.execute(
            "SELECT COUNT(*) FROM taxi_availability"
        ).fetchone()[0]

    print(f"Loaded taxi data into DuckDB: {DB_PATH} ({row_count} total rows)")


def run_taxi_pipeline() -> None:
    """Run one complete ingestion cycle."""
    # The Dockerfile CMD runs this function once, so this image behaves like a
    # short-running batch job instead of a long-running service.
    raw_path = fetch_taxi_data()
    clean_path = clean_taxi_data(raw_path)
    load_taxi_to_duckdb(clean_path)


if __name__ == "__main__":
    run_taxi_pipeline()
