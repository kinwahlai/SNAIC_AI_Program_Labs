from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI
from fastapi.responses import FileResponse


SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name.startswith(("01_", "02_", "03_", "04_")) else SCRIPT_DIR
DB_PATH = Path(
    os.getenv("WEATHER_TAXI_DB_PATH", APP_DIR / "shared_data" / "day_1_weather_taxi_data.duckdb")
)
FRONTEND_PATH = SCRIPT_DIR / "dashboard_frontend.html"

app = FastAPI(title="Day 1 Data Serving API")


def query_duckdb(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        df = con.execute(sql, params or []).df()

    # Convert timestamp columns to strings so they are easy to return as JSON.
    for column in df.columns:
        if "timestamp" in column or column.startswith("valid_period"):
            df[column] = df[column].astype(str)

    return df.to_dict(orient="records")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_PATH)


@app.get("/api/taxi/current")
def current_taxi(timestamp: str | None = None) -> list[dict[str, Any]]:
    return query_duckdb(
        """
        SELECT
            t.timestamp_30min AS api_timestamp,
            a.area AS planning_area,
            t.taxi_count AS available_taxi_count
        FROM fact_taxi AS t
        JOIN dim_area AS a
          ON t.area_id = a.area_id
        WHERE t.timestamp_30min = COALESCE(
            CAST(? AS TIMESTAMP),
            (SELECT MAX(timestamp_30min) FROM fact_taxi)
        )
        ORDER BY available_taxi_count DESC, planning_area
        """,
        [timestamp],
    )


@app.get("/api/weather/current")
def current_weather(update_timestamp: str | None = None) -> list[dict[str, Any]]:
    return query_duckdb(
        """
        SELECT
            w.record_timestamp AS api_update_timestamp,
            a.area,
            w.weather_forecast AS forecast,
            w.forecast_start_timestamp AS valid_period_start,
            w.forecast_end_timestamp AS valid_period_end
        FROM fact_weather AS w
        JOIN dim_area AS a
          ON w.area_id = a.area_id
        WHERE w.record_timestamp = COALESCE(
            CAST(? AS TIMESTAMP),
            (SELECT MAX(record_timestamp) FROM fact_weather)
        )
        ORDER BY area
        """,
        [update_timestamp],
    )


@app.get("/api/taxi/timeseries")
def taxi_timeseries(area: str = "ANG MO KIO") -> list[dict[str, Any]]:
    return query_duckdb(
        """
        SELECT
            t.timestamp_30min AS api_timestamp,
            w.weather_forecast AS forecast,
            t.taxi_count AS available_taxi_count,
            a.area AS planning_area
        FROM fact_taxi AS t
        JOIN dim_area AS a
          ON t.area_id = a.area_id
        LEFT JOIN fact_weather AS w
          ON t.area_id = w.area_id
         AND t.timestamp_30min = w.timestamp_30min
        WHERE UPPER(a.area) = UPPER(?)
        ORDER BY t.timestamp_30min
        """,
        [area],
    )


@app.get("/api/freshness")
def freshness() -> list[dict[str, Any]]:
    return query_duckdb(
        """
        SELECT
            'taxi' AS source,
            MAX(timestamp_30min) AS latest_data_timestamp,
            COUNT(*) AS row_count
        FROM fact_taxi
        UNION ALL
        SELECT
            'weather' AS source,
            MAX(record_timestamp) AS latest_data_timestamp,
            COUNT(*) AS row_count
        FROM fact_weather
        ORDER BY source
        """
    )
