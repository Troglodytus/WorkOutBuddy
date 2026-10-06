#!/usr/bin/env python3
"""One-time migration of the existing WorkOutBuddy SQLite data to Supabase.

Required environment variables:
  SUPABASE_URL
  SUPABASE_SECRET_KEY      # secret/service-role key; NEVER put this in browser code
  WORKOUTBUDDY_USER_ID     # UUID of the Auth user created for WorkOutBuddy

Optional:
  WORKOUTBUDDY_DB=data/workoutbuddy.sqlite

Run from the repository root:
  python tools/migrate_sqlite_to_supabase.py
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("WORKOUTBUDDY_DB", ROOT / "data" / "workoutbuddy.sqlite"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SECRET = os.environ.get("SUPABASE_SECRET_KEY", "")
USER_ID = os.environ.get("WORKOUTBUDDY_USER_ID", "")
TIMEZONE = ZoneInfo("Europe/Vienna")


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(2)


def clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, bytes):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return str(value)


def f(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            x = float(value)
            if math.isfinite(x):
                return x
        except Exception:
            pass
    return None


def pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def canonical_time(value: Any) -> str:
    if not value:
        return datetime.now(TIMEZONE).isoformat()
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt.isoformat()
    except Exception:
        return text


def sport_category(value: Any) -> str:
    s = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    if "run" in s:
        return "run"
    if any(x in s for x in ("ride", "bike", "cycling", "ergo")):
        return "bike"
    if any(x in s for x in ("strength", "weight")):
        return "strength"
    if any(x in s for x in ("hike", "walk")):
        return "hike"
    return "other"


def headers() -> dict[str, str]:
    return {
        "apikey": SECRET,
        "Authorization": f"Bearer {SECRET}",
        "Content-Type": "application/json",
    }


def request(method: str, path: str, **kwargs):
    r = requests.request(method, SUPABASE_URL + path, headers=headers(), timeout=60, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:1000]}")
    if not r.text:
        return None
    return r.json()


def resolve_path(value: Any) -> Path | None:
    if not value:
        return None
    p = Path(str(value))
    candidates = [p, ROOT / p, DB_PATH.parent / p, ROOT / "data" / "streams" / p.name]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def read_json_file(value: Any) -> Any:
    p = resolve_path(value)
    if not p:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _stream_values(data: Any, key: str) -> list[Any]:
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return value["data"]
    if isinstance(value, list):
        return value
    return []


def normalize_stream_json(data: Any, max_points: int = 4000) -> dict[str, Any]:
    """Convert legacy Strava/TCX stream dictionaries to the browser's point format."""
    if not isinstance(data, dict):
        return {"points": []}
    if isinstance(data.get("points"), list):
        points = data["points"]
    else:
        time = _stream_values(data, "time")
        latlng = _stream_values(data, "latlng")
        hr = _stream_values(data, "heartrate")
        altitude = _stream_values(data, "altitude")
        distance = _stream_values(data, "distance")
        speed = _stream_values(data, "velocity_smooth")
        cadence = _stream_values(data, "cadence")
        watts = _stream_values(data, "watts")
        n_points = max(map(len, [time, latlng, hr, altitude, distance, speed, cadence, watts]), default=0)
        points = []
        for i in range(n_points):
            ll = latlng[i] if i < len(latlng) and isinstance(latlng[i], (list, tuple)) and len(latlng[i]) >= 2 else [None, None]
            points.append({
                "elapsed_s": time[i] if i < len(time) else None,
                "lat": ll[0],
                "lon": ll[1],
                "hr": hr[i] if i < len(hr) else None,
                "alt": altitude[i] if i < len(altitude) else None,
                "distance_m": distance[i] if i < len(distance) else None,
                "speed_mps": speed[i] if i < len(speed) else None,
                "cadence": cadence[i] if i < len(cadence) else None,
                "watts": watts[i] if i < len(watts) else None,
            })
    if len(points) > max_points:
        if max_points <= 1:
            points = points[:1]
        else:
            step = (len(points) - 1) / (max_points - 1)
            points = [points[min(len(points)-1, round(i * step))] for i in range(max_points)]
    return {"points": clean(points)}


def main() -> None:
    if not SUPABASE_URL or ".supabase.co" not in SUPABASE_URL:
        fail("Set SUPABASE_URL.")
    if not SECRET:
        fail("Set SUPABASE_SECRET_KEY. Use it only for this local migration.")
    try:
        uuid.UUID(USER_ID)
    except Exception:
        fail("Set WORKOUTBUDDY_USER_ID to the UUID from Supabase Authentication -> Users.")
    if not DB_PATH.exists():
        fail(f"SQLite database not found: {DB_PATH}")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        activities = [dict(r) for r in conn.execute("select * from activities").fetchall()]
        metrics = {}
        try:
            metrics = {str(r["activity_id"]): dict(r) for r in conn.execute("select * from metrics").fetchall()}
        except sqlite3.OperationalError:
            pass

    print(f"Found {len(activities)} activities in {DB_PATH}")
    imported = skipped = stream_count = 0

    for idx, a in enumerate(activities, start=1):
        old_id = str(a.get("activity_id"))
        row = dict(a)
        row.update(metrics.get(old_id, {}))

        start = canonical_time(pick(row, "start_date_local", "start_date", "start_time"))
        sport = str(pick(row, "sport_type", "type", "sport") or "Workout")
        duration = f(row, "moving_time_s", "elapsed_time_s")
        distance = f(row, "distance_m")
        dedupe = "|".join([
            start[:19],
            sport_category(sport),
            str(round((duration or 0) / 10)),
            str(round((distance or 0) / 10)),
        ])

        existing = request(
            "GET",
            "/rest/v1/activities?user_id=eq."
            + quote(USER_ID)
            + "&dedupe_key=eq."
            + quote(dedupe, safe="")
            + "&select=id&limit=1",
        )
        if existing:
            skipped += 1
            print(f"[{idx}/{len(activities)}] skip duplicate {old_id}")
            continue

        payload = {
            "id": str(uuid.uuid4()),
            "user_id": USER_ID,
            "source": str(pick(row, "source") or "legacy_sqlite"),
            "source_external_id": old_id,
            "name": str(pick(row, "name") or sport),
            "sport_type": sport,
            "sport_category": sport_category(sport),
            "start_time": start,
            "original_start_time": str(pick(row, "start_date_local", "start_date") or start),
            "local_timezone": "Europe/Vienna",
            "duration_s": duration,
            "moving_time_s": f(row, "moving_time_s"),
            "elapsed_time_s": f(row, "elapsed_time_s"),
            "distance_m": distance,
            "elevation_gain_m": f(row, "elevation_gain_m", "total_elevation_gain", "elev_gain_m"),
            "elevation_loss_m": f(row, "elevation_loss_m"),
            "avg_hr": f(row, "avg_hr", "average_heartrate"),
            "max_hr": f(row, "max_hr", "max_heartrate"),
            "avg_pace_min_km": f(row, "avg_pace_min_km", "avg_pace_min_per_km"),
            "avg_gap_pace_min_km": f(row, "avg_gap_pace_min_km"),
            "training_load_score": f(row, "training_load_score"),
            "trimp_score": f(row, "trimp_score"),
            "easy_zone_fraction": f(row, "easy_zone_fraction"),
            "hard_zone_fraction": f(row, "hard_zone_fraction"),
            "z1_s": f(row, "z1_s"),
            "z2_s": f(row, "z2_s"),
            "z3_s": f(row, "z3_s"),
            "z4_s": f(row, "z4_s"),
            "z5_s": f(row, "z5_s"),
            "apple_vo2max": f(row, "apple_vo2max"),
            "own_vo2max_estimate": f(row, "own_vo2max_estimate"),
            "estimated_vo2max": f(row, "estimated_vo2max"),
            "body_weight_kg": f(row, "body_weight_kg", "body_weight_kg_effective"),
            "normalized_power": f(row, "normalized_power"),
            "average_power": f(row, "avg_power", "average_power", "cycling_power_w"),
            "cadence_spm": f(row, "run_step_frequency_spm", "cadence"),
            "hr_efficiency_drift_pct": f(row, "hr_efficiency_drift_pct"),
            "gap_hr_efficiency_drift_pct": f(row, "gap_hr_efficiency_drift_pct", "grade_adjusted_hr_efficiency_drift_pct"),
            "km_gap_hr_efficiency_drift_pct": f(row, "km_gap_hr_efficiency_drift_pct"),
            "dedupe_key": dedupe,
            "metrics_json": clean(row),
        }

        created = request(
            "POST",
            "/rest/v1/activities",
            data=json.dumps(clean(payload)),
            params={"select": "id"},
        )
        new_id = payload["id"]
        imported += 1

        stream_json = read_json_file(row.get("streams_json_path"))
        if stream_json is not None:
            normalized_stream = normalize_stream_json(stream_json)
            request(
                "POST",
                "/rest/v1/activity_streams",
                data=json.dumps({
                    "activity_id": new_id,
                    "user_id": USER_ID,
                    "stream_data": normalized_stream,
                }),
            )
            stream_count += 1

        print(f"[{idx}/{len(activities)}] imported {old_id} -> {new_id}")

    print()
    print(f"Done. Imported {imported}, skipped {skipped}, migrated streams for {stream_count} activities.")


if __name__ == "__main__":
    main()
