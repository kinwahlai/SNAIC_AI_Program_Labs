import json
from datetime import datetime, timezone

from jobs.config import (
    RAINFALL_API_URL,
    STAGING_DIR,
    INTERMEDIATE_DIR,
    MARTS_DIR,
    DB_PATH,
    ensure_dirs,
)
from jobs.geo_utils import attach_area_from_coordinates


def fetch_rainfall_data():
    """
    Fetch raw rainfall data from data.gov.sg.
    Save the raw JSON file.
    """
    import requests  # lazy: keep DAG parse time fast

    ensure_dirs()

    collected_at = datetime.now(timezone.utc)
    timestamp_str = collected_at.strftime("%Y%m%d_%H%M%S")

    response = requests.get(RAINFALL_API_URL, timeout=10)
    response.raise_for_status()

    data = response.json()

    raw_path = STAGING_DIR / f"stg_rainfall_raw_{timestamp_str}.json"

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return str(raw_path)


def clean_rainfall_data(raw_path: str):
    """
    Convert raw rainfall JSON into station-level rainfall records.

    Output columns:
    - reading_timestamp
    - station_id
    - station_name
    - longitude
    - latitude
    - rainfall_mm
    - is_raining
    """
    import pandas as pd  # lazy: keep DAG parse time fast

    with open(raw_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    data = payload.get("data", payload)
    stations = data.get("stations", [])
    readings = data.get("readings", [])
    if not readings:
        raise ValueError("Rainfall API response has no readings.")

    station_lookup = {
        str(station.get("id") or station.get("deviceId")): station
        for station in stations
    }
    reading_block = readings[0]
    reading_timestamp = reading_block.get("timestamp")

    rows = []
    for reading in reading_block.get("data", []):
        station_id = str(reading.get("stationId", ""))
        station = station_lookup.get(station_id, {})
        location = station.get("location", {})
        value = float(reading.get("value") or 0)
        rows.append(
            {
                "reading_timestamp": reading_timestamp,
                "station_id": station_id,
                "station_name": station.get("name"),
                "longitude": location.get("longitude"),
                "latitude": location.get("latitude"),
                "rainfall_mm": value,
                "is_raining": int(value > 0),
            }
        )

    df = pd.DataFrame(rows).dropna(subset=["longitude", "latitude"])

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_path = INTERMEDIATE_DIR / f"int_rainfall_station_readings_{timestamp_str}.csv"

    df.to_csv(clean_path, index=False)

    return str(clean_path)


def join_rainfall_to_planning_area(clean_path: str):
    """
    Join rainfall stations to Singapore planning areas and aggregate by area.
    """
    import pandas as pd  # lazy: keep DAG parse time fast

    ensure_dirs()
    df = pd.read_csv(clean_path)
    joined = attach_area_from_coordinates(df)

    station_area_path = INTERMEDIATE_DIR / "int_rainfall_stations_with_area.csv"
    joined.to_csv(station_area_path, index=False)

    rainfall_by_area = (
        joined.dropna(subset=["planning_area"])
        .groupby(["reading_timestamp", "planning_area"], as_index=False)
        .agg(
            rainfall_mm=("rainfall_mm", "mean"),
            station_count=("station_id", "count"),
            raining_station_count=("is_raining", "sum"),
        )
        .sort_values(["reading_timestamp", "planning_area"])
        .reset_index(drop=True)
    )
    rainfall_by_area["rainfall_mm"] = rainfall_by_area["rainfall_mm"].round(2)
    rainfall_by_area["station_count"] = rainfall_by_area["station_count"].astype(int)
    rainfall_by_area["raining_station_count"] = rainfall_by_area[
        "raining_station_count"
    ].astype(int)

    counts_path = MARTS_DIR / "fct_rainfall_area.csv"
    rainfall_by_area.to_csv(counts_path, index=False)

    return str(counts_path)


def load_rainfall_to_duckdb(counts_path: str):
    """
    Load rainfall planning-area facts into DuckDB.
    """
    import duckdb  # lazy: keep DAG parse time fast
    import pandas as pd  # lazy: keep DAG parse time fast

    df = pd.read_csv(counts_path)

    conn = duckdb.connect(str(DB_PATH))

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rainfall_area (
            reading_timestamp TIMESTAMP,
            planning_area VARCHAR,
            rainfall_mm DOUBLE,
            station_count INTEGER,
            raining_station_count INTEGER
        )
        """
    )

    conn.register("rainfall_df", df)

    conn.execute(
        """
        DELETE FROM rainfall_area
        WHERE reading_timestamp IN (
            SELECT DISTINCT CAST(reading_timestamp AS TIMESTAMP)
            FROM rainfall_df
        )
        """
    )
    conn.execute(
        """
        INSERT INTO rainfall_area (
            reading_timestamp,
            planning_area,
            rainfall_mm,
            station_count,
            raining_station_count
        )
        SELECT
            CAST(reading_timestamp AS TIMESTAMP),
            planning_area,
            CAST(rainfall_mm AS DOUBLE),
            CAST(station_count AS INTEGER),
            CAST(raining_station_count AS INTEGER)
        FROM rainfall_df
        """
    )

    conn.close()

    print(f"Loaded rainfall planning-area facts into DuckDB: {counts_path}")


def run_rainfall_pipeline():
    """
    Main function called by Airflow DAG.
    """
    raw_path = fetch_rainfall_data()
    clean_path = clean_rainfall_data(raw_path)
    counts_path = join_rainfall_to_planning_area(clean_path)
    load_rainfall_to_duckdb(counts_path)
