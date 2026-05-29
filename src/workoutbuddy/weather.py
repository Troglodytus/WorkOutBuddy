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
    """Fetch archived weather for an activity and store temperature in DB.

    Uses the first available GPS coordinate from the stream JSON and the activity
    starting time.

    Important time handling:
    - Z means UTC/Zulu and is converted to the coordinate-local weather hour.
    - Offset-less timestamps are treated as local clock time.
    """
    row = db.read_activity_row(activity_id)
    if not row:
        return None

    time_ref = _build_weather_time_ref(row)
    if time_ref is None:
        return None

    latlon = _first_latlon_from_streams(row.get("streams_json_path"))
    if latlon is None:
        return None

    lat, lon = latlon
    result = fetch_archive_temperature(db, lat, lon, time_ref, timeout_s=timeout_s)
    if result is None:
        return None

    db.set_activity_weather(
        activity_id=activity_id,
        temp_c=result.get("temp_c"),
        apparent_temp_c=result.get("apparent_temp_c"),
        latitude=lat,
        longitude=lon,
        source=result.get("source") or "open-meteo archive",
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )
    return result


def update_missing_weather_for_recent_activities(db: Any, limit: int = 300, progress=None) -> Dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT m.activity_id, m.weather_temp_c, a.streams_json_path
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
        if r["weather_temp_c"] is not None:
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
                        f"Weather archive: {activity_id} -> {res.get('temp_c')} °C "
                        f"at local hour {res.get('local_hour')} ({res.get('time_source')})"
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
    """Fetch hourly archive temperature for one activity.

    Backwards-compatible: if a plain datetime is passed:
    - aware datetime = true instant
    - naive datetime = local clock
    """
    if isinstance(time_ref, datetime):
        if time_ref.tzinfo is None:
            ref = WeatherTimeRef("local_clock", time_ref.replace(tzinfo=None), "legacy-naive-local-clock")
        else:
            ref = WeatherTimeRef("utc_instant", time_ref, "legacy-aware-zulu-or-offset")
    else:
        ref = time_ref

    # For true instants, final matching needs Open-Meteo's coordinate-specific
    # utc_offset_seconds. The cache key therefore includes the instant/source kind.
    cache_basis = ref.dt.replace(minute=0, second=0, microsecond=0)
    cache_key = f"openmeteo:v5:{round(lat, 3)}:{round(lon, 3)}:{ref.kind}:{cache_basis.isoformat()}"
    cached = db.get_weather_cache(cache_key)
    if cached and cached.get("temp_c") is not None:
        return {
            "temp_c": _safe_float(cached.get("temp_c")),
            "apparent_temp_c": _safe_float(cached.get("apparent_temp_c")),
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
        "hourly": "temperature_2m,apparent_temperature",
        "timezone": "auto",
    }

    resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=timeout_s)
    if resp.status_code >= 400:
        return None

    data = resp.json()
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    apparent = hourly.get("apparent_temperature") or []

    if not times or not temps:
        return None

    offset_s = int(data.get("utc_offset_seconds") or 0)
    target_local = _time_ref_to_openmeteo_local(ref, offset_s)

    idx = _nearest_hour_index(times, target_local)
    if idx is None:
        return None

    temp_c = _safe_float(temps[idx] if idx < len(temps) else None)
    app_c = _safe_float(apparent[idx] if idx < len(apparent) else None)
    local_hour = str(times[idx])

    source = (
        "open-meteo archive temperature_2m"
        f"; matched_local_hour={local_hour}"
        f"; target_local={target_local.isoformat(timespec='minutes')}"
        f"; time_source={ref.source_label}"
    )

    db.set_weather_cache(cache_key, lat, lon, local_hour, temp_c, app_c, source, json.dumps(data)[:5000])

    return {
        "temp_c": temp_c,
        "apparent_temp_c": app_c,
        "source": source,
        "local_hour": local_hour,
        "time_source": ref.source_label,
    }


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


def _first_latlon_from_streams(path: Any) -> Optional[Tuple[float, float]]:
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

    for pnt in pts:
        try:
            lat, lon = float(pnt[0]), float(pnt[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except Exception:
            continue

    return None


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
