"""Serve taxi and rainfall analytics from the DuckDB file created by the pipelines."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException
import geopandas as gpd
import pandas as pd


# The API image does not generate data. It reads the DuckDB file that the
# pipeline container wrote into the shared data/ bind mount.
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/basic_ingestion.duckdb"))

# Docker Compose mounts the planning-area GeoJSON as a read-only reference file.
# Using an env var keeps the path configurable between local and container runs.
GEOJSON_PATH = Path(
    os.getenv("GEOJSON_PATH", "reference/planning-areas.geojson")
)
app = FastAPI(title="Docker Data Engineering Demo API")


def query_dataframe(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Run a DuckDB query and return a dataframe."""
    # A missing database usually means the pipeline has not run yet, or the API
    # container was started without the same data/ bind mount.
    if not DATABASE_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Database not found. Run the pipeline before starting the API.",
        )

    with duckdb.connect(str(DATABASE_PATH), read_only=True) as connection:
        return connection.execute(sql, params or []).df()


def dataframe_records(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert dataframe timestamps into JSON-friendly records."""
    return dataframe.astype(object).where(pd.notna(dataframe), None).to_dict(
        orient="records"
    )


@lru_cache(maxsize=1)
def load_subzones() -> gpd.GeoDataFrame:
    """Load the planning-area boundaries used for the spatial join."""
    # Boundary data is relatively static, so cache it after the first request.
    # This also makes repeated dashboard refreshes much faster.
    if not GEOJSON_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Planning-area GeoJSON not found: {GEOJSON_PATH}",
        )

    subzones = gpd.read_file(GEOJSON_PATH)
    if "PLN_AREA_N" not in subzones.columns:
        raise HTTPException(
            status_code=500,
            detail="Planning-area GeoJSON has no PLN_AREA_N column.",
        )

    if subzones.crs is None:
        subzones = subzones.set_crs("EPSG:4326")
    else:
        subzones = subzones.to_crs("EPSG:4326")

    subzones = subzones[["PLN_AREA_N", "geometry"]].copy()
    subzones["planning_area"] = subzones["PLN_AREA_N"].str.strip().str.upper()
    return subzones[["planning_area", "geometry"]]


def spatially_join_taxi_rows(taxi_rows: pd.DataFrame) -> gpd.GeoDataFrame:
    """Assign taxi coordinates to planning areas."""
    # The source API gives points, but the dashboard wants area-level counts.
    # GeoPandas turns longitude/latitude into point geometry for the join.
    taxi_points = gpd.GeoDataFrame(
        taxi_rows,
        geometry=gpd.points_from_xy(taxi_rows["longitude"], taxi_rows["latitude"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(taxi_points, load_subzones(), how="left", predicate="intersects")
    return joined.dropna(subset=["planning_area"])


def latest_taxi_points() -> pd.DataFrame:
    """Return raw coordinates from the latest taxi snapshot."""
    return query_dataframe(
        """
        SELECT api_timestamp, longitude, latitude
        FROM taxi_availability
        WHERE api_timestamp = (SELECT MAX(api_timestamp) FROM taxi_availability)
        """
    )


def taxi_area_timeseries_dataframe() -> pd.DataFrame:
    """Spatially aggregate all stored taxi snapshots by planning area."""
    taxi_rows = query_dataframe(
        """
        SELECT api_timestamp, longitude, latitude
        FROM taxi_availability
        ORDER BY api_timestamp
        """
    )
    if taxi_rows.empty:
        raise HTTPException(status_code=404, detail="No taxi data found.")

    matched = spatially_join_taxi_rows(taxi_rows)
    counts = (
        matched.groupby(["api_timestamp", "planning_area"])
        .size()
        .rename("available_taxi_positions")
    )
    # Fill missing timestamp/area combinations with zero. Without this, areas
    # with no taxis in a snapshot would disappear from the chart data.
    timestamps = sorted(taxi_rows["api_timestamp"].unique())
    areas = sorted(load_subzones()["planning_area"].unique())
    complete_index = pd.MultiIndex.from_product(
        [timestamps, areas],
        names=["api_timestamp", "planning_area"],
    )
    return (
        counts.reindex(complete_index, fill_value=0)
        .reset_index()
        .sort_values(["api_timestamp", "planning_area"])
    )


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Docker Data Engineering Demo API"}


@app.get("/health")
def health() -> dict[str, object]:
    # This endpoint is useful during Docker debugging: it quickly tells students
    # whether the expected mounted files are visible inside the API container.
    return {
        "status": "ok",
        "database_exists": DATABASE_PATH.exists(),
        "geojson_exists": GEOJSON_PATH.exists(),
    }


@app.get("/taxi-summary")
def taxi_summary() -> dict[str, object]:
    # Summary endpoints keep dashboard code simple. The dashboard can ask for a
    # small JSON object instead of embedding SQL or DuckDB logic.
    rows = query_dataframe(
        """
        SELECT
            MAX(api_timestamp) AS latest_api_timestamp,
            COUNT(DISTINCT api_timestamp) AS snapshot_count,
            COUNT(*) FILTER (
                WHERE api_timestamp = (SELECT MAX(api_timestamp) FROM taxi_availability)
            ) AS latest_available_taxi_positions,
            COUNT(*) AS total_rows
        FROM taxi_availability
        """
    )
    return dataframe_records(rows)[0]


@app.get("/taxi-latest")
def taxi_latest() -> list[dict[str, object]]:
    """Return all coordinates from the latest taxi snapshot for map display."""
    rows = query_dataframe(
        """
        SELECT
            CAST(api_timestamp AS VARCHAR) AS api_timestamp,
            longitude,
            latitude
        FROM taxi_availability
        WHERE api_timestamp = (SELECT MAX(api_timestamp) FROM taxi_availability)
        ORDER BY longitude, latitude
        """
    )
    return dataframe_records(rows)


@app.get("/taxi-timeseries")
def taxi_timeseries() -> list[dict[str, object]]:
    rows = query_dataframe(
        """
        SELECT
            CAST(api_timestamp AS VARCHAR) AS api_timestamp,
            COUNT(*) AS available_taxi_positions
        FROM taxi_availability
        GROUP BY api_timestamp
        ORDER BY api_timestamp
        """
    )
    return dataframe_records(rows)


@app.get("/taxi-current-by-area")
def taxi_current_by_area() -> list[dict[str, object]]:
    """Return spatially joined taxi counts for the latest snapshot."""
    taxi_rows = latest_taxi_points()
    matched = spatially_join_taxi_rows(taxi_rows)
    counts = (
        matched.groupby("planning_area")
        .size()
        .rename("available_taxi_positions")
        .reset_index()
    )
    areas = pd.DataFrame(
        {"planning_area": sorted(load_subzones()["planning_area"].unique())}
    )
    areas = areas.merge(counts, on="planning_area", how="left")
    areas["available_taxi_positions"] = (
        areas["available_taxi_positions"].fillna(0).astype(int)
    )
    areas = areas.sort_values(
        ["available_taxi_positions", "planning_area"],
        ascending=[False, True],
    )
    return dataframe_records(areas)


@app.get("/taxi-area-timeseries")
def taxi_area_timeseries() -> list[dict[str, object]]:
    """Return spatially joined taxi counts for all stored snapshots."""
    return dataframe_records(taxi_area_timeseries_dataframe())


@app.get("/rainfall-latest")
def rainfall_latest() -> list[dict[str, object]]:
    """Return planning-area rainfall facts from the latest ingestion."""
    rows = query_dataframe(
        """
        SELECT
            CAST(reading_timestamp AS VARCHAR) AS reading_timestamp,
            planning_area,
            rainfall_mm,
            station_count,
            raining_station_count
        FROM rainfall_area
        WHERE reading_timestamp = (SELECT MAX(reading_timestamp) FROM rainfall_area)
        ORDER BY planning_area
        """
    )
    return dataframe_records(rows)


@app.get("/rainfall-summary")
def rainfall_summary() -> dict[str, object]:
    """Return a small ingestion summary for the rainfall pipeline."""
    rows = query_dataframe(
        """
        SELECT
            MAX(reading_timestamp) AS latest_reading_timestamp,
            COUNT(DISTINCT reading_timestamp) AS snapshot_count,
            COUNT(*) FILTER (
                WHERE reading_timestamp = (SELECT MAX(reading_timestamp) FROM rainfall_area)
            ) AS latest_planning_areas,
            SUM(raining_station_count) FILTER (
                WHERE reading_timestamp = (SELECT MAX(reading_timestamp) FROM rainfall_area)
            ) AS latest_raining_station_count,
            COUNT(*) AS total_rows
        FROM rainfall_area
        """
    )
    return dataframe_records(rows)[0]


@app.get("/rainfall-area-timeseries")
def rainfall_area_timeseries() -> list[dict[str, object]]:
    """Return rainfall facts for all stored snapshots and planning areas."""
    rows = query_dataframe(
        """
        SELECT
            CAST(reading_timestamp AS VARCHAR) AS reading_timestamp,
            planning_area,
            rainfall_mm,
            station_count,
            raining_station_count
        FROM rainfall_area
        ORDER BY reading_timestamp, planning_area
        """
    )
    return dataframe_records(rows)


@app.get("/last-updated")
def last_updated() -> dict[str, object]:
    rows = query_dataframe(
        """
        SELECT
            CAST(MAX(taxi_timestamp) AS VARCHAR) AS latest_taxi_timestamp,
            CAST(MAX(rainfall_timestamp) AS VARCHAR) AS latest_rainfall_timestamp
        FROM (
            SELECT MAX(api_timestamp) AS taxi_timestamp, NULL::TIMESTAMP AS rainfall_timestamp
            FROM taxi_availability
            UNION ALL
            SELECT NULL::TIMESTAMP AS taxi_timestamp, MAX(reading_timestamp) AS rainfall_timestamp
            FROM rainfall_area
        )
        """
    )
    return dataframe_records(rows)[0]
