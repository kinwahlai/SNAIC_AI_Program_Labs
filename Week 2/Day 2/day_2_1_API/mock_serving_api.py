"""
Day 2.1 Advanced API mock serving service.

Run from this folder:
    python -m uvicorn mock_serving_api:app --reload --port 8011

Open:
    http://127.0.0.1:8011/docs

This service proxies the real Day 1 data.gov.sg sources:
    - taxi availability
    - 2-hour weather forecast
    - rainfall observations

The instructor can still switch failure modes while you are writing API
clients:
    POST /admin/scenario/flaky
    POST /admin/scenario/rate_limit
    POST /admin/schema/v2
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Literal

import requests
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse


Scenario = Literal[
    "normal",
    "flaky",
    "rate_limit",
    "timeout",
    "http_error",
    "duplicates",
]
SchemaVersion = Literal["v1", "v2"]
SourceName = Literal["taxi", "weather", "rainfall"]

DEFAULT_SCENARIO: Scenario = os.getenv("DEFAULT_SCENARIO", "normal")  # type: ignore[assignment]
DEFAULT_SCHEMA_VERSION: SchemaVersion = os.getenv("DEFAULT_SCHEMA_VERSION", "v1")  # type: ignore[assignment]
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# Failure mode: when scenario="timeout", the service sleeps for this
# many seconds before responding. Learners should use client-side timeouts.
TIMEOUT_SECONDS = 12

# Upstream safety settings. These protect data.gov.sg from classroom traffic.
# Even if students refresh aggressively, most requests should be served from
# this mock serving service's memory cache instead of being forwarded upstream.
UPSTREAM_TIMEOUT_SECONDS = 10
UPSTREAM_CACHE_TTL_SECONDS = int(os.getenv("UPSTREAM_CACHE_TTL_SECONDS", "60"))

# Scenario rate-limit settings. These are intentionally small so learners can
# trigger 429 responses during the lab.
RATE_LIMIT_WINDOW_SECONDS = 5
RATE_LIMIT_MAX_REQUESTS = 3

SOURCE_URLS = {
    "taxi": "https://api.data.gov.sg/v1/transport/taxi-availability",
    "weather": "https://api-open.data.gov.sg/v2/real-time/api/two-hr-forecast",
    "rainfall": "https://api-open.data.gov.sg/v2/real-time/api/rainfall",
}

app = FastAPI(
    title="Day 2.1 Mock Serving API",
    description=(
        "A mock serving API provider that proxies real data.gov.sg sources and adds "
        "pagination, retry, rate-limit, schema-change, deduplication, and "
        "checkpoint/resume exercises."
    ),
    version="1.0.0",
)

# Protects instructor-controlled teaching state.
state_lock = Lock()
state = {
    "scenario": DEFAULT_SCENARIO,
    "schema_version": DEFAULT_SCHEMA_VERSION,
    "request_count": 0,
    "last_request_at": None,
}
client_attempts: dict[str, int] = defaultdict(int)
rate_limit_windows: dict[str, deque[float]] = defaultdict(deque)

# In-memory cache for real upstream payloads. This is the main protection
# against accidental client traffic spikes hitting data.gov.sg.
upstream_cache: dict[str, dict[str, object]] = {}

# One lock per upstream source prevents a cache stampede. Without this, 40
# students refreshing at the same time after cache expiry could create 40
# simultaneous upstream requests.
upstream_locks: dict[str, Lock] = {
    "taxi": Lock(),
    "weather": Lock(),
    "rainfall": Lock(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_state() -> tuple[Scenario, SchemaVersion]:
    with state_lock:
        return state["scenario"], state["schema_version"]  # type: ignore[return-value]


def record_request() -> None:
    with state_lock:
        state["request_count"] += 1
        state["last_request_at"] = now_iso()


def reset_runtime_state() -> None:
    client_attempts.clear()
    rate_limit_windows.clear()


def client_id_from_request(request: Request, explicit_client_id: str | None) -> str:
    if explicit_client_id:
        return explicit_client_id
    if request.client:
        return request.client.host
    return "unknown"


def verify_admin_token(
    admin_token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    # Local development remains frictionless when ADMIN_TOKEN is empty.
    # Shared deployments should set ADMIN_TOKEN in the runtime environment.
    if not ADMIN_TOKEN:
        return
    if admin_token == ADMIN_TOKEN or x_admin_token == ADMIN_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Missing or invalid admin token.")


def maybe_apply_failure_scenario(scenario: Scenario, client_id: str) -> JSONResponse | None:
    # These scenarios are intentionally implemented in our mock serving API layer.
    # The upstream provider is not asked to fail; we simulate production
    # problems while still using real data as the base payload.
    if scenario == "timeout":
        time.sleep(TIMEOUT_SECONDS)
        return None

    if scenario == "http_error":
        raise HTTPException(
            status_code=503,
            detail="The mock serving API is simulating an upstream outage.",
        )

    if scenario == "flaky":
        client_attempts[client_id] += 1
        if client_attempts[client_id] <= 2:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Temporary server error. This client succeeds after two "
                    f"failed attempts. attempt={client_attempts[client_id]}"
                ),
            )

    if scenario == "rate_limit":
        now = time.time()
        window = rate_limit_windows[client_id]
        while window and now - window[0] >= RATE_LIMIT_WINDOW_SECONDS:
            window.popleft()

        if len(window) >= RATE_LIMIT_MAX_REQUESTS:
            retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - window[0]) + 0.999))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too many requests",
                    "message": (
                        f"Max {RATE_LIMIT_MAX_REQUESTS} requests per "
                        f"{RATE_LIMIT_WINDOW_SECONDS} seconds."
                    ),
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)

    return None


def fetch_upstream_payload(source: SourceName) -> dict[str, object]:
    """
    Fetch a real upstream payload with cache and stampede protection.

    Classroom safety rule:
    - Client requests should usually hit our in-memory cache.
    - Only one request per source is allowed to refresh the cache at a time.
    - If the upstream API fails but we have an older cached payload, return it.

    This keeps the exercise realistic without turning the class into a traffic
    spike against data.gov.sg.
    """
    cached = upstream_cache.get(source)
    now = time.time()
    if cached and now - float(cached["fetched_at_epoch"]) < UPSTREAM_CACHE_TTL_SECONDS:
        return cached["payload"]  # type: ignore[return-value]

    with upstream_locks[source]:
        # Another request may have refreshed the cache while this request was
        # waiting for the lock, so check the cache again before calling upstream.
        cached = upstream_cache.get(source)
        now = time.time()
        if cached and now - float(cached["fetched_at_epoch"]) < UPSTREAM_CACHE_TTL_SECONDS:
            return cached["payload"]  # type: ignore[return-value]

        try:
            response = requests.get(SOURCE_URLS[source], timeout=UPSTREAM_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            # Stale data is better than a class-wide outage when the upstream
            # source has a temporary problem.
            if cached:
                return cached["payload"]  # type: ignore[return-value]
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch upstream {source} API: {exc}",
            ) from exc

        payload = response.json()
        upstream_cache[source] = {
            "payload": payload,
            "fetched_at_epoch": now,
            "fetched_at": now_iso(),
        }
        return payload


def flatten_taxi(payload: dict[str, object]) -> list[dict[str, object]]:
    # Day 1 taxi data is a nested GeoJSON-like payload. For the Day 2 advanced
    # API exercise, we flatten taxi coordinates into one row per taxi point.
    features = payload.get("features", [])
    if not isinstance(features, list) or not features:
        return []

    feature = features[0]
    properties = feature.get("properties", {})
    geometry = feature.get("geometry", {})
    timestamp = properties.get("timestamp")
    taxi_count = properties.get("taxi_count")
    coordinates = geometry.get("coordinates", [])

    rows = []
    for index, coordinate in enumerate(coordinates, start=1):
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            continue
        rows.append(
            {
                "record_id": f"taxi:{timestamp}:{index}",
                "api_timestamp": timestamp,
                "taxi_index": index,
                "longitude": coordinate[0],
                "latitude": coordinate[1],
                "available_taxi_count": taxi_count,
                "source": "data.gov.sg taxi availability",
            }
        )
    return rows


def flatten_weather(payload: dict[str, object]) -> list[dict[str, object]]:
    # Day 1 weather v2 data stores forecasts under data.items[0].forecasts.
    # The flattened table keeps the validity window because it matters for
    # downstream joins and dashboard freshness.
    data = payload.get("data", payload)
    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        return []

    item = items[0]
    update_timestamp = item.get("update_timestamp") or item.get("timestamp")
    timestamp = item.get("timestamp") or update_timestamp
    valid_period = item.get("valid_period", {})
    forecasts = item.get("forecasts", [])

    rows = []
    for forecast in forecasts:
        area = forecast.get("area")
        rows.append(
            {
                "record_id": f"weather:{update_timestamp}:{area}",
                "api_timestamp": timestamp,
                "api_update_timestamp": update_timestamp,
                "area": area,
                "forecast": forecast.get("forecast"),
                "valid_period_start": valid_period.get("start"),
                "valid_period_end": valid_period.get("end"),
                "source": "data.gov.sg two-hour weather forecast",
            }
        )
    return rows


def flatten_rainfall(payload: dict[str, object]) -> list[dict[str, object]]:
    # Rainfall readings reference station IDs, so we first build a station
    # lookup table and then attach station metadata to every reading.
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []

    stations = data.get("stations", [])
    readings = data.get("readings", [])
    station_lookup = {
        str(station.get("id") or station.get("deviceId")): station
        for station in stations
    }

    rows = []
    for reading_block in readings:
        reading_timestamp = reading_block.get("timestamp")
        for reading in reading_block.get("data", []):
            station_id = str(reading.get("stationId", ""))
            station = station_lookup.get(station_id, {})
            location = station.get("location", {})
            rows.append(
                {
                    "record_id": f"rainfall:{reading_timestamp}:{station_id}",
                    "reading_timestamp": reading_timestamp,
                    "station_id": station_id,
                    "station_name": station.get("name"),
                    "longitude": location.get("longitude"),
                    "latitude": location.get("latitude"),
                    "rainfall_mm": reading.get("value"),
                    "source": "data.gov.sg rainfall",
                }
            )
    return rows


def source_rows(source: SourceName) -> list[dict[str, object]]:
    # This is the provider-side boundary between raw upstream JSON and the flat
    # analytics-friendly records used in the lab.
    payload = fetch_upstream_payload(source)
    if source == "taxi":
        return flatten_taxi(payload)
    if source == "weather":
        return flatten_weather(payload)
    return flatten_rainfall(payload)


def make_flat_records(
    source: SourceName,
    schema_version: SchemaVersion,
    duplicate_rows: bool = False,
) -> list[dict[str, object]]:
    # This function is where the instructor can demonstrate provider-side
    # contract changes. Real data is used first; then the mock serving API mutates
    # the response shape when schema_version is v2.
    records = [dict(row) for row in source_rows(source)]
    if duplicate_rows:
        records = records + [dict(row) for row in records[10:18]] + [dict(row) for row in records[30:35]]

    if schema_version == "v2" and source == "taxi":
        for record in records:
            record["taxi_count"] = record.pop("available_taxi_count")
            record["schema_note"] = "schema v2 renamed available_taxi_count to taxi_count"

    if schema_version == "v2" and source == "rainfall":
        for record in records:
            record["rainfall_value_mm"] = record.pop("rainfall_mm")
            record["schema_note"] = "schema v2 renamed rainfall_mm to rainfall_value_mm"

    return records


def paginated_response(
    records: list[dict[str, object]],
    page: int,
    page_size: int,
    source: SourceName,
    scenario: Scenario,
    schema_version: SchemaVersion,
) -> dict[str, object]:
    total_records = len(records)
    total_pages = (total_records + page_size - 1) // page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]

    return {
        "source": source,
        "page": page,
        "page_size": page_size,
        "total_records": total_records,
        "total_pages": total_pages,
        "next_page": page + 1 if page < total_pages else None,
        "schema_version": schema_version,
        "scenario": scenario,
        "data": page_records,
    }


def challenge_records(
    source: SourceName,
    request: Request,
    scenario: Scenario,
    schema_version: SchemaVersion = "v1",
    page: int = 1,
    page_size: int = 10,
    client_id: str | None = None,
) -> dict[str, object] | JSONResponse:
    """Serve a fixed challenge scenario without changing global API state."""
    record_request()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario(scenario, resolved_client_id)
    if scenario_response is not None:
        return scenario_response

    records = make_flat_records(
        source=source,
        schema_version=schema_version,
        duplicate_rows=scenario == "duplicates",
    )
    return paginated_response(records, page, page_size, source, scenario, schema_version)


@app.get("/")
def home() -> dict[str, object]:
    return {
        "message": "Day 2.1 Advanced API mock serving service is running.",
        "docs": "/docs",
        "status": "/api/v1/status",
        "raw_endpoints": {
            "taxi": "/api/v1/transport/taxi-availability",
            "weather": "/api/v1/environment/2-hour-weather-forecast",
            "rainfall": "/api/v1/environment/rainfall",
        },
        "paginated_endpoint": "/api/v1/sources/{source}/records",
        "legacy_taxi_paginated_endpoint": "/api/v1/taxi/availability",
        "debugging_lab": "/debug-lab",
    }


@app.get("/api/v1/status")
def status() -> dict[str, object]:
    with state_lock:
        return {
            **state,
            "available_scenarios": [
                "normal",
                "flaky",
                "rate_limit",
                "timeout",
                "http_error",
                "duplicates",
            ],
            "available_schema_versions": ["v1", "v2"],
            "upstream_sources": SOURCE_URLS,
            "upstream_cache": {
                key: {
                    "fetched_at": value["fetched_at"],
                    "age_seconds": round(time.time() - float(value["fetched_at_epoch"]), 1),
                }
                for key, value in upstream_cache.items()
            },
            "upstream_cache_ttl_seconds": UPSTREAM_CACHE_TTL_SECONDS,
            "rate_limit": {
                "max_requests": RATE_LIMIT_MAX_REQUESTS,
                "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
            },
        }


@app.post("/admin/scenario/{scenario}")
def set_scenario(
    scenario: Scenario,
    admin_token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, object]:
    verify_admin_token(admin_token, x_admin_token)
    with state_lock:
        state["scenario"] = scenario
    reset_runtime_state()
    return {"message": "Scenario updated.", "scenario": scenario}


@app.post("/admin/schema/{schema_version}")
def set_schema(
    schema_version: SchemaVersion,
    admin_token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, object]:
    verify_admin_token(admin_token, x_admin_token)
    with state_lock:
        state["schema_version"] = schema_version
    return {"message": "Schema version updated.", "schema_version": schema_version}


@app.post("/admin/reset")
def reset(
    admin_token: str | None = Query(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, object]:
    verify_admin_token(admin_token, x_admin_token)
    with state_lock:
        state["scenario"] = DEFAULT_SCENARIO
        state["schema_version"] = DEFAULT_SCHEMA_VERSION
        state["request_count"] = 0
        state["last_request_at"] = None
    reset_runtime_state()
    upstream_cache.clear()
    return {
        "message": "State reset.",
        "scenario": DEFAULT_SCENARIO,
        "schema_version": DEFAULT_SCHEMA_VERSION,
    }


@app.get("/api/v1/transport/taxi-availability", response_model=None)
def day_1_style_taxi_availability(
    request: Request,
    client_id: str | None = Query(default=None),
):
    """
    Real Day 1 taxi endpoint proxied through the mock serving API.

    It keeps the original nested GeoJSON-like shape, so Day 1 parsing code can
    still work.
    """
    record_request()
    scenario, _ = get_state()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario(scenario, resolved_client_id)
    if scenario_response is not None:
        return scenario_response
    return fetch_upstream_payload("taxi")


@app.get("/api/v1/environment/2-hour-weather-forecast", response_model=None)
def day_1_style_weather_forecast(
    request: Request,
    client_id: str | None = Query(default=None),
):
    """Real Day 1 weather forecast endpoint proxied through the mock serving API."""
    record_request()
    scenario, _ = get_state()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario(scenario, resolved_client_id)
    if scenario_response is not None:
        return scenario_response
    return fetch_upstream_payload("weather")


@app.get("/api/v1/environment/rainfall", response_model=None)
def day_1_style_rainfall(
    request: Request,
    client_id: str | None = Query(default=None),
):
    """Real Day 1 rainfall endpoint proxied through the mock serving API."""
    record_request()
    scenario, _ = get_state()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario(scenario, resolved_client_id)
    if scenario_response is not None:
        return scenario_response
    return fetch_upstream_payload("rainfall")


@app.get("/api/v1/sources/{source}/records", response_model=None)
def paginated_source_records(
    source: SourceName,
    request: Request,
    page: int = Query(default=1, ge=1, description="Page number, starting from 1."),
    page_size: int = Query(default=10, ge=1, le=20, description="Rows per page. Max 20."),
    client_id: str | None = Query(default=None, description="Use a group ID to isolate retry/rate-limit state."),
):
    """
    Flat analytics-friendly endpoint for taxi, weather, or rainfall records.

    This is the main endpoint for advanced API practice. It demonstrates
    pagination, rate limiting, temporary errors, duplicate records, and schema
    changes while still using real upstream data.
    """
    record_request()
    scenario, schema_version = get_state()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario(scenario, resolved_client_id)
    if scenario_response is not None:
        return scenario_response

    records = make_flat_records(
        source=source,
        schema_version=schema_version,
        duplicate_rows=scenario == "duplicates",
    )
    return paginated_response(records, page, page_size, source, scenario, schema_version)


@app.get("/api/v1/taxi/availability", response_model=None)
def legacy_paginated_taxi_availability(
    request: Request,
    page: int = Query(default=1, ge=1, description="Page number, starting from 1."),
    page_size: int = Query(default=10, ge=1, le=20, description="Rows per page. Max 20."),
    client_id: str | None = Query(default=None, description="Use a group ID to isolate retry/rate-limit state."),
):
    """Backward-compatible taxi endpoint used by the first notebook draft."""
    return paginated_source_records(
        source="taxi",
        request=request,
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab")
def debugging_lab_index() -> dict[str, object]:
    """List fixed endpoints used by the failure-first debugging scripts."""
    return {
        "idea": "Each endpoint maps to one row in the PPT robust API collection table.",
        "expected_api_key": "class-demo-key",
        "endpoints": {
            "01_auth_moved": "/debug-lab/01-auth-moved/taxi/records",
            "02_schema_changed": "/debug-lab/02-schema-change/taxi/records",
            "03_rate_limited": "/debug-lab/03-rate-limit/weather/records",
            "04_flaky_server": "/debug-lab/04-flaky/rainfall/records",
            "05_duplicates": "/debug-lab/05-duplicates/taxi/records",
            "06_pagination": "/debug-lab/06-pagination/taxi/records",
            "07_timeout": "/debug-lab/07-timeout/taxi/records",
            "08_checkpoint": "/debug-lab/08-checkpoint/taxi/records",
            "09_incremental": "/debug-lab/09-incremental/weather/records",
            "10_scheduled_ingestion": "/debug-lab/10-scheduled/taxi/current",
        },
    }


@app.get("/debug-lab/01-auth-moved/taxi/records", response_model=None)
def challenge_auth_moved(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    api_key: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    client_id: str | None = Query(default=None),
):
    """
    Challenge 01: authentication location changed.

    Old clients send X-API-Key. This endpoint now expects api_key as a query
    parameter. Learners should read the 401 response and update their request.
    """
    if x_api_key is not None:
        raise HTTPException(
            status_code=401,
            detail=(
                "API key location changed. Do not send X-API-Key. "
                "Send api_key=class-demo-key as a query parameter."
            ),
        )
    if api_key != "class-demo-key":
        raise HTTPException(
            status_code=401,
            detail="Missing api_key query parameter. Expected api_key=class-demo-key.",
        )

    return challenge_records(
        source="taxi",
        request=request,
        scenario="normal",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/02-schema-change/taxi/records", response_model=None)
def challenge_schema_change(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 02: taxi count field changed from available_taxi_count to taxi_count."""
    return challenge_records(
        source="taxi",
        request=request,
        scenario="normal",
        schema_version="v2",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/03-rate-limit/weather/records", response_model=None)
def challenge_rate_limit(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 03: fixed endpoint that returns 429 after too many requests."""
    return challenge_records(
        source="weather",
        request=request,
        scenario="rate_limit",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/04-flaky/rainfall/records", response_model=None)
def challenge_flaky(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 04: first two requests fail with 503 for each client_id."""
    return challenge_records(
        source="rainfall",
        request=request,
        scenario="flaky",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/05-duplicates/taxi/records", response_model=None)
def challenge_duplicates(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 05: response contains duplicate record_id values."""
    return challenge_records(
        source="taxi",
        request=request,
        scenario="duplicates",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/06-pagination/taxi/records", response_model=None)
def challenge_pagination(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 06: the API returns only one page at a time."""
    return challenge_records(
        source="taxi",
        request=request,
        scenario="normal",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/07-timeout/taxi/records", response_model=None)
def challenge_timeout(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 07: the service responds too slowly for a short client timeout."""
    return challenge_records(
        source="taxi",
        request=request,
        scenario="timeout",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/08-checkpoint/taxi/records", response_model=None)
def challenge_checkpoint(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=20),
    client_id: str | None = Query(default=None),
):
    """Challenge 08: long paginated job used for checkpoint/resume practice."""
    return challenge_records(
        source="taxi",
        request=request,
        scenario="normal",
        page=page,
        page_size=page_size,
        client_id=client_id,
    )


@app.get("/debug-lab/09-incremental/weather/records", response_model=None)
def challenge_incremental(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    updated_since: str | None = Query(default=None),
    client_id: str | None = Query(default=None),
):
    """
    Challenge 09: incremental loading.

    If updated_since is provided, return only records with api_update_timestamp
    later than that value. If not provided, this behaves like a full refresh.
    """
    record_request()
    scenario, schema_version = get_state()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario("normal", resolved_client_id)
    if scenario_response is not None:
        return scenario_response

    records = make_flat_records(source="weather", schema_version="v1", duplicate_rows=False)
    if updated_since:
        records = [
            record
            for record in records
            if str(record.get("api_update_timestamp") or "") > updated_since
        ]

    return paginated_response(records, page, page_size, "weather", scenario, schema_version)


@app.get("/debug-lab/10-scheduled/taxi/current", response_model=None)
def challenge_scheduled_ingestion(
    request: Request,
    client_id: str | None = Query(default=None),
):
    """Challenge 10: one snapshot is not enough for trend analysis."""
    record_request()
    resolved_client_id = client_id_from_request(request, client_id)
    scenario_response = maybe_apply_failure_scenario("normal", resolved_client_id)
    if scenario_response is not None:
        return scenario_response

    records = make_flat_records(source="taxi", schema_version="v1", duplicate_rows=False)
    return {
        "collection_time": now_iso(),
        "source": "taxi",
        "snapshot_rows": len(records),
        "sample": records[:10],
    }


@app.get("/api/v1/validation/expected")
def validation_expected() -> dict[str, object]:
    expected: dict[str, object] = {
        "schema_v1_taxi_count_field": "available_taxi_count",
        "schema_v2_taxi_count_field": "taxi_count",
        "schema_v1_rainfall_field": "rainfall_mm",
        "schema_v2_rainfall_field": "rainfall_value_mm",
    }

    for source in ("taxi", "weather", "rainfall"):
        base_records = make_flat_records(source=source, schema_version="v1", duplicate_rows=False)
        duplicate_records = make_flat_records(source=source, schema_version="v1", duplicate_rows=True)
        expected[f"{source}_base_total_records"] = len(base_records)
        expected[f"{source}_duplicate_total_records"] = len(duplicate_records)
        expected[f"{source}_unique_record_ids"] = len(
            {record["record_id"] for record in duplicate_records}
        )

    return expected
