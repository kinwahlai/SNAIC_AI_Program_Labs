from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
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


def find_day1_base_dir() -> Path:
    for path in [SCRIPT_DIR, *SCRIPT_DIR.parents]:
        if (path / "shared_data").exists() or (path / "shared_assets").exists():
            return path
    return SCRIPT_DIR.parent


BASE_DIR = find_day1_base_dir()
RAW_DIR = BASE_DIR / "data" / "raw"
SHARED_DATA_DIR = BASE_DIR / "shared_data"
SHARED_ASSETS_DIR = BASE_DIR / "shared_assets"
DB_PATH = SHARED_DATA_DIR / "simple_live_serving.duckdb"
GEOJSON_PATH = SHARED_ASSETS_DIR / "MasterPlan2019SubzoneBoundaryNoSeaGEOJSON.geojson"
FRONTEND_PATH = SCRIPT_DIR / "simple_live_dashboard.html"
LATEST_MAP_PATH = SCRIPT_DIR / "simple_live_latest_map.png"

TAXI_API_URL = "https://api.data.gov.sg/v1/transport/taxi-availability"
RAINFALL_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
REQUEST_TIMEOUT_SECONDS = 30

app = FastAPI(title="Simple Live Taxi and Rainfall Serving")

_subzones_cache: gpd.GeoDataFrame | None = None
_planning_area_geojson_cache: dict[str, Any] | None = None


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


def payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def clean_area_name(value: Any) -> str:
    return str(value).strip().title()


def load_subzones() -> gpd.GeoDataFrame:
    global _subzones_cache
    if _subzones_cache is not None:
        return _subzones_cache

    subzones = gpd.read_file(GEOJSON_PATH)
    if subzones.crs is None:
        subzones = subzones.set_crs("EPSG:4326")
    else:
        subzones = subzones.to_crs("EPSG:4326")

    subzones = subzones[["PLN_AREA_N", "geometry"]].copy()
    subzones["planning_area"] = subzones["PLN_AREA_N"].map(clean_area_name)
    _subzones_cache = subzones[["planning_area", "geometry"]]
    return _subzones_cache


def planning_area_geojson() -> dict[str, Any]:
    global _planning_area_geojson_cache
    if _planning_area_geojson_cache is not None:
        return json.loads(json.dumps(_planning_area_geojson_cache))

    planning_areas = load_subzones().dissolve(by="planning_area", as_index=False)
    _planning_area_geojson_cache = json.loads(planning_areas.to_json())
    return json.loads(json.dumps(_planning_area_geojson_cache))


def attach_area_from_coordinates(
    df: pd.DataFrame,
    longitude_col: str = "longitude",
    latitude_col: str = "latitude",
) -> pd.DataFrame:
    points = gpd.GeoDataFrame(
        df.copy(),
        geometry=gpd.points_from_xy(df[longitude_col], df[latitude_col]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, load_subzones(), how="left", predicate="intersects")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))


def extract_taxi_counts(payload: dict[str, Any]) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    features = payload.get("features", [])
    if not features:
        raise ValueError("Taxi API response has no features.")

    feature = features[0]
    timestamp = feature.get("properties", {}).get("timestamp")
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    if not timestamp:
        raise ValueError("Taxi API response has no timestamp.")

    taxi_points = pd.DataFrame(coordinates, columns=["longitude", "latitude"])
    taxi_points = taxi_points.dropna(subset=["longitude", "latitude"]).reset_index(drop=True)
    taxi_with_area = attach_area_from_coordinates(taxi_points)

    taxi_counts = (
        taxi_with_area.dropna(subset=["planning_area"])
        .groupby("planning_area", as_index=False)
        .size()
        .rename(columns={"size": "available_taxi_count"})
        .sort_values("planning_area")
        .reset_index(drop=True)
    )
    taxi_counts["available_taxi_count"] = taxi_counts["available_taxi_count"].astype(int)
    return to_iso(parse_api_timestamp(timestamp)), taxi_counts, taxi_points


def extract_rainfall_by_area(payload: dict[str, Any]) -> tuple[str, pd.DataFrame]:
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
                "station_name": station.get("name"),
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
    rainfall_by_area["raining_station_count"] = rainfall_by_area["raining_station_count"].astype(int)
    return to_iso(parse_api_timestamp(reading_timestamp)), rainfall_by_area


def init_database() -> None:
    SHARED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS simple_taxi_area (
                api_timestamp VARCHAR NOT NULL,
                planning_area VARCHAR NOT NULL,
                available_taxi_count INTEGER NOT NULL,
                collected_at VARCHAR NOT NULL,
                payload_hash VARCHAR NOT NULL,
                UNIQUE(api_timestamp, planning_area)
            );

            CREATE TABLE IF NOT EXISTS simple_rainfall_area (
                reading_timestamp VARCHAR NOT NULL,
                planning_area VARCHAR NOT NULL,
                rainfall_mm DOUBLE NOT NULL,
                station_count INTEGER NOT NULL,
                raining_station_count INTEGER NOT NULL,
                collected_at VARCHAR NOT NULL,
                payload_hash VARCHAR NOT NULL,
                UNIQUE(reading_timestamp, planning_area)
            );

            CREATE TABLE IF NOT EXISTS simple_refresh_runs (
                refresh_started_at VARCHAR NOT NULL,
                refresh_ended_at VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                taxi_rows INTEGER NOT NULL,
                rainfall_rows INTEGER NOT NULL,
                message VARCHAR
            );
            """
        )


def draw_latest_map(taxi_points: pd.DataFrame, taxi_timestamp: str, rainfall_timestamp: str) -> None:
    planning_areas = load_subzones().dissolve(by="planning_area", as_index=False)
    rainfall_rows = latest_rows()[1]
    rainfall_by_area = pd.DataFrame(rainfall_rows)
    if len(rainfall_by_area):
        planning_areas = planning_areas.merge(
            rainfall_by_area[["planning_area", "rainfall_mm"]],
            on="planning_area",
            how="left",
        )
    else:
        planning_areas["rainfall_mm"] = 0.0

    def rainfall_color(value: Any) -> str:
        # Rainfall API values are 5-minute totals. Convert to an approximate hourly intensity.
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
    taxi_points_gdf.plot(
        ax=ax,
        color="#d7191c",
        markersize=9,
        alpha=0.45,
    )

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


def save_latest_to_database(
    taxi_timestamp: str,
    taxi_counts: pd.DataFrame,
    taxi_hash: str,
    rainfall_timestamp: str,
    rainfall_by_area: pd.DataFrame,
    rainfall_hash: str,
    refresh_started_at: datetime,
) -> None:
    collected_at = to_iso(now_sgt())
    taxi_rows = [
        (
            taxi_timestamp,
            row.planning_area,
            int(row.available_taxi_count),
            collected_at,
            taxi_hash,
        )
        for row in taxi_counts.itertuples(index=False)
    ]
    rainfall_rows = [
        (
            rainfall_timestamp,
            row.planning_area,
            float(row.rainfall_mm),
            int(row.station_count),
            int(row.raining_station_count),
            collected_at,
            rainfall_hash,
        )
        for row in rainfall_by_area.itertuples(index=False)
    ]

    with duckdb.connect(str(DB_PATH)) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO simple_taxi_area (
                api_timestamp, planning_area, available_taxi_count,
                collected_at, payload_hash
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            taxi_rows,
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO simple_rainfall_area (
                reading_timestamp, planning_area, rainfall_mm, station_count,
                raining_station_count, collected_at, payload_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rainfall_rows,
        )
        conn.execute(
            """
            INSERT INTO simple_refresh_runs VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                to_iso(refresh_started_at),
                to_iso(now_sgt()),
                "success",
                len(taxi_rows),
                len(rainfall_rows),
                "Fetched taxi and rainfall APIs, cleaned, stored, and served latest snapshot.",
            ),
        )


def collect_once() -> None:
    init_database()
    refresh_started_at = now_sgt()
    try:
        taxi_payload = fetch_json(TAXI_API_URL)
        rainfall_payload = fetch_json(RAINFALL_API_URL)

        taxi_timestamp, taxi_counts, taxi_points = extract_taxi_counts(taxi_payload)
        rainfall_timestamp, rainfall_by_area = extract_rainfall_by_area(rainfall_payload)

        save_latest_to_database(
            taxi_timestamp=taxi_timestamp,
            taxi_counts=taxi_counts,
            taxi_hash=payload_hash(taxi_payload),
            rainfall_timestamp=rainfall_timestamp,
            rainfall_by_area=rainfall_by_area,
            rainfall_hash=payload_hash(rainfall_payload),
            refresh_started_at=refresh_started_at,
        )
        draw_latest_map(taxi_points, taxi_timestamp, rainfall_timestamp)
    except Exception as exc:
        with duckdb.connect(str(DB_PATH)) as conn:
            conn.execute(
                "INSERT INTO simple_refresh_runs VALUES (?, ?, ?, ?, ?, ?)",
                (to_iso(refresh_started_at), to_iso(now_sgt()), "failed", 0, 0, str(exc)),
            )
        raise


def latest_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    init_database()
    with duckdb.connect(str(DB_PATH), read_only=True) as conn:
        taxi = conn.execute(
            """
            SELECT api_timestamp, planning_area, available_taxi_count, collected_at
            FROM simple_taxi_area
            WHERE api_timestamp = (SELECT MAX(api_timestamp) FROM simple_taxi_area)
            ORDER BY planning_area
            """
        ).df()
        rainfall = conn.execute(
            """
            SELECT
                reading_timestamp,
                planning_area,
                rainfall_mm,
                station_count,
                raining_station_count,
                collected_at
            FROM simple_rainfall_area
            WHERE reading_timestamp = (SELECT MAX(reading_timestamp) FROM simple_rainfall_area)
            ORDER BY planning_area
            """
        ).df()
        last_run = conn.execute(
            """
            SELECT *
            FROM simple_refresh_runs
            ORDER BY refresh_started_at DESC
            LIMIT 1
            """
        ).df()

    return (
        taxi.to_dict(orient="records"),
        rainfall.to_dict(orient="records"),
        last_run.to_dict(orient="records")[0] if len(last_run) else {},
    )


def map_payload() -> dict[str, Any]:
    taxi_rows, rainfall_rows, last_run = latest_rows()
    taxi_by_area = {row["planning_area"]: row for row in taxi_rows}
    rainfall_by_area = {row["planning_area"]: row for row in rainfall_rows}

    geojson = planning_area_geojson()
    total_taxis = 0
    raining_area_count = 0

    for feature in geojson["features"]:
        properties = feature.setdefault("properties", {})
        area = properties.get("planning_area")
        taxi = taxi_by_area.get(area)
        rainfall = rainfall_by_area.get(area)

        available_taxi_count = int(taxi["available_taxi_count"]) if taxi else 0
        rainfall_mm = float(rainfall["rainfall_mm"]) if rainfall else 0.0
        rainfall_rate_mm_hr = rainfall_mm * 12
        raining_station_count = int(rainfall["raining_station_count"]) if rainfall else 0
        station_count = int(rainfall["station_count"]) if rainfall else 0

        properties["available_taxi_count"] = available_taxi_count
        properties["rainfall_mm"] = rainfall_mm
        properties["rainfall_rate_mm_hr"] = round(rainfall_rate_mm_hr, 2)
        properties["station_count"] = station_count
        properties["raining_station_count"] = raining_station_count
        properties["is_raining"] = rainfall_rate_mm_hr >= 0.5

        total_taxis += available_taxi_count
        if raining_station_count > 0:
            raining_area_count += 1

    latest_taxi_timestamp = taxi_rows[0]["api_timestamp"] if taxi_rows else None
    latest_rainfall_timestamp = rainfall_rows[0]["reading_timestamp"] if rainfall_rows else None

    return {
        "summary": {
            "total_taxis": total_taxis,
            "raining_area_count": raining_area_count,
            "area_count": len(geojson["features"]),
            "taxi_timestamp": latest_taxi_timestamp,
            "rainfall_timestamp": latest_rainfall_timestamp,
            "map_image_url": f"/map.png?ts={datetime.now().timestamp()}",
            "last_run": last_run,
        },
        "geojson": geojson,
    }


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
        collect_once()
        return map_payload()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/current")
def current() -> dict[str, Any]:
    return map_payload()
