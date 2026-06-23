"""Fetch, clean, spatially aggregate, and load rainfall readings."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache

import duckdb
import geopandas as gpd
import pandas as pd
import requests

from scripts.config import (
    CLEAN_DIR,
    DB_PATH,
    GEOJSON_PATH,
    RAINFALL_API_URL,
    RAW_DIR,
    ensure_dirs,
)


def singapore_timestamp(value: str) -> str:
    """Normalize an API timestamp before storing it in DuckDB."""
    return (
        pd.to_datetime(value, utc=True)
        .tz_convert("Asia/Singapore")
        .tz_localize(None)
        .isoformat()
    )


@lru_cache(maxsize=1)
def load_subzones() -> gpd.GeoDataFrame:
    """Load planning-area boundaries for station-to-area assignment."""
    subzones = gpd.read_file(GEOJSON_PATH)
    if subzones.crs is None:
        subzones = subzones.set_crs("EPSG:4326")
    else:
        subzones = subzones.to_crs("EPSG:4326")

    subzones = subzones[["PLN_AREA_N", "geometry"]].copy()
    subzones["planning_area"] = subzones["PLN_AREA_N"].str.strip().str.title()
    return subzones[["planning_area", "geometry"]]


def attach_area_from_coordinates(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Assign rainfall station coordinates to planning areas."""
    points = gpd.GeoDataFrame(
        dataframe.copy(),
        geometry=gpd.points_from_xy(dataframe["longitude"], dataframe["latitude"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, load_subzones(), how="left", predicate="intersects")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))


def fetch_rainfall_data() -> str:
    """Fetch raw rainfall data and save one JSON snapshot."""
    ensure_dirs()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    response = requests.get(RAINFALL_API_URL, timeout=10)
    response.raise_for_status()
    data = response.json()

    raw_path = RAW_DIR / f"rainfall_raw_{timestamp}.json"
    raw_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved raw rainfall JSON: {raw_path}")
    return str(raw_path)


def clean_rainfall_data(raw_path: str) -> str:
    """Convert the raw rainfall API response into station-level CSV rows."""
    payload = json.loads(open(raw_path, encoding="utf-8").read())
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
    reading_timestamp = singapore_timestamp(reading_block["timestamp"])

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

    dataframe = pd.DataFrame(rows).dropna(subset=["longitude", "latitude"])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    clean_path = CLEAN_DIR / f"rainfall_clean_{timestamp}.csv"
    dataframe.to_csv(clean_path, index=False)
    print(f"Saved clean rainfall CSV: {clean_path} ({len(dataframe)} rows)")
    return str(clean_path)


def aggregate_rainfall_by_area(clean_path: str) -> str:
    """Join rainfall stations to planning areas and aggregate by area."""
    dataframe = pd.read_csv(clean_path)
    joined = attach_area_from_coordinates(dataframe)
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

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    area_path = CLEAN_DIR / f"rainfall_area_{timestamp}.csv"
    rainfall_by_area.to_csv(area_path, index=False)
    print(f"Saved rainfall area CSV: {area_path} ({len(rainfall_by_area)} rows)")
    return str(area_path)


def load_rainfall_to_duckdb(area_path: str) -> None:
    """Append the rainfall planning-area facts to DuckDB."""
    dataframe = pd.read_csv(area_path)

    with duckdb.connect(str(DB_PATH)) as connection:
        connection.execute(
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
        connection.register("rainfall_df", dataframe)
        connection.execute(
            """
            DELETE FROM rainfall_area
            WHERE reading_timestamp IN (
                SELECT DISTINCT CAST(reading_timestamp AS TIMESTAMP)
                FROM rainfall_df
            )
            """
        )
        connection.execute(
            """
            INSERT INTO rainfall_area
            SELECT
                CAST(reading_timestamp AS TIMESTAMP),
                planning_area,
                CAST(rainfall_mm AS DOUBLE),
                CAST(station_count AS INTEGER),
                CAST(raining_station_count AS INTEGER)
            FROM rainfall_df
            """
        )
        row_count = connection.execute("SELECT COUNT(*) FROM rainfall_area").fetchone()[0]

    print(f"Loaded rainfall data into DuckDB: {DB_PATH} ({row_count} total rows)")


def run_rainfall_pipeline() -> None:
    """Run one complete rainfall ingestion cycle."""
    raw_path = fetch_rainfall_data()
    clean_path = clean_rainfall_data(raw_path)
    area_path = aggregate_rainfall_by_area(clean_path)
    load_rainfall_to_duckdb(area_path)


if __name__ == "__main__":
    run_rainfall_pipeline()
