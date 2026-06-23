"""Static PNG dashboard for taxi points and rainfall intensity.

The dashboard is intentionally separate from the data API:

- api/serving_api.py reads the pipeline DuckDB file and serves taxi endpoints.
- this file calls the API, fetches rainfall, draws a PNG map, and serves HTML.

That keeps the Docker lesson focused on service boundaries instead of putting
all data logic back into the dashboard container.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse


SINGAPORE_TZ = timezone(timedelta(hours=8))

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
GEOJSON_PATH = Path(os.getenv("GEOJSON_PATH", "reference/planning-areas.geojson"))
FRONTEND_PATH = SCRIPT_DIR / "simple_live_dashboard.html"
LATEST_MAP_PATH = DATA_DIR / "simple_live_latest_map.png"

TAXI_API_URL = os.getenv("TAXI_API_URL", "http://localhost:8000/taxi-latest")
RAINFALL_API_URL = os.getenv("RAINFALL_API_URL", "http://localhost:8000/rainfall-latest")
REQUEST_TIMEOUT_SECONDS = 30

app = FastAPI(title="Docker Taxi and Rainfall Dashboard")

_subzones_cache: gpd.GeoDataFrame | None = None
_latest_payload: dict[str, Any] | None = None


def now_sgt() -> datetime:
    return datetime.now(SINGAPORE_TZ)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SINGAPORE_TZ)
    return value.astimezone(SINGAPORE_TZ).isoformat(timespec="seconds")


def parse_api_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed.astimezone(SINGAPORE_TZ)


def fetch_json(url: str) -> Any:
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def clean_area_name(value: Any) -> str:
    return str(value).strip().title()


def load_subzones() -> gpd.GeoDataFrame:
    """Load planning areas for rainfall coloring and map outlines."""
    global _subzones_cache
    if _subzones_cache is not None:
        return _subzones_cache

    if not GEOJSON_PATH.exists():
        raise FileNotFoundError(f"Planning-area GeoJSON not found: {GEOJSON_PATH}")

    subzones = gpd.read_file(GEOJSON_PATH)
    if subzones.crs is None:
        subzones = subzones.set_crs("EPSG:4326")
    else:
        subzones = subzones.to_crs("EPSG:4326")

    subzones = subzones[["PLN_AREA_N", "geometry"]].copy()
    subzones["planning_area"] = subzones["PLN_AREA_N"].map(clean_area_name)
    _subzones_cache = subzones[["planning_area", "geometry"]]
    return _subzones_cache


def attach_area_from_coordinates(
    dataframe: pd.DataFrame,
    longitude_col: str = "longitude",
    latitude_col: str = "latitude",
) -> pd.DataFrame:
    """Assign point rows to planning areas using the mounted GeoJSON."""
    points = gpd.GeoDataFrame(
        dataframe.copy(),
        geometry=gpd.points_from_xy(dataframe[longitude_col], dataframe[latitude_col]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, load_subzones(), how="left", predicate="intersects")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))


def extract_taxi_points(payload: Any) -> tuple[str, pd.DataFrame]:
    """Read taxi points from the Docker API response or the public API shape."""
    if isinstance(payload, list):
        if not payload:
            raise ValueError("Taxi API returned no rows.")

        taxi_points = pd.DataFrame(payload)[["longitude", "latitude"]]
        timestamp = str(payload[0].get("api_timestamp"))
        return timestamp, taxi_points.dropna().reset_index(drop=True)

    features = payload.get("features", [])
    if not features:
        raise ValueError("Taxi API response has no features.")

    feature = features[0]
    timestamp = feature.get("properties", {}).get("timestamp")
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    if not timestamp:
        raise ValueError("Taxi API response has no timestamp.")

    taxi_points = pd.DataFrame(coordinates, columns=["longitude", "latitude"])
    return to_iso(parse_api_timestamp(timestamp)), taxi_points.dropna().reset_index(
        drop=True
    )


def extract_rainfall_by_area(payload: Any) -> tuple[str, pd.DataFrame]:
    """Read rainfall area facts from the API response or public rainfall shape."""
    if isinstance(payload, list):
        if not payload:
            raise ValueError("Rainfall API returned no rows.")

        rainfall_by_area = pd.DataFrame(payload)
        timestamp = str(rainfall_by_area["reading_timestamp"].iloc[0])
        rainfall_by_area = rainfall_by_area[
            [
                "planning_area",
                "rainfall_mm",
                "station_count",
                "raining_station_count",
            ]
        ].copy()
        rainfall_by_area["planning_area"] = rainfall_by_area["planning_area"].map(
            clean_area_name
        )
        rainfall_by_area["rainfall_mm"] = rainfall_by_area["rainfall_mm"].fillna(0.0)
        rainfall_by_area["station_count"] = (
            rainfall_by_area["station_count"].fillna(0).astype(int)
        )
        rainfall_by_area["raining_station_count"] = (
            rainfall_by_area["raining_station_count"].fillna(0).astype(int)
        )
        return timestamp, rainfall_by_area.sort_values("planning_area").reset_index(
            drop=True
        )

    """Aggregate rainfall stations to planning areas for the color legend."""
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
    rows: list[dict[str, Any]] = []

    for reading in reading_block.get("data", []):
        station_id = str(reading.get("stationId", ""))
        station = station_lookup.get(station_id, {})
        location = station.get("location", {})
        value = float(reading.get("value") or 0)
        rows.append(
            {
                "station_id": station_id,
                "longitude": location.get("longitude"),
                "latitude": location.get("latitude"),
                "rainfall_mm": value,
                "is_raining": int(value > 0),
            }
        )

    rainfall = pd.DataFrame(rows).dropna(subset=["longitude", "latitude"])
    rainfall_with_area = attach_area_from_coordinates(rainfall)
    rainfall_by_area = (
        rainfall_with_area.dropna(subset=["planning_area"])
        .groupby("planning_area", as_index=False)
        .agg(
            rainfall_mm=("rainfall_mm", "mean"),
            station_count=("station_id", "count"),
            raining_station_count=("is_raining", "sum"),
        )
        .sort_values("planning_area")
        .reset_index(drop=True)
    )
    rainfall_by_area["rainfall_mm"] = rainfall_by_area["rainfall_mm"].round(2)
    rainfall_by_area["station_count"] = rainfall_by_area["station_count"].astype(int)
    rainfall_by_area["raining_station_count"] = rainfall_by_area[
        "raining_station_count"
    ].astype(int)
    return to_iso(parse_api_timestamp(reading_timestamp)), rainfall_by_area


def rainfall_color(value: Any) -> str:
    """Map 5-minute rainfall totals to an approximate hourly intensity color."""
    rate_mm_hr = 0 if pd.isna(value) else float(value) * 12
    if rate_mm_hr < 0.5:
        return "#9db7d3"
    if rate_mm_hr < 2:
        return "#86efac"
    if rate_mm_hr < 10:
        return "#facc15"
    if rate_mm_hr < 30:
        return "#f97316"
    return "#7e22ce"


def draw_latest_map(
    taxi_points: pd.DataFrame,
    rainfall_by_area: pd.DataFrame,
    taxi_timestamp: str,
    rainfall_timestamp: str,
) -> None:
    """Draw the static PNG used by the browser dashboard."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    planning_areas = load_subzones().dissolve(by="planning_area", as_index=False)
    planning_areas = planning_areas.merge(
        rainfall_by_area[["planning_area", "rainfall_mm"]],
        on="planning_area",
        how="left",
    )
    planning_areas["rainfall_mm"] = planning_areas["rainfall_mm"].fillna(0.0)
    planning_areas["map_color"] = planning_areas["rainfall_mm"].map(rainfall_color)

    taxi_points_gdf = gpd.GeoDataFrame(
        taxi_points.copy(),
        geometry=gpd.points_from_xy(taxi_points["longitude"], taxi_points["latitude"]),
        crs="EPSG:4326",
    )

    fig, ax = plt.subplots(figsize=(14, 9))
    planning_areas.plot(
        ax=ax,
        color=planning_areas["map_color"],
        edgecolor="#5f6f7a",
        linewidth=0.7,
        alpha=0.95,
    )
    taxi_points_gdf.plot(ax=ax, color="#d7191c", markersize=9, alpha=0.45)

    label_layer = planning_areas.copy()
    label_layer["label_point"] = label_layer.geometry.representative_point()
    for row in label_layer.itertuples():
        ax.text(
            row.label_point.x,
            row.label_point.y,
            row.planning_area,
            fontsize=8,
            ha="center",
            va="center",
            color="#26323f",
        )

    ax.set_title("Current Rainfall Areas with Available Taxi Points", fontsize=16)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    ax.grid(True, color="#dddddd", linewidth=0.5)
    ax.text(
        0.01,
        0.01,
        f"Taxi: {taxi_timestamp} | Rainfall: {rainfall_timestamp}",
        transform=ax.transAxes,
        fontsize=9,
        color="#4b5563",
        ha="left",
        va="bottom",
        bbox={"facecolor": "white", "edgecolor": "#dddddd", "alpha": 0.85},
    )

    fig.tight_layout()
    fig.savefig(LATEST_MAP_PATH, dpi=140)
    plt.close(fig)


def build_payload(
    taxi_points: pd.DataFrame,
    rainfall_by_area: pd.DataFrame,
    taxi_timestamp: str,
    rainfall_timestamp: str,
    refresh_started_at: datetime,
) -> dict[str, Any]:
    raining_area_count = int((rainfall_by_area["raining_station_count"] > 0).sum())
    area_count = int(load_subzones()["planning_area"].nunique())
    refresh_ended_at = now_sgt()

    return {
        "summary": {
            "total_taxis": int(len(taxi_points)),
            "raining_area_count": raining_area_count,
            "area_count": area_count,
            "taxi_timestamp": taxi_timestamp,
            "rainfall_timestamp": rainfall_timestamp,
            "map_image_url": f"/map.png?ts={datetime.now().timestamp()}",
            "last_run": {
                "refresh_started_at": to_iso(refresh_started_at),
                "refresh_ended_at": to_iso(refresh_ended_at),
                "status": "success",
                "message": (
                    "Fetched taxi points from the API service, fetched rainfall, "
                    "and redrew the dashboard PNG."
                ),
            },
        }
    }


def collect_once() -> dict[str, Any]:
    """Refresh the dashboard image without owning the pipeline database."""
    global _latest_payload

    refresh_started_at = now_sgt()
    taxi_payload = fetch_json(TAXI_API_URL)
    rainfall_payload = fetch_json(RAINFALL_API_URL)

    taxi_timestamp, taxi_points = extract_taxi_points(taxi_payload)
    rainfall_timestamp, rainfall_by_area = extract_rainfall_by_area(rainfall_payload)

    draw_latest_map(taxi_points, rainfall_by_area, taxi_timestamp, rainfall_timestamp)
    _latest_payload = build_payload(
        taxi_points=taxi_points,
        rainfall_by_area=rainfall_by_area,
        taxi_timestamp=taxi_timestamp,
        rainfall_timestamp=rainfall_timestamp,
        refresh_started_at=refresh_started_at,
    )
    return _latest_payload


def current_payload() -> dict[str, Any]:
    if _latest_payload is None or not LATEST_MAP_PATH.exists():
        return collect_once()
    return json.loads(json.dumps(_latest_payload))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_PATH)


@app.get("/map.png")
def map_png() -> FileResponse:
    if not LATEST_MAP_PATH.exists():
        collect_once()
    return FileResponse(LATEST_MAP_PATH, media_type="image/png")


@app.get("/api/refresh")
def refresh() -> dict[str, Any]:
    try:
        return collect_once()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/current")
def current() -> dict[str, Any]:
    return current_payload()
