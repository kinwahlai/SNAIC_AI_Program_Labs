from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import duckdb
import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(
    os.getenv("TAXI_INGESTION_DB_PATH", APP_DIR / "data" / "basic_ingestion.duckdb")
)
GEOJSON_PATHS = [
    APP_DIR / "dashboard" / "shared_assets" / "MasterPlan2019SubzoneBoundaryNoSeaGEOJSON.geojson",
    APP_DIR / "jobs" / "shared_assets" / "MasterPlan2019SubzoneBoundaryNoSeaGEOJSON.geojson",
]
MAP_PATH = APP_DIR / "dashboard" / "airflow_latest_map.png"
TAXI_POINTS_PATHS = [
    APP_DIR / "data" / "intermediate" / "int_taxi_points_with_area.csv",
    APP_DIR / "data" / "curated" / "taxi_points_with_area.csv",
]

app = FastAPI(title="Day 2 Airflow Dashboard API")


def clean_area_name(value: Any) -> str:
    return str(value).strip().title()


def geojson_path() -> Path:
    for path in GEOJSON_PATHS:
        if path.exists():
            return path
    raise HTTPException(status_code=500, detail="Planning-area GeoJSON not found.")


def query_dataframe(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail=f"DuckDB not found: {DB_PATH}")

    with duckdb.connect(str(DB_PATH), read_only=True) as conn:
        return conn.execute(sql, params or []).df()


def latest_taxi_counts() -> dict[str, Any]:
    rows = query_dataframe(
        """
        SELECT
            api_timestamp,
            planning_area,
            available_taxi_count
        FROM taxi_area_counts
        WHERE api_timestamp = (
            SELECT MAX(api_timestamp) FROM taxi_area_counts
        )
        ORDER BY available_taxi_count DESC, planning_area
        """
    )
    if rows.empty:
        raise HTTPException(status_code=404, detail="No taxi counts found.")

    rows["api_timestamp"] = rows["api_timestamp"].astype(str)
    return {
        "api_timestamp": rows["api_timestamp"].iloc[0],
        "planning_area_count": int(rows["planning_area"].nunique()),
        "total_taxis": int(rows["available_taxi_count"].sum()),
        "areas": rows.to_dict(orient="records"),
    }


def latest_rainfall() -> dict[str, Any]:
    rows = query_dataframe(
        """
        SELECT
            reading_timestamp,
            planning_area,
            rainfall_mm,
            station_count,
            raining_station_count
        FROM rainfall_area
        WHERE reading_timestamp = (
            SELECT MAX(reading_timestamp) FROM rainfall_area
        )
        ORDER BY planning_area
        """
    )
    if rows.empty:
        raise HTTPException(status_code=404, detail="No rainfall data found.")

    rows["reading_timestamp"] = rows["reading_timestamp"].astype(str)
    rows["planning_area"] = rows["planning_area"].map(clean_area_name)
    return {
        "reading_timestamp": rows["reading_timestamp"].iloc[0],
        "area_count": int(rows["planning_area"].nunique()),
        "raining_area_count": int((rows["raining_station_count"] > 0).sum()),
        "areas": rows.to_dict(orient="records"),
    }


def dashboard_summary(taxi: dict[str, Any], rainfall: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_taxis": taxi["total_taxis"],
        "taxi_area_count": taxi["planning_area_count"],
        "raining_area_count": rainfall["raining_area_count"],
        "rainfall_area_count": rainfall["area_count"],
        "taxi_timestamp": taxi["api_timestamp"],
        "rainfall_timestamp": rainfall["reading_timestamp"],
        "map_image_url": f"/map.png?ts={pd.Timestamp.utcnow().timestamp()}",
    }


def taxi_timeseries() -> dict[str, Any]:
    rows = query_dataframe(
        """
        SELECT
            api_timestamp,
            SUM(available_taxi_count) AS total_taxis
        FROM taxi_area_counts
        GROUP BY api_timestamp
        ORDER BY api_timestamp
        """
    )
    if rows.empty:
        raise HTTPException(status_code=404, detail="No taxi counts found.")

    return {
        "timestamps": [str(value) for value in rows["api_timestamp"]],
        "total_taxis": [int(value) for value in rows["total_taxis"]],
    }


def taxi_color(value: int, max_value: int) -> str:
    if value <= 0:
        return "#dbe4ee"
    ratio = value / max(max_value, 1)
    if ratio < 0.15:
        return "#a7d3f2"
    if ratio < 0.35:
        return "#5bb8d7"
    if ratio < 0.60:
        return "#2386b8"
    if ratio < 0.85:
        return "#f59e0b"
    return "#dc2626"


def rainfall_color(value: Any) -> str:
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


def load_planning_areas() -> gpd.GeoDataFrame:
    planning_areas = gpd.read_file(geojson_path())
    if planning_areas.crs is None:
        planning_areas = planning_areas.set_crs("EPSG:4326")
    else:
        planning_areas = planning_areas.to_crs("EPSG:4326")

    planning_areas = planning_areas[["PLN_AREA_N", "geometry"]].copy()
    planning_areas["planning_area"] = planning_areas["PLN_AREA_N"].map(clean_area_name)
    return planning_areas.dissolve(by="planning_area", as_index=False)


def latest_taxi_points() -> pd.DataFrame:
    for path in TAXI_POINTS_PATHS:
        if path.exists():
            rows = pd.read_csv(path)
            return rows.dropna(subset=["longitude", "latitude"]).reset_index(drop=True)
    raise HTTPException(
        status_code=500,
        detail=(
            "Taxi point CSV not found. Run the Airflow DAG first so it writes "
            "data/intermediate/int_taxi_points_with_area.csv."
        ),
    )


def draw_latest_map() -> None:
    taxi = latest_taxi_counts()
    rainfall = latest_rainfall()
    taxi_points = latest_taxi_points()
    counts = pd.DataFrame(taxi["areas"])
    rainfall_by_area = pd.DataFrame(rainfall["areas"])

    planning_areas = load_planning_areas()
    planning_areas = planning_areas.merge(
        rainfall_by_area[["planning_area", "rainfall_mm"]],
        on="planning_area",
        how="left",
    )
    planning_areas = planning_areas.merge(
        counts[["planning_area", "available_taxi_count"]],
        on="planning_area",
        how="left",
    )
    planning_areas["available_taxi_count"] = (
        planning_areas["available_taxi_count"].fillna(0).astype(int)
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
        edgecolor="#64748b",
        linewidth=0.7,
        alpha=0.95,
    )
    taxi_points_gdf.plot(
        ax=ax,
        color="#d7191c",
        markersize=9,
        alpha=0.45,
    )

    label_layer = (
        planning_areas[planning_areas["available_taxi_count"] > 0]
        .sort_values("available_taxi_count", ascending=False)
        .head(12)
        .copy()
    )
    label_layer["label_point"] = label_layer.geometry.representative_point()
    for row in label_layer.itertuples():
        ax.text(
            row.label_point.x,
            row.label_point.y,
            f"{row.planning_area}\n{int(row.available_taxi_count)}",
            fontsize=8,
            ha="center",
            va="center",
            color="#26323f",
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.65, "pad": 1.5},
        )

    ax.set_title("Current Rainfall Areas with Available Taxi Points", fontsize=16)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    ax.grid(True, color="#e5e7eb", linewidth=0.5)

    ax.text(
        0.01,
        0.01,
        (
            f"Taxi: {taxi['api_timestamp']} | "
            f"Rainfall: {rainfall['reading_timestamp']} | "
            f"Total taxis: {taxi['total_taxis']}"
        ),
        transform=ax.transAxes,
        fontsize=9,
        color="#334155",
        ha="left",
        va="bottom",
        bbox={"facecolor": "white", "edgecolor": "#d0d7de", "alpha": 0.88},
    )

    fig.tight_layout()
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(MAP_PATH, dpi=140)
    plt.close(fig)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Day 2 Airflow Output</title>
  <style>
    :root {
      --bg: #f7f8fa;
      --panel: #ffffff;
      --line: #d7dce2;
      --text: #1d2733;
      --muted: #647184;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Arial, Helvetica, sans-serif; }
    header { height: 64px; display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 0 24px; border-bottom: 1px solid var(--line); background: var(--panel); }
    h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 0; }
    .status { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; white-space: nowrap; }
    .dot { width: 10px; height: 10px; border-radius: 999px; background: #98a2b3; }
    .dot.loading { background: #f59e0b; }
    .dot.ok { background: #16a34a; }
    .dot.error { background: #dc2626; }
    main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; min-height: calc(100vh - 64px); }
    .image-wrap { padding: 18px; min-height: calc(100vh - 64px); display: flex; align-items: center; justify-content: center; }
    #map-image { width: 100%; max-height: calc(100vh - 100px); object-fit: contain; border: 1px solid var(--line); background: white; }
    aside { border-left: 1px solid var(--line); background: var(--panel); padding: 18px; overflow: auto; }
    .metric { border-bottom: 1px solid var(--line); padding: 14px 0; }
    .metric:first-child { padding-top: 0; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
    .value { margin-top: 6px; font-size: 28px; font-weight: 700; }
    .small { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.4; overflow-wrap: anywhere; }
    .legend { display: grid; gap: 8px; margin-top: 10px; }
    .legend-row { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
    .swatch { width: 22px; height: 14px; border: 1px solid var(--line); flex: 0 0 auto; }
    button { width: 100%; height: 40px; margin-top: 18px; border: 1px solid #1f2937; background: #1f2937; color: white; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: 0.55; cursor: default; }
    @media (max-width: 900px) {
      header { height: auto; align-items: flex-start; flex-direction: column; padding: 14px 16px; }
      main { grid-template-columns: 1fr; }
      .image-wrap { min-height: 55vh; }
      aside { border-left: 0; border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <h1>Day 2 Airflow Output</h1>
    <div class="status">
      <span id="status-dot" class="dot"></span>
      <span id="status-text">Waiting for first refresh</span>
    </div>
  </header>
  <main>
    <section class="image-wrap">
      <img id="map-image" alt="Singapore map of latest taxi availability">
    </section>
    <aside>
      <div class="metric">
        <div class="label">Available Taxis</div>
        <div id="total-taxis" class="value">-</div>
        <div id="taxi-time" class="small">Taxi API timestamp: -</div>
      </div>
      <div class="metric">
        <div class="label">Areas With Rain</div>
        <div id="raining-areas" class="value">-</div>
        <div id="rainfall-time" class="small">Rainfall API timestamp: -</div>
      </div>
      <div class="metric">
        <div class="label">Rainfall Intensity Color</div>
        <div class="legend">
          <div class="legend-row"><span class="swatch" style="background:#9db7d3"></span>No rain / trace: &lt; 0.5 mm/hr</div>
          <div class="legend-row"><span class="swatch" style="background:#86efac"></span>Light: 0.5 to &lt; 2 mm/hr</div>
          <div class="legend-row"><span class="swatch" style="background:#facc15"></span>Moderate: 2 to &lt; 10 mm/hr</div>
          <div class="legend-row"><span class="swatch" style="background:#f97316"></span>Heavy: 10 to &lt; 30 mm/hr</div>
          <div class="legend-row"><span class="swatch" style="background:#7e22ce"></span>Very heavy: 30+ mm/hr</div>
        </div>
        <div class="small">The rainfall API gives 5-minute totals, so the dashboard approximates intensity as mm/hr. Red points are available taxi locations.</div>
      </div>
      <div class="metric">
        <div class="label">Pipeline</div>
        <div id="pipeline-status" class="small">Airflow DAG -> DuckDB tables -> FastAPI reads latest rows -> redraw PNG -> update page</div>
      </div>
      <button id="refresh-button" type="button">Refresh now</button>
      <div class="small">The browser refreshes this dashboard every 2 minutes. Each refresh reads the latest Airflow output.</div>
    </aside>
  </main>
  <script>
    const REFRESH_MS = 120000;

    function setStatus(kind, text) {
      const dot = document.getElementById('status-dot');
      dot.className = `dot ${kind}`;
      document.getElementById('status-text').textContent = text;
    }

    function renderSummary(summary) {
      document.getElementById('total-taxis').textContent = summary.total_taxis ?? '-';
      document.getElementById('raining-areas').textContent =
        `${summary.raining_area_count ?? '-'} / ${summary.rainfall_area_count ?? '-'}`;
      document.getElementById('taxi-time').textContent =
        `Taxi API timestamp: ${summary.taxi_timestamp ?? '-'}`;
      document.getElementById('rainfall-time').textContent =
        `Rainfall API timestamp: ${summary.rainfall_timestamp ?? '-'}`;
      document.getElementById('pipeline-status').textContent =
        `Airflow-produced DuckDB output | ${summary.taxi_area_count ?? '-'} taxi areas | ${summary.raining_area_count ?? '-'} raining areas`;
      document.getElementById('map-image').src = summary.map_image_url || `/map.png?ts=${Date.now()}`;
    }

    async function refreshDashboard() {
      const button = document.getElementById('refresh-button');
      button.disabled = true;
      setStatus('loading', 'Refreshing: read DuckDB output and redraw PNG');
      try {
        const response = await fetch('/api/refresh', { cache: 'no-store' });
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        renderSummary(payload.summary);
        setStatus('ok', `Updated ${new Date().toLocaleTimeString()}`);
      } catch (error) {
        setStatus('error', `Refresh failed: ${error.message}`);
      } finally {
        button.disabled = false;
      }
    }

    document.getElementById('refresh-button').addEventListener('click', refreshDashboard);
    refreshDashboard();
    setInterval(refreshDashboard, REFRESH_MS);
  </script>
</body>
</html>
"""


@app.get("/map.png")
def map_png() -> FileResponse:
    draw_latest_map()
    return FileResponse(MAP_PATH, media_type="image/png")


@app.get("/api/refresh")
def refresh() -> dict[str, Any]:
    draw_latest_map()
    taxi = latest_taxi_counts()
    rainfall = latest_rainfall()
    return {
        "summary": dashboard_summary(taxi, rainfall),
        "taxi": taxi,
        "rainfall": rainfall,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "geojson": str(geojson_path()),
        "map": str(MAP_PATH),
    }


@app.get("/api/taxi/current")
def current_taxi() -> dict[str, Any]:
    return latest_taxi_counts()


@app.get("/api/taxi/timeseries")
def taxi_total_timeseries() -> dict[str, Any]:
    return taxi_timeseries()


@app.get("/api/rainfall/current")
def current_rainfall() -> dict[str, Any]:
    return latest_rainfall()
