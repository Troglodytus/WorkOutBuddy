from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

LOCAL_TZ_NAME = "Europe/Vienna"

RUN_TYPES = {"run", "running", "trailrun", "trail_run", "virtualrun", "virtual_run"}
RIDE_TYPES = {"ride", "biking", "bike", "cycling", "virtualride", "virtual_ride", "mountainbikeride", "mountain_bike_ride", "gravelride", "gravel_ride", "ebikeride", "e_bike_ride"}


def cleanup_duplicate_activities(db: Any) -> Dict[str, Any]:
    rows = _read_rows(db)
    removed: List[Dict[str, Any]] = []
    pairs_checked = 0

    # Compare only plausible pairs. O(n²) is fine for the typical local training DB.
    for i in range(len(rows)):
        a = rows[i]
        if a.get("_deleted"):
            continue
        for j in range(i + 1, len(rows)):
            b = rows[j]
            if b.get("_deleted"):
                continue
            pairs_checked += 1
            match = _duplicate_match(a, b)
            if not match["is_duplicate"]:
                continue
            keep, delete = _choose_keep_delete(a, b)
            if not keep or not delete:
                continue
            db.delete_activity(str(delete["activity_id"]))
            delete["_deleted"] = True
            removed.append({
                "deleted_activity_id": str(delete["activity_id"]),
                "deleted_source": delete.get("source"),
                "kept_activity_id": str(keep["activity_id"]),
                "kept_source": keep.get("source"),
                "reason": match["reason"],
                "score": match["score"],
            })
            break

    return {
        "pairs_checked": pairs_checked,
        "duplicates_removed": len(removed),
        "removed": removed,
    }


def _read_rows(db: Any) -> List[Dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT m.*, a.source, a.raw_json_path, a.streams_json_path, a.synced_at
            FROM metrics m
            JOIN activities a ON a.activity_id = m.activity_id
            ORDER BY m.start_date_local ASC
            """
        ).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        pts = _load_points(r.get("streams_json_path"))
        r["_points"] = pts
        r["_start_point"] = pts[0] if pts else None
    return out


def _duplicate_match(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    sport_a = _norm_sport(a.get("sport_type"))
    sport_b = _norm_sport(b.get("sport_type"))
    if sport_a and sport_b and sport_a != sport_b:
        # Treat close ride aliases/running aliases as equivalent, otherwise reject.
        if not ((_is_run(sport_a) and _is_run(sport_b)) or (_is_ride(sport_a) and _is_ride(sport_b))):
            return {"is_duplicate": False, "score": 9999, "reason": "different sport"}

    time_diff = _minimum_start_time_difference_s(a.get("start_date_local"), b.get("start_date_local"))
    dur_a = _safe_float(a.get("moving_time_s"))
    dur_b = _safe_float(b.get("moving_time_s"))
    dist_a = _safe_float(a.get("distance_m"))
    dist_b = _safe_float(b.get("distance_m"))

    duration_ok = False
    duration_diff = None
    if dur_a and dur_b and dur_a > 0 and dur_b > 0:
        duration_diff = abs(dur_a - dur_b)
        duration_ok = duration_diff <= max(300.0, 0.12 * max(dur_a, dur_b))

    distance_ok = False
    distance_diff = None
    if dist_a and dist_b and dist_a > 0 and dist_b > 0:
        distance_diff = abs(dist_a - dist_b)
        distance_ok = distance_diff <= _distance_tolerance_m(sport_a or sport_b, max(dist_a, dist_b))

    start_gps_m = None
    start_gps_ok = False
    if a.get("_start_point") and b.get("_start_point"):
        start_gps_m = _haversine_m(a["_start_point"], b["_start_point"])
        start_gps_ok = start_gps_m <= 350.0

    route_overlap_m = None
    route_ok = False
    if len(a.get("_points") or []) >= 10 and len(b.get("_points") or []) >= 10:
        route_overlap_m = _mean_sampled_route_distance_m(a["_points"], b["_points"])
        route_ok = route_overlap_m <= 600.0

    time_ok = time_diff is not None and time_diff <= 900.0
    loose_time_ok = time_diff is not None and time_diff <= 3.25 * 3600.0

    # Enough overlap combinations. Track/start GPS can rescue timezone-confused exports,
    # but time must still be plausible within a few hours.
    strong_non_gps = time_ok and duration_ok and distance_ok
    gps_confirmed = loose_time_ok and duration_ok and distance_ok and (start_gps_ok or route_ok)
    route_confirmed = loose_time_ok and distance_ok and route_ok

    is_dup = bool(strong_non_gps or gps_confirmed or route_confirmed)
    score = 0.0
    if time_diff is not None:
        score += min(time_diff, 7200) / 20.0
    if duration_diff is not None:
        score += duration_diff / 20.0
    if distance_diff is not None:
        score += distance_diff / 30.0
    if start_gps_m is not None:
        score += min(start_gps_m, 2000) / 10.0
    if route_overlap_m is not None:
        score += min(route_overlap_m, 3000) / 30.0

    reason = (
        f"time_diff={time_diff:.0f}s" if time_diff is not None else "time_diff=n/a"
    ) + (
        f", duration_diff={duration_diff:.0f}s" if duration_diff is not None else ", duration_diff=n/a"
    ) + (
        f", distance_diff={distance_diff:.0f}m" if distance_diff is not None else ", distance_diff=n/a"
    ) + (
        f", start_gps={start_gps_m:.0f}m" if start_gps_m is not None else ", start_gps=n/a"
    ) + (
        f", route_mean={route_overlap_m:.0f}m" if route_overlap_m is not None else ", route_mean=n/a"
    )
    return {"is_duplicate": is_dup, "score": round(score, 1), "reason": reason}


def _choose_keep_delete(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    a_tcx = str(a.get("source") or "").startswith("tcx")
    b_tcx = str(b.get("source") or "").startswith("tcx")
    if a_tcx and not b_tcx:
        return a, b
    if b_tcx and not a_tcx:
        return b, a
    # If both are the same source class, keep the richer stream row and delete the poorer one.
    rich_a = _richness(a)
    rich_b = _richness(b)
    if rich_a > rich_b:
        return a, b
    if rich_b > rich_a:
        return b, a
    # Avoid deleting arbitrary same-quality rows unless one is clearly Strava/API.
    if not a_tcx and not b_tcx:
        return None, None
    return a, b


def _richness(r: Dict[str, Any]) -> float:
    return (
        float(r.get("gps_point_count") or 0) * 2.0
        + float(r.get("data_point_count") or 0)
        + 100.0 * float(r.get("has_hr") or 0)
        + 100.0 * float(r.get("has_power") or 0)
    )


def _load_points(path: Any) -> List[Tuple[float, float]]:
    if not path:
        return []
    try:
        p = Path(str(path))
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    obj = data.get("latlng") if isinstance(data, dict) else None
    if isinstance(obj, dict):
        pts = obj.get("data") or []
    elif isinstance(obj, list):
        pts = obj
    else:
        pts = []
    out: List[Tuple[float, float]] = []
    for pnt in pts:
        try:
            lat, lon = float(pnt[0]), float(pnt[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                out.append((lat, lon))
        except Exception:
            continue
    return out


def _dt_variants(value: Any) -> List[datetime]:
    """Return plausible clock variants for duplicate detection.

    This is intentionally more permissive than weather lookup:
    duplicate detection should notice that 13:10Z and 15:10 local are the same
    workout during CEST. Therefore, for an aware/Zulu timestamp, variants include
    both the true UTC clock and the Europe/Vienna local clock equivalent.
    For a naive timestamp, variants include the raw local clock and conversions
    under both local-as-source and UTC-as-source assumptions.

    The duplicate decision still also requires duration/distance/GPS/route overlap,
    so this permissive time matching should not delete unrelated workouts by time
    alone.
    """
    if not value:
        return []
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return []
    local_tz = ZoneInfo(LOCAL_TZ_NAME)
    out: List[datetime] = []
    if dt.tzinfo is not None:
        out.append(dt.astimezone(timezone.utc).replace(tzinfo=None))       # true UTC instant
        out.append(dt.astimezone(local_tz).replace(tzinfo=None))           # local clock equivalent
        out.append(dt.replace(tzinfo=None))                                # raw clock from file
    else:
        out.append(dt)                                                     # local/raw clock
        try:
            out.append(dt.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None))
            out.append(dt.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None))
        except Exception:
            pass
    # Deduplicate within one second.
    uniq: List[datetime] = []
    for x in out:
        if not any(abs((x - y).total_seconds()) < 1 for y in uniq):
            uniq.append(x)
    return uniq


def _minimum_start_time_difference_s(a: Any, b: Any) -> Optional[float]:
    va, vb = _dt_variants(a), _dt_variants(b)
    if not va or not vb:
        return None
    return min(abs((x - y).total_seconds()) for x in va for y in vb)


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000.0 * 2.0 * math.asin(min(1.0, math.sqrt(h)))


def _mean_sampled_route_distance_m(a: Sequence[Tuple[float, float]], b: Sequence[Tuple[float, float]], samples: int = 20) -> float:
    n = min(samples, len(a), len(b))
    if n <= 1:
        return float("inf")
    vals: List[float] = []
    for k in range(n):
        ia = round(k * (len(a) - 1) / (n - 1))
        ib = round(k * (len(b) - 1) / (n - 1))
        vals.append(_haversine_m(a[ia], b[ib]))
    return float(sum(vals) / len(vals))


def _norm_sport(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _is_run(norm_value: str) -> bool:
    return norm_value in {x.replace("_", "") for x in RUN_TYPES}


def _is_ride(norm_value: str) -> bool:
    return norm_value in {x.replace("_", "") for x in RIDE_TYPES}


def _distance_tolerance_m(norm_sport: str, distance_m: float) -> float:
    if _is_run(norm_sport):
        return max(300.0, distance_m * 0.05)
    if _is_ride(norm_sport):
        return max(1200.0, distance_m * 0.06)
    return max(500.0, distance_m * 0.07)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None
