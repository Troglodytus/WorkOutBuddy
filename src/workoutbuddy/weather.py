from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
LOCAL_FALLBACK_TZ = ZoneInfo("Europe/Vienna")
LANDSKRON_LAT = 46.6368
LANDSKRON_LON = 13.8960
VIRTUAL_ACTIVITY_TYPES = {"virtualride", "virtualrun"}


@dataclass(frozen=True)
class WeatherTimeRef:
    """Temporal reference for matching an activity to archive weather.

    Correct convention:
    - trailing Z is always true Zulu/UTC.
    - explicit +HH:MM / -HH:MM offsets are true instants.
    - only timestamps without offset are local clock times.

    This means 13:10Z is converted to the coordinate-local time before matching
    the Open-Meteo hourly weather. In Austria during CEST, that corresponds to
    roughly 15:10 local.
    """

    kind: str  # "utc_instant" or "local_clock"
    dt: datetime
    source_label: str


def update_activity_weather_from_archive(db: Any, activity_id: str, timeout_s: int = 20) -> Optional[Dict[str, Any]]:
    """Store start/end averaged historical conditions for one activity."""
    row = db.read_activity_row(activity_id)
    if not row:
        return None

    time_ref = _build_weather_time_ref(row)
    if time_ref is None:
        return None

    endpoints = _route_endpoints_from_streams(row.get("streams_json_path"))
    use_landskron = _is_virtual_activity(row.get("sport_type")) or endpoints is None
    if use_landskron:
        start_latlon = end_latlon = (LANDSKRON_LAT, LANDSKRON_LON)
        location_source = "Landskron fallback (virtual/no GPS)"
    else:
        start_latlon, end_latlon = endpoints
        location_source = "route start/end nearest archive grid"

    elapsed_s = _safe_float(row.get("elapsed_time_s"))
    moving_s = _safe_float(row.get("moving_time_s"))
    duration_s = elapsed_s if elapsed_s is not None and elapsed_s > 0 else (moving_s or 0.0)
    end_time_ref = _shift_time_ref(time_ref, max(0.0, duration_s))

    start_result = fetch_archive_temperature(
        db, start_latlon[0], start_latlon[1], time_ref, timeout_s=timeout_s,
    )
    end_result = fetch_archive_temperature(
        db, end_latlon[0], end_latlon[1], end_time_ref, timeout_s=timeout_s,
    )
    if start_result is None and end_result is None:
        return None

    result = _average_weather_conditions(start_result, end_result)
    result["start"] = start_result
    result["end"] = end_result
    result["location_source"] = location_source
    result["source"] = (
        f"open-meteo historical start/end average; {location_source}; "
        f"duration_source={'elapsed_time_s' if elapsed_s else 'moving_time_s'}"
    )

    db.set_activity_weather(
        activity_id=activity_id,
        temp_c=result.get("temp_c"),
        apparent_temp_c=result.get("apparent_temp_c"),
        latitude=start_latlon[0],
        longitude=start_latlon[1],
        end_latitude=end_latlon[0],
        end_longitude=end_latlon[1],
        humidity_pct=result.get("humidity_pct"),
        cloud_cover_pct=result.get("cloud_cover_pct"),
        shortwave_radiation_w_m2=result.get("shortwave_radiation_w_m2"),
        direct_radiation_w_m2=result.get("direct_radiation_w_m2"),
        start_temp_c=(start_result or {}).get("temp_c"),
        end_temp_c=(end_result or {}).get("temp_c"),
        start_humidity_pct=(start_result or {}).get("humidity_pct"),
        end_humidity_pct=(end_result or {}).get("humidity_pct"),
        start_cloud_cover_pct=(start_result or {}).get("cloud_cover_pct"),
        end_cloud_cover_pct=(end_result or {}).get("cloud_cover_pct"),
        start_shortwave_radiation_w_m2=(start_result or {}).get("shortwave_radiation_w_m2"),
        end_shortwave_radiation_w_m2=(end_result or {}).get("shortwave_radiation_w_m2"),
        source=result.get("source") or "open-meteo historical start/end average",
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )
    return result


def update_missing_weather_for_recent_activities(db: Any, limit: int = 300, progress=None) -> Dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT m.activity_id, m.weather_temp_c, m.weather_humidity_pct,
                   m.weather_cloud_cover_pct, m.weather_shortwave_radiation_w_m2,
                   a.streams_json_path
            FROM metrics m
            JOIN activities a ON a.activity_id = m.activity_id
            ORDER BY m.start_date_local DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

    updated = 0
    skipped = 0
    failed = 0

    for r in rows:
        activity_id = str(r["activity_id"])
        if all(r[key] is not None for key in [
            "weather_temp_c", "weather_humidity_pct",
            "weather_cloud_cover_pct", "weather_shortwave_radiation_w_m2",
        ]):
            skipped += 1
            continue

        try:
            res = update_activity_weather_from_archive(db, activity_id)
            if res is None:
                skipped += 1
            else:
                updated += 1
                if progress:
                    progress(
                        f"Weather archive: {activity_id} -> {res.get('temp_c')} °C average "
                        f"from {res.get('local_hour')} ({res.get('time_source')})"
                    )
        except Exception as e:
            failed += 1
            if progress:
                progress(f"Weather archive failed for {activity_id}: {e}")

    return {"updated": updated, "skipped": skipped, "failed": failed}


def fetch_archive_temperature(
    db: Any,
    lat: float,
    lon: float,
    time_ref: WeatherTimeRef | datetime,
    timeout_s: int = 20,
) -> Optional[Dict[str, Any]]:
    """Fetch and interpolate historical conditions at one coordinate/time."""
    if isinstance(time_ref, datetime):
        if time_ref.tzinfo is None:
            ref = WeatherTimeRef("local_clock", time_ref.replace(tzinfo=None), "legacy-naive-local-clock")
        else:
            ref = WeatherTimeRef("utc_instant", time_ref, "legacy-aware-zulu-or-offset")
    else:
        ref = time_ref

    cache_basis = ref.dt.replace(second=0, microsecond=0)
    cache_key = f"openmeteo:v7:{round(lat, 3)}:{round(lon, 3)}:{ref.kind}:{cache_basis.isoformat()}"
    cached = db.get_weather_cache(cache_key)
    if cached and cached.get("temp_c") is not None:
        return {
            "temp_c": _safe_float(cached.get("temp_c")),
            "apparent_temp_c": _safe_float(cached.get("apparent_temp_c")),
            "humidity_pct": _safe_float(cached.get("humidity_pct")),
            "cloud_cover_pct": _safe_float(cached.get("cloud_cover_pct")),
            "shortwave_radiation_w_m2": _safe_float(cached.get("shortwave_radiation_w_m2")),
            "direct_radiation_w_m2": _safe_float(cached.get("direct_radiation_w_m2")),
            "source": cached.get("source") or "open-meteo archive cache",
            "local_hour": cached.get("local_hour"),
            "time_source": ref.source_label,
        }

    # Request a 3-day window to cover UTC/local midnight and DST boundaries.
    if ref.kind == "local_clock":
        center_date = ref.dt.date()
    else:
        dt_utc = ref.dt.astimezone(timezone.utc) if ref.dt.tzinfo else ref.dt.replace(tzinfo=timezone.utc)
        center_date = dt_utc.astimezone(LOCAL_FALLBACK_TZ).date()

    params = {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "start_date": (center_date - timedelta(days=1)).isoformat(),
        "end_date": (center_date + timedelta(days=1)).isoformat(),
        "hourly": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "cloud_cover,shortwave_radiation,direct_radiation"
        ),
        "timezone": "auto",
        "cell_selection": "nearest",
    }

    resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=timeout_s)
    if resp.status_code >= 400:
        return None

    data = resp.json()
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times or not (hourly.get("temperature_2m") or []):
        return None

    offset_s = int(data.get("utc_offset_seconds") or 0)
    target_local = _time_ref_to_openmeteo_local(ref, offset_s)

    values = {
        "temp_c": _interpolate_hourly(times, hourly.get("temperature_2m") or [], target_local),
        "apparent_temp_c": _interpolate_hourly(times, hourly.get("apparent_temperature") or [], target_local),
        "humidity_pct": _interpolate_hourly(times, hourly.get("relative_humidity_2m") or [], target_local),
        "cloud_cover_pct": _interpolate_hourly(times, hourly.get("cloud_cover") or [], target_local),
        "shortwave_radiation_w_m2": _interpolate_hourly(times, hourly.get("shortwave_radiation") or [], target_local),
        "direct_radiation_w_m2": _interpolate_hourly(times, hourly.get("direct_radiation") or [], target_local),
    }
    if values["temp_c"] is None:
        return None
    local_hour = target_local.isoformat(timespec="minutes")

    source = (
        "open-meteo historical nearest grid; exact-minute linear interpolation"
        f"; target_local={local_hour}"
        f"; time_source={ref.source_label}"
    )

    db.set_weather_cache(
        cache_key, lat, lon, local_hour,
        values["temp_c"], values["apparent_temp_c"], source, json.dumps(data)[:5000],
        humidity_pct=values["humidity_pct"],
        cloud_cover_pct=values["cloud_cover_pct"],
        shortwave_radiation_w_m2=values["shortwave_radiation_w_m2"],
        direct_radiation_w_m2=values["direct_radiation_w_m2"],
    )

    return {
        **values,
        "source": source,
        "local_hour": local_hour,
        "time_source": ref.source_label,
        "archive_lat": _safe_float(data.get("latitude")),
        "archive_lon": _safe_float(data.get("longitude")),
    }


def _shift_time_ref(ref: WeatherTimeRef, seconds: float) -> WeatherTimeRef:
    return WeatherTimeRef(ref.kind, ref.dt + timedelta(seconds=float(seconds)), ref.source_label + "+duration")


def _average_weather_conditions(
    start: Optional[Dict[str, Any]],
    end: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in [
        "temp_c", "apparent_temp_c", "humidity_pct", "cloud_cover_pct",
        "shortwave_radiation_w_m2", "direct_radiation_w_m2",
    ]:
        values = [
            value for value in [
                _safe_float((start or {}).get(key)),
                _safe_float((end or {}).get(key)),
            ]
            if value is not None
        ]
        out[key] = sum(values) / len(values) if values else None
    out["local_hour"] = (
        f"{(start or {}).get('local_hour', '?')} -> {(end or {}).get('local_hour', '?')}"
    )
    out["time_source"] = (start or end or {}).get("time_source")
    return out


def _interpolate_hourly(times: List[str], values: List[Any], target_local: datetime) -> Optional[float]:
    points: List[Tuple[datetime, float]] = []
    for raw_time, raw_value in zip(times, values):
        value = _safe_float(raw_value)
        if value is None:
            continue
        try:
            point_time = datetime.fromisoformat(str(raw_time)).replace(tzinfo=None)
        except Exception:
            continue
        points.append((point_time, value))
    if not points:
        return None

    target = target_local.replace(tzinfo=None)
    if target <= points[0][0]:
        return points[0][1]
    if target >= points[-1][0]:
        return points[-1][1]

    for (left_time, left_value), (right_time, right_value) in zip(points, points[1:]):
        if left_time <= target <= right_time:
            width_s = (right_time - left_time).total_seconds()
            if width_s <= 0:
                return left_value
            fraction = (target - left_time).total_seconds() / width_s
            return left_value + (right_value - left_value) * fraction
    return None


def _is_virtual_activity(sport_type: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(sport_type or "").lower())
    return normalized in VIRTUAL_ACTIVITY_TYPES


def _build_weather_time_ref(row: Dict[str, Any]) -> Optional[WeatherTimeRef]:
    """Choose correct time interpretation for weather lookup.

    This is intentionally stricter than duplicate detection:
    for weather we need the real hour. So if the string says Z, it is UTC.
    """
    source = str(row.get("source") or "").lower()
    raw = _read_raw_activity_json(row.get("raw_json_path"))

    raw_start_date = _nested_get(raw, "detail", "start_date") or _nested_get(raw, "summary", "start_date")
    raw_start_local = _nested_get(raw, "detail", "start_date_local") or _nested_get(raw, "summary", "start_date_local")
    metric_start_local = row.get("start_date_local")

    if "strava" in source:
        # Strava start_date is the canonical true UTC instant.
        dt = _parse_aware_instant(raw_start_date)
        if dt is not None:
            return WeatherTimeRef("utc_instant", dt, "strava-start_date-utc-zulu")

        # If a local-looking Strava field has Z/offset, respect that marker.
        dt = _parse_aware_instant(raw_start_local)
        if dt is not None:
            return WeatherTimeRef("utc_instant", dt, "strava-start_date_local-explicit-zulu-or-offset")

        dt = _parse_aware_instant(metric_start_local)
        if dt is not None:
            return WeatherTimeRef("utc_instant", dt, "metric-start-date-explicit-zulu-or-offset")

        # Only offset-less fields are local clock times.
        dt_local = _parse_local_clock_if_no_offset(raw_start_local or metric_start_local)
        if dt_local is not None:
            return WeatherTimeRef("local_clock", dt_local, "strava-start_date_local-naive-local-clock")

    # TCX/GPX/local files: explicit Z/offset is a true instant.
    dt = _parse_aware_instant(metric_start_local)
    if dt is not None:
        return WeatherTimeRef("utc_instant", dt, "file-or-metric-explicit-zulu-or-offset")

    # Offset-less local file times are local clock times.
    dt_local = _parse_local_clock_if_no_offset(metric_start_local)
    if dt_local is not None:
        return WeatherTimeRef("local_clock", dt_local, "file-or-metric-naive-local-clock")

    # Fallback to raw local field only if offset-less.
    dt_local = _parse_local_clock_if_no_offset(raw_start_local)
    if dt_local is not None:
        return WeatherTimeRef("local_clock", dt_local, "fallback-raw-local-naive-clock")

    return None


def _read_raw_activity_json(path: Any) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        p = Path(str(path))
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _nested_get(obj: Dict[str, Any], *keys: str) -> Any:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _route_endpoints_from_streams(
    path: Any,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    if not path:
        return None
    try:
        p = Path(str(path))
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    pts = None
    obj = data.get("latlng") if isinstance(data, dict) else None
    if isinstance(obj, dict):
        pts = obj.get("data")
    elif isinstance(obj, list):
        pts = obj

    if not isinstance(pts, list):
        return None

    valid: List[Tuple[float, float]] = []
    for pnt in pts:
        try:
            lat, lon = float(pnt[0]), float(pnt[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                valid.append((lat, lon))
        except Exception:
            continue
    if not valid:
        return None
    return valid[0], valid[-1]


def _first_latlon_from_streams(path: Any) -> Optional[Tuple[float, float]]:
    """Compatibility wrapper for callers which only need the route start."""
    endpoints = _route_endpoints_from_streams(path)
    return endpoints[0] if endpoints else None


def _parse_activity_time(value: Any) -> Optional[datetime]:
    # Kept for compatibility with older callers/tests.
    return _parse_aware_or_naive(value)


def _parse_aware_or_naive(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _has_explicit_offset(value: Any) -> bool:
    if not value:
        return False
    s = str(value).strip()
    if s.endswith("Z"):
        return True
    return bool(re.search(r"[+-]\d\d:?\d\d$", s))


def _parse_local_clock(value: Any) -> Optional[datetime]:
    """Parse an ISO-like timestamp as local clock, ignoring offset/Z.

    This is only used after checking that the original timestamp has no explicit
    offset. Do not call this on a Z timestamp unless you explicitly want to strip
    the timezone.
    """
    if not value:
        return None
    s = str(value).strip()
    s = re.sub(r"Z$", "", s)
    s = re.sub(r"([+-]\d\d:?\d\d)$", "", s)
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        return None


def _parse_local_clock_if_no_offset(value: Any) -> Optional[datetime]:
    if not value or _has_explicit_offset(value):
        return None
    return _parse_local_clock(value)


def _parse_aware_instant(value: Any) -> Optional[datetime]:
    if not value or not _has_explicit_offset(value):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt
    except Exception:
        return None


def _activity_time_to_local_clock(dt: datetime) -> datetime:
    # Compatibility helper.
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(LOCAL_FALLBACK_TZ).replace(tzinfo=None)


def _activity_time_to_openmeteo_local(dt: datetime, utc_offset_seconds: int) -> datetime:
    # Compatibility helper.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=None)
    return (dt.astimezone(timezone.utc) + timedelta(seconds=utc_offset_seconds)).replace(tzinfo=None)


def _time_ref_to_openmeteo_local(ref: WeatherTimeRef, utc_offset_seconds: int) -> datetime:
    if ref.kind == "local_clock":
        return ref.dt.replace(tzinfo=None)

    dt = ref.dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return (dt.astimezone(timezone.utc) + timedelta(seconds=utc_offset_seconds)).replace(tzinfo=None)


def _nearest_hour_index(times: List[str], target_local: datetime) -> Optional[int]:
    best_idx = None
    best_diff = None

    target_hour = target_local.replace(minute=0, second=0, microsecond=0)
    if target_local.minute >= 30:
        target_hour = target_hour + timedelta(hours=1)

    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(str(t)).replace(tzinfo=None)
        except Exception:
            continue

        diff = abs((dt - target_hour).total_seconds())
        if best_diff is None or diff < best_diff:
            best_idx = i
            best_diff = diff

    if best_idx is None or best_diff is None or best_diff > 2 * 3600:
        return None

    return best_idx


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None
