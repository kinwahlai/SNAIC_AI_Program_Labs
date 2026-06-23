import json
from datetime import datetime, timezone

import duckdb
import pandas as pd
import requests

from jobs.config import (
    TAXI_API_URL,
    STAGING_DIR,
    INTERMEDIATE_DIR,
    MARTS_DIR,
    DB_PATH,
    ensure_dirs,
)
from jobs.geo_utils import attach_area_from_coordinates

# Demo override for section 19: Day 2.1 mock API flaky scenario.
#TAXI_API_URL = "http://host.docker.internal:8011/debug-lab/04-flaky/transport/taxi-availability?client_id=airflow_retry_demo"


def fetch_taxi_data():
    """
    Fetch raw taxi availability data from data.gov.sg.
    Save the raw JSON file.
    """
    ensure_dirs()

    collected_at = datetime.now(timezone.utc)
    timestamp_str = collected_at.strftime("%Y%m%d_%H%M%S")

    response = requests.get(TAXI_API_URL, timeout=10)
    response.raise_for_status()

    data = response.json()

    raw_path = STAGING_DIR / f"stg_taxi_availability_raw_{timestamp_str}.json"

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return str(raw_path)


def clean_taxi_data(raw_path: str):
    """
    Convert raw taxi JSON into a clean table.

    Output columns:
    - api_timestamp
    - longitude
    - latitude
    """
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    api_timestamp = data["features"][0]["properties"]["timestamp"]
    coordinates = data["features"][0]["geometry"]["coordinates"]

    rows = []

    for lon, lat in coordinates:
        rows.append(
            {
                "api_timestamp": api_timestamp,
                "longitude": lon,
                "latitude": lat,
            }
        )

    df = pd.DataFrame(rows)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_path = INTERMEDIATE_DIR / f"int_taxi_points_{timestamp_str}.csv"

    df.to_csv(clean_path, index=False)

    return str(clean_path)


def join_taxi_to_planning_area(clean_path: str):
    """
    Join taxi coordinates to Singapore planning areas.

    This is the transformation step that used to be hidden inside the
    dashboard code. Keeping it as its own task makes orchestration visible.
    """
    ensure_dirs()
    df = pd.read_csv(clean_path)
    joined = attach_area_from_coordinates(df)

    taxi_points_path = INTERMEDIATE_DIR / "int_taxi_points_with_area.csv"
    joined.to_csv(taxi_points_path, index=False)

    counts = (
        joined.dropna(subset=["planning_area"])
        .groupby(["api_timestamp", "planning_area"], as_index=False)
        .size()
        .rename(columns={"size": "available_taxi_count"})
        .sort_values(["api_timestamp", "planning_area"])
        .reset_index(drop=True)
    )

    counts_path = MARTS_DIR / "fct_taxi_area_counts.csv"
    counts.to_csv(counts_path, index=False)

    return str(counts_path)


def load_taxi_to_duckdb(counts_path: str):
    """
    Load taxi planning-area counts into DuckDB.
    """
    df = pd.read_csv(counts_path)
    points_path = INTERMEDIATE_DIR / "int_taxi_points_with_area.csv"
    points_df = pd.read_csv(points_path)

    conn = duckdb.connect(str(DB_PATH))

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taxi_availability (
            api_timestamp TIMESTAMP,
            longitude DOUBLE,
            latitude DOUBLE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taxi_area_counts (
            api_timestamp TIMESTAMP,
            planning_area VARCHAR,
            available_taxi_count INTEGER
        )
        """
    )

    conn.register("taxi_df", df)
    conn.register("taxi_points_df", points_df)

    # Keep a point-level table for serving APIs that need the latest taxi map.
    conn.execute(
        """
        DELETE FROM taxi_availability
        WHERE api_timestamp IN (
            SELECT DISTINCT CAST(api_timestamp AS TIMESTAMP)
            FROM taxi_points_df
        )
        """
    )
    conn.execute(
        """
        INSERT INTO taxi_availability (
            api_timestamp,
            longitude,
            latitude
        )
        SELECT
            CAST(api_timestamp AS TIMESTAMP),
            CAST(longitude AS DOUBLE),
            CAST(latitude AS DOUBLE)
        FROM taxi_points_df
        """
    )

    # Make this task rerunnable for the same API timestamp.
    conn.execute(
        """
        DELETE FROM taxi_area_counts
        WHERE api_timestamp IN (
            SELECT DISTINCT CAST(api_timestamp AS TIMESTAMP)
            FROM taxi_df
        )
        """
    )
    conn.execute(
        """
        INSERT INTO taxi_area_counts (
            api_timestamp,
            planning_area,
            available_taxi_count
        )
        SELECT
            CAST(api_timestamp AS TIMESTAMP),
            planning_area,
            CAST(available_taxi_count AS INTEGER)
        FROM taxi_df
        """
    )

    conn.close()

    print(f"Loaded taxi planning-area counts into DuckDB: {counts_path}")


def run_taxi_pipeline():
    """
    Main function called by Airflow DAG.
    """
    raw_path = fetch_taxi_data()
    clean_path = clean_taxi_data(raw_path)
    counts_path = join_taxi_to_planning_area(clean_path)
    load_taxi_to_duckdb(counts_path)
