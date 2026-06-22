from pathlib import Path

import geopandas as gpd
import pandas as pd


def find_day1_base_dir() -> Path:
    current = Path.cwd().resolve()

    if (current / "day_1.pptx").exists():
        return current

    if (current / "day_1" / "day_1.pptx").exists():
        return current / "day_1"

    for parent in current.parents:
        if parent.name == "day_1" or (parent / "day_1.pptx").exists():
            return parent

    return current


BASE_DIR = find_day1_base_dir()
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"
GEOJSON_PATH = BASE_DIR / "shared_assets" / "MasterPlan2019SubzoneBoundaryNoSeaGEOJSON.geojson"

PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def latest_raw_file(prefix: str) -> Path:
    files = sorted(RAW_DIR.glob(f"{prefix}_*.csv"))
    if not files:
        raise FileNotFoundError(f"No raw CSV found for {prefix} in {RAW_DIR}")
    return files[-1]


def remove_timezone(series: pd.Series) -> pd.Series:
    # API timestamps look like 2026-06-07T22:21:44+08:00.
    # pandas reads the +08:00 timezone, then tz_localize(None) removes it for a simpler class display.
    return pd.to_datetime(series).dt.tz_localize(None)


def add_30min_bucket(df: pd.DataFrame, timestamp_col: str, bucket_col: str = "timestamp_30min") -> pd.DataFrame:
    df = df.copy()
    df[bucket_col] = df[timestamp_col].dt.floor("30min")
    return df


def clean_area_name(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.title()


def quality_report(name: str, df: pd.DataFrame, duplicate_cols: list[str]) -> None:
    print("=" * 70)
    print(name)
    print("Rows:", len(df))
    print("Duplicate rows using key columns:", df.duplicated(subset=duplicate_cols).sum())
    print("Missing values by column:")
    print(df.isna().sum())


def load_subzones() -> gpd.GeoDataFrame:
    subzones = gpd.read_file(GEOJSON_PATH)
    if "PLN_AREA_N" not in subzones.columns:
        raise ValueError("The GeoJSON file must contain a PLN_AREA_N column.")

    if subzones.crs is None:
        subzones = subzones.set_crs("EPSG:4326")
    else:
        subzones = subzones.to_crs("EPSG:4326")

    subzones = subzones[["SUBZONE_N", "PLN_AREA_N", "geometry"]].copy()
    subzones["subzone"] = clean_area_name(subzones["SUBZONE_N"])
    subzones["area"] = clean_area_name(subzones["PLN_AREA_N"])
    return subzones[["subzone", "area", "geometry"]]


def attach_area_from_coordinates(
    df: pd.DataFrame,
    subzones: gpd.GeoDataFrame,
    longitude_col: str = "longitude",
    latitude_col: str = "latitude",
) -> pd.DataFrame:
    points = gpd.GeoDataFrame(
        df.copy(),
        geometry=gpd.points_from_xy(df[longitude_col], df[latitude_col]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points, subzones, how="left", predicate="intersects")
    return pd.DataFrame(joined.drop(columns=["geometry", "index_right"]))


def clean_weather(raw_path: Path) -> pd.DataFrame:
    weather = pd.read_csv(raw_path)

    weather["record_timestamp"] = remove_timezone(weather["timestamp"])
    weather["api_update_timestamp"] = remove_timezone(weather["update_timestamp"])
    weather["forecast_start_timestamp"] = remove_timezone(weather["valid_start"])
    weather["forecast_end_timestamp"] = remove_timezone(weather["valid_end"])
    weather = add_30min_bucket(weather, "record_timestamp")

    clean = weather.rename(columns={"forecast": "weather_forecast"}).copy()
    clean["area"] = clean_area_name(clean["area"])
    clean = clean[
        [
            "timestamp_30min",
            "record_timestamp",
            "forecast_start_timestamp",
            "forecast_end_timestamp",
            "area",
            "weather_forecast",
        ]
    ]
    return clean.drop_duplicates().sort_values(["timestamp_30min", "area"]).reset_index(drop=True)


def clean_taxi(raw_path: Path, subzones: gpd.GeoDataFrame) -> pd.DataFrame:
    taxi = pd.read_csv(raw_path)

    taxi["record_timestamp"] = remove_timezone(taxi["timestamp"])
    taxi = add_30min_bucket(taxi, "record_timestamp")
    taxi_with_area = attach_area_from_coordinates(taxi, subzones)

    clean = (
        taxi_with_area.dropna(subset=["area"])
        .groupby(["timestamp_30min", "area"], as_index=False)
        .size()
        .rename(columns={"size": "taxi_count"})
        .sort_values(["timestamp_30min", "area"])
        .reset_index(drop=True)
    )
    return clean


def clean_rainfall(raw_path: Path, subzones: gpd.GeoDataFrame) -> pd.DataFrame:
    rainfall = pd.read_csv(raw_path)

    rainfall["record_timestamp"] = remove_timezone(rainfall["timestamp"])
    rainfall = add_30min_bucket(rainfall, "record_timestamp")
    rainfall_with_area = attach_area_from_coordinates(rainfall, subzones)

    clean = (
        rainfall_with_area.dropna(subset=["area"])
        .groupby(["timestamp_30min", "area"], as_index=False)["value"]
        .mean()
        .rename(columns={"value": "rainfall_mm"})
        .sort_values(["timestamp_30min", "area"])
        .reset_index(drop=True)
    )
    clean["rainfall_mm"] = clean["rainfall_mm"].round(2)
    return clean


def main() -> None:
    weather_raw = latest_raw_file("weather_forecast")
    taxi_raw = latest_raw_file("taxi_availability")
    rainfall_raw = latest_raw_file("rainfall")

    print("Weather raw:", weather_raw)
    print("Taxi raw:", taxi_raw)
    print("Rainfall raw:", rainfall_raw)

    subzones = load_subzones()

    weather_clean = clean_weather(weather_raw)
    taxi_clean = clean_taxi(taxi_raw, subzones)
    rainfall_clean = clean_rainfall(rainfall_raw, subzones)

    quality_report(
        "Clean weather",
        weather_clean,
        ["timestamp_30min", "area", "forecast_start_timestamp", "forecast_end_timestamp"],
    )
    quality_report("Clean taxi", taxi_clean, ["timestamp_30min", "area"])
    quality_report("Clean rainfall", rainfall_clean, ["timestamp_30min", "area"])

    weather_path = PROCESSED_DIR / "clean_weather_30min_by_area.csv"
    taxi_path = PROCESSED_DIR / "clean_taxi_30min_by_area.csv"
    rainfall_path = PROCESSED_DIR / "clean_rainfall_30min_by_area.csv"

    weather_clean.to_csv(weather_path, index=False)
    taxi_clean.to_csv(taxi_path, index=False)
    rainfall_clean.to_csv(rainfall_path, index=False)

    print("=" * 70)
    print("Saved clean files:")
    print(weather_path)
    print(taxi_path)
    print(rainfall_path)


if __name__ == "__main__":
    main()
