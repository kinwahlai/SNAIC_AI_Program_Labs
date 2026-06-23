import json
from functools import lru_cache
from typing import Any

import pandas as pd

from jobs.config import GEOJSON_PATH


def clean_area_name(value: Any) -> str:
    return str(value).strip().title()


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """
    Return True if a point is inside one polygon ring.

    This is a small ray-casting implementation. It keeps the Airflow demo
    lightweight, so students do not need geopandas inside the Airflow container.
    """
    inside = False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        lon_i, lat_i = point[0], point[1]
        lon_j, lat_j = ring[j][0], ring[j][1]
        crosses_lat = (lat_i > lat) != (lat_j > lat)
        if crosses_lat:
            crossing_lon = (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i) + lon_i
            if lon < crossing_lon:
                inside = not inside
        j = i
    return inside


def point_in_polygon(lon: float, lat: float, rings: list[list[list[float]]]) -> bool:
    if not rings:
        return False

    # First ring is the outer boundary. Later rings are holes.
    if not point_in_ring(lon, lat, rings[0]):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in rings[1:])


def bbox_for_rings(rings: list[list[list[float]]]) -> tuple[float, float, float, float]:
    points = [point for ring in rings for point in ring]
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    return min(lons), min(lats), max(lons), max(lats)


@lru_cache(maxsize=1)
def load_planning_area_shapes() -> list[dict[str, Any]]:
    if not GEOJSON_PATH.exists():
        raise FileNotFoundError(f"Planning-area GeoJSON not found: {GEOJSON_PATH}")

    payload = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    shapes: list[dict[str, Any]] = []

    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        planning_area = clean_area_name(properties.get("PLN_AREA_N"))
        geometry = feature.get("geometry", {})
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])

        if geometry_type == "Polygon":
            polygons = [coordinates]
        elif geometry_type == "MultiPolygon":
            polygons = coordinates
        else:
            continue

        for rings in polygons:
            min_lon, min_lat, max_lon, max_lat = bbox_for_rings(rings)
            shapes.append(
                {
                    "planning_area": planning_area,
                    "rings": rings,
                    "bbox": (min_lon, min_lat, max_lon, max_lat),
                }
            )

    return shapes


def find_planning_area(lon: float, lat: float) -> str | None:
    for shape in load_planning_area_shapes():
        min_lon, min_lat, max_lon, max_lat = shape["bbox"]
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if point_in_polygon(lon, lat, shape["rings"]):
            return shape["planning_area"]
    return None


def attach_area_from_coordinates(
    df: pd.DataFrame,
    longitude_col: str = "longitude",
    latitude_col: str = "latitude",
) -> pd.DataFrame:
    output = df.copy()
    output["planning_area"] = [
        find_planning_area(float(row[longitude_col]), float(row[latitude_col]))
        for _, row in output.iterrows()
    ]
    return output
