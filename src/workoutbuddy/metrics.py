from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# User-defined heart-rate zones. Edit here if thresholds change.
HR_ZONE_BOUNDS = [
    ("Z1", None, 134.0),
    ("Z2", 134.0, 146.0),
    ("Z3", 146.0, 156.0),
    ("Z4", 156.0, 167.0),
    ("Z5", 167.0, None),
]

RUN_TYPES = {"run", "running", "trailrun", "trail_run", "virtualrun", "virtual_run"}
RIDE_TYPES = {"ride", "cycling", "bike", "biking", "virtualride", "virtual_ride", "gravelride", "mountainbikeride", "ebikeride"}


@dataclass
class ActivityMetrics:
    activity_id: str
    sport_type: str
    name: str
    start_date_local: str
    distance_m: Optional[float]
    moving_time_s: Optional[float]
    elapsed_time_s: Optional[float]
    elevation_gain_m: Optional[float]
    elevation_loss_m: Optional[float]
    avg_speed_mps: Optional[float]
    max_speed_mps: Optional[float]
    avg_pace_min_km: Optional[float]
    avg_hr: Optional[float]
    max_hr: Optional[float]
    avg_cadence: Optional[float]
    max_cadence: Optional[float]
    run_step_frequency_spm: Optional[float]
    max_step_frequency_spm: Optional[float]
    estimated_total_steps: Optional[float]
    avg_vertical_speed_m_per_h: Optional[float]
    avg_power: Optional[float]
    max_power: Optional[float]
    normalized_power: Optional[float]
    variability_index: Optional[float]
    avg_temp_c: Optional[float]
    calories: Optional[float]
    time_since_previous_h: Optional[float]
    moving_ratio: Optional[float]
    first_half_avg_hr: Optional[float]
    second_half_avg_hr: Optional[float]
    first_half_avg_speed_mps: Optional[float]
    second_half_avg_speed_mps: Optional[float]
    first_half_eff_speed_per_hr: Optional[float]
    second_half_eff_speed_per_hr: Optional[float]
    hr_efficiency_drift_pct: Optional[float]
    km_hr_efficiency_drift_pct: Optional[float]
    km_gap_hr_efficiency_drift_pct: Optional[float]
    avg_grade_pct: Optional[float]
    min_grade_pct: Optional[float]
    max_grade_pct: Optional[float]
    avg_gap_speed_mps: Optional[float]
    avg_gap_pace_min_km: Optional[float]
    first_half_avg_gap_speed_mps: Optional[float]
    second_half_avg_gap_speed_mps: Optional[float]
    first_half_eff_gap_speed_per_hr: Optional[float]
    second_half_eff_gap_speed_per_hr: Optional[float]
    gap_hr_efficiency_drift_pct: Optional[float]
    vo2_demand_est_ml_kg_min: Optional[float]
    best_1min_pace_min_km: Optional[float]
    best_5min_pace_min_km: Optional[float]
    best_10min_pace_min_km: Optional[float]
    best_20min_pace_min_km: Optional[float]
    best_1min_power: Optional[float]
    best_5min_power: Optional[float]
    best_20min_power: Optional[float]
    avg_impact_bw: Optional[float]
    max_impact_bw: Optional[float]
    impact_load_index: Optional[float]
    training_load_score: Optional[float]
    trimp_score: Optional[float]
    easy_zone_s: Optional[float]
    hard_zone_s: Optional[float]
    easy_zone_fraction: Optional[float]
    hard_zone_fraction: Optional[float]
    z1_s: Optional[float]
    z2_s: Optional[float]
    z3_s: Optional[float]
    z4_s: Optional[float]
    z5_s: Optional[float]
    data_point_count: Optional[int]
    gps_point_count: Optional[int]
    has_gps: Optional[int]
    has_altitude: Optional[int]
    has_hr: Optional[int]
    has_power: Optional[int]
    zones_json: str
    km_splits_json: str
    best_efforts_json: str
    flags_json: str

    def to_db_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        val = float(x)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (TypeError, ValueError):
        return None


def _norm_type(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").replace("-", "").replace("_", "").lower()


def _is_run(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {x.replace("_", "") for x in RUN_TYPES}


def _is_ride(sport_type: Any) -> bool:
    return _norm_type(sport_type) in {x.replace("_", "") for x in RIDE_TYPES}


def _stream_array(streams: Dict[str, Any], key: str) -> np.ndarray:
    obj = streams.get(key)
    if not isinstance(obj, dict) or "data" not in obj:
        return np.array([], dtype=float)
    try:
        return np.asarray(obj["data"], dtype=float)
    except (TypeError, ValueError):
        # Some arrays contain None or malformed elements; convert manually.
        out: List[float] = []
        for v in obj.get("data", []):
            fv = _safe_float(v)
            out.append(float("nan") if fv is None else fv)
        return np.asarray(out, dtype=float)


def _latlng_stream(streams: Dict[str, Any]) -> List[Tuple[float, float]]:
    obj = streams.get("latlng")
    if not isinstance(obj, dict) or "data" not in obj:
        return []
    out: List[Tuple[float, float]] = []
    for p in obj["data"]:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            try:
                out.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                pass
    return out


def _finite(a: np.ndarray) -> np.ndarray:
    if a.size == 0:
        return a
    return a[np.isfinite(a)]


def _nanmean(a: np.ndarray) -> Optional[float]:
    f = _finite(a)
    if f.size == 0:
        return None
    return float(np.nanmean(f))


def _nanmax(a: np.ndarray) -> Optional[float]:
    f = _finite(a)
    if f.size == 0:
        return None
    return float(np.nanmax(f))


def _nanmin(a: np.ndarray) -> Optional[float]:
    f = _finite(a)
    if f.size == 0:
        return None
    return float(np.nanmin(f))


def _mean_where(values: np.ndarray, mask: np.ndarray) -> Optional[float]:
    if values.size == 0 or mask.size == 0 or values.size != mask.size:
        return None
    return _nanmean(values[mask])


def _pace_min_km_from_speed(speed_mps: Optional[float]) -> Optional[float]:
    if speed_mps is None or speed_mps <= 0:
        return None
    return 1000.0 / speed_mps / 60.0


def _pace_min_km_from_distance_time(distance_m: Optional[float], moving_time_s: Optional[float]) -> Optional[float]:
    if not distance_m or distance_m <= 0 or not moving_time_s or moving_time_s <= 0:
        return None
    return moving_time_s / (distance_m / 1000.0) / 60.0


def _estimate_running_vo2(speed_mps: Optional[float], grade_fraction: Optional[float]) -> Optional[float]:
    if speed_mps is None or speed_mps <= 0:
        return None
    g = grade_fraction if grade_fraction is not None else 0.0
    g = max(min(g, 0.20), -0.20)
    speed_m_min = speed_mps * 60.0
    return float(0.2 * speed_m_min + 0.9 * speed_m_min * g + 3.5)


def _estimate_running_vo2_array(speed_mps: np.ndarray, grade_pct: np.ndarray) -> np.ndarray:
    n = min(speed_mps.size, grade_pct.size)
    if n == 0:
        return np.array([], dtype=float)
    speed = np.asarray(speed_mps[:n], dtype=float)
    grade = np.asarray(grade_pct[:n], dtype=float) / 100.0
    grade = np.clip(grade, -0.20, 0.20)
    vo2 = 0.2 * (speed * 60.0) + 0.9 * (speed * 60.0) * grade + 3.5
    vo2[~np.isfinite(vo2) | (speed <= 0)] = np.nan
    return vo2


def _gap_speed_from_vo2(vo2: Optional[float]) -> Optional[float]:
    if vo2 is None or vo2 <= 3.5:
        return None
    speed_m_min = (vo2 - 3.5) / 0.2
    return speed_m_min / 60.0


def _gap_speed_array_from_vo2(vo2: np.ndarray) -> np.ndarray:
    if vo2.size == 0:
        return np.array([], dtype=float)
    out = (vo2 - 3.5) / 0.2 / 60.0
    out[~np.isfinite(out) | (out <= 0)] = np.nan
    return out


def _zone_name(hr_value: float) -> Optional[str]:
    if not math.isfinite(hr_value):
        return None
    for name, lower, upper in HR_ZONE_BOUNDS:
        lower_ok = True if lower is None else hr_value >= lower
        upper_ok = True if upper is None else hr_value < upper
        if lower_ok and upper_ok:
            return name
    return None


def _zone_thresholds_json() -> List[Dict[str, Any]]:
    return [
        {"zone": name, "lower_bpm_inclusive": lower, "upper_bpm_exclusive": upper}
        for name, lower, upper in HR_ZONE_BOUNDS
    ]


def _sample_durations(t: np.ndarray, n: int) -> np.ndarray:
    if t.size == n and n > 1:
        tt = np.asarray(t, dtype=float)
        diffs = np.diff(tt)
        finite = diffs[np.isfinite(diffs) & (diffs > 0) & (diffs < 60)]
        fallback = float(np.nanmedian(finite)) if finite.size else 1.0
        durations = np.diff(tt, append=tt[-1] + fallback)
        durations = np.where(np.isfinite(durations) & (durations > 0) & (durations < 300), durations, fallback)
        return durations
    return np.ones(n, dtype=float)


def _infer_hr_zones(t: np.ndarray, hr: np.ndarray) -> Dict[str, Any]:
    zone_seconds = {name: 0.0 for name, _, _ in HR_ZONE_BOUNDS}
    zone_samples = {name: 0 for name, _, _ in HR_ZONE_BOUNDS}

    if hr.size == 0:
        return {"source": "local_hr_stream", "thresholds_bpm": _zone_thresholds_json(), "available": False, "zones": []}

    hr = np.asarray(hr, dtype=float)
    n = hr.size
    durations = _sample_durations(t, n)
    for h, dt in zip(hr, durations):
        if not math.isfinite(float(h)):
            continue
        z = _zone_name(float(h))
        if z:
            zone_seconds[z] += float(dt)
            zone_samples[z] += 1

    total_seconds = sum(zone_seconds.values())
    total_samples = sum(zone_samples.values())
    zones = []
    for name, lower, upper in HR_ZONE_BOUNDS:
        seconds = zone_seconds[name] if total_seconds > 0 else None
        samples = zone_samples[name]
        percent = None
        if total_seconds > 0 and seconds is not None:
            percent = 100.0 * seconds / total_seconds
        elif total_samples > 0:
            percent = 100.0 * samples / total_samples
        zones.append({"zone": name, "lower_bpm_inclusive": lower, "upper_bpm_exclusive": upper, "seconds": seconds, "samples": samples, "percent": percent})

    return {"source": "local_hr_stream", "thresholds_bpm": _zone_thresholds_json(), "available": bool(total_seconds > 0 or total_samples > 0), "total_seconds": total_seconds if total_seconds > 0 else None, "total_samples": total_samples, "zones": zones}


def _zone_seconds_tuple(zone_info: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    by_zone = {z.get("zone"): z for z in zone_info.get("zones", []) if isinstance(z, dict)}
    out: List[Optional[float]] = []
    for name in ["Z1", "Z2", "Z3", "Z4", "Z5"]:
        seconds = by_zone.get(name, {}).get("seconds")
        out.append(_safe_float(seconds))
    return tuple(out)  # type: ignore[return-value]


def _training_load_from_zones(z1_s: Optional[float], z2_s: Optional[float], z3_s: Optional[float], z4_s: Optional[float], z5_s: Optional[float], moving_time_s: Optional[float]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    z = [z1_s, z2_s, z3_s, z4_s, z5_s]
    if any(v is not None and v > 0 for v in z):
        z1, z2, z3, z4, z5 = [(float(v) if v is not None else 0.0) for v in z]
        easy = z1 + z2
        hard = z4 + z5
        total = z1 + z2 + z3 + z4 + z5
        load = (z1 / 60.0) * 1.0 + (z2 / 60.0) * 2.0 + (z3 / 60.0) * 3.0 + (z4 / 60.0) * 4.5 + (z5 / 60.0) * 6.0
        # TRIMP-like score: similar but slightly steeper penalty for Z4/Z5.
        trimp = (z1 / 60.0) * 0.8 + (z2 / 60.0) * 1.5 + (z3 / 60.0) * 2.7 + (z4 / 60.0) * 5.0 + (z5 / 60.0) * 7.0
        easy_fraction = easy / total if total > 0 else None
        hard_fraction = hard / total if total > 0 else None
        return float(load), float(trimp), float(easy), float(hard), easy_fraction, hard_fraction
    if moving_time_s is not None and moving_time_s > 0:
        load = float((moving_time_s / 60.0) * 1.8)
        return load, load, None, None, None, None
    return None, None, None, None, None, None


def _elevation_gain_loss(altitude: np.ndarray) -> Tuple[Optional[float], Optional[float]]:
    f = np.asarray(altitude, dtype=float)
    f = f[np.isfinite(f)]
    if f.size < 2:
        return None, None
    diff = np.diff(f)
    gain = float(np.sum(diff[diff > 0]))
    loss = float(-np.sum(diff[diff < 0]))
    return gain, loss


def _normalise_run_cadence_spm(cadence: np.ndarray) -> np.ndarray:
    if cadence.size == 0:
        return cadence
    c = np.asarray(cadence, dtype=float).copy()
    med = _nanmean(c)
    if med is not None and 50 <= med <= 120:
        # Some sources store single-foot cadence; convert to total steps/min.
        c *= 2.0
    return c


def _estimate_cadence_from_speed(speed: np.ndarray) -> np.ndarray:
    if speed.size == 0:
        return np.array([], dtype=float)
    s = np.asarray(speed, dtype=float)
    spm = 150.0 + (s - 2.2) * 18.0
    spm = np.clip(spm, 130.0, 190.0)
    spm[~np.isfinite(s) | (s <= 0)] = np.nan
    return spm


def _impact_bw_index(speed: np.ndarray, cadence_spm: np.ndarray, grade_pct: np.ndarray) -> np.ndarray:
    n = min(speed.size, cadence_spm.size if cadence_spm.size else speed.size, grade_pct.size if grade_pct.size else speed.size)
    if n == 0:
        return np.array([], dtype=float)
    s = np.asarray(speed[:n], dtype=float)
    c = np.asarray(cadence_spm[:n] if cadence_spm.size else _estimate_cadence_from_speed(s), dtype=float)
    if grade_pct.size:
        g = np.asarray(grade_pct[:n], dtype=float)
    else:
        g = np.zeros(n, dtype=float)
    downhill = np.where(g < 0, np.abs(g), 0.0)
    uphill = np.where(g > 0, g, 0.0)
    # Heuristic, not measured ground-reaction force. Unit is estimated multiples of body weight.
    impact = 1.55 + 0.20 * s + 0.006 * (c - 160.0) + 0.035 * downhill - 0.012 * uphill
    impact[~np.isfinite(impact) | (s <= 0)] = np.nan
    return np.clip(impact, 1.1, 4.5)


def _normalised_power(power: np.ndarray, t: np.ndarray) -> Optional[float]:
    if power.size == 0:
        return None
    p = np.asarray(power, dtype=float)
    if _finite(p).size < 10:
        return _nanmean(p)
    # Approximate 30 s rolling average from sample count, using median dt.
    if t.size == p.size and t.size > 2:
        dt = np.diff(t)
        dt = dt[np.isfinite(dt) & (dt > 0) & (dt < 60)]
        med_dt = float(np.nanmedian(dt)) if dt.size else 1.0
    else:
        med_dt = 1.0
    win = max(1, int(round(30.0 / max(med_dt, 0.2))))
    valid = np.where(np.isfinite(p), p, np.nan)
    if valid.size < win:
        return _nanmean(valid)
    kernel = np.ones(win, dtype=float)
    sums = np.convolve(np.nan_to_num(valid, nan=0.0), kernel, mode="valid")
    counts = np.convolve(np.isfinite(valid).astype(float), kernel, mode="valid")
    roll = sums / np.maximum(counts, 1.0)
    roll[counts < max(1, win * 0.5)] = np.nan
    f = roll[np.isfinite(roll) & (roll > 0)]
    if f.size == 0:
        return None
    return float(np.mean(f ** 4.0) ** 0.25)


def _best_mean_value(t: np.ndarray, values: np.ndarray, window_s: float) -> Optional[float]:
    n = min(t.size, values.size)
    if n < 2:
        return None
    tt = np.asarray(t[:n], dtype=float)
    vv = np.asarray(values[:n], dtype=float)
    mask = np.isfinite(tt) & np.isfinite(vv) & (vv > 0)
    if mask.sum() < 2:
        return None
    tt = tt[mask]
    vv = vv[mask]
    order = np.argsort(tt)
    tt = tt[order]
    vv = vv[order]
    best = None
    j = 0
    for i in range(tt.size):
        while j < tt.size and tt[j] - tt[i] < window_s:
            j += 1
        if j - i >= 2:
            m = float(np.mean(vv[i:j]))
            if best is None or m > best:
                best = m
    return best


def _build_best_efforts(t: np.ndarray, speed: np.ndarray, power: np.ndarray) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for sec, key in [(60, "1min"), (300, "5min"), (600, "10min"), (1200, "20min")]:
        best_speed = _best_mean_value(t, speed, sec)
        out[f"best_{key}_speed_mps"] = best_speed
        out[f"best_{key}_pace_min_km"] = _pace_min_km_from_speed(best_speed)
    for sec, key in [(60, "1min"), (300, "5min"), (1200, "20min")]:
        out[f"best_{key}_power_w"] = _best_mean_value(t, power, sec)
    return out


def _km_splits(t: np.ndarray, distance: np.ndarray, hr: np.ndarray, speed: np.ndarray, gap_speed: np.ndarray, grade: np.ndarray) -> Tuple[List[Dict[str, Any]], Optional[float], Optional[float]]:
    n = min(distance.size, t.size if t.size else distance.size)
    if n < 10:
        return [], None, None
    d = np.asarray(distance[:n], dtype=float)
    if not np.isfinite(d).any() or np.nanmax(d) < 1000:
        return [], None, None
    max_km = int(math.floor(float(np.nanmax(d)) / 1000.0))
    if max_km <= 0:
        return [], None, None
    splits: List[Dict[str, Any]] = []
    effs: List[float] = []
    gap_effs: List[float] = []
    kms: List[float] = []
    for km in range(1, max_km + 1):
        lo = (km - 1) * 1000.0
        hi = km * 1000.0
        mask = (d >= lo) & (d < hi) & np.isfinite(d)
        if mask.sum() < 5:
            continue
        h = _mean_where(hr[:n], mask) if hr.size >= n else None
        s = _mean_where(speed[:n], mask) if speed.size >= n else None
        gs = _mean_where(gap_speed[:n], mask) if gap_speed.size >= n else None
        gp = _mean_where(grade[:n], mask) if grade.size >= n else None
        eff = s / h if s is not None and h and h > 0 else None
        geff = gs / h if gs is not None and h and h > 0 else None
        splits.append({
            "km": km,
            "avg_hr": h,
            "avg_speed_mps": s,
            "avg_pace_min_km": _pace_min_km_from_speed(s),
            "avg_gap_speed_mps": gs,
            "avg_gap_pace_min_km": _pace_min_km_from_speed(gs),
            "avg_grade_pct": gp,
            "eff_speed_per_hr": eff,
            "eff_gap_speed_per_hr": geff,
        })
        if eff is not None and math.isfinite(eff):
            effs.append(float(eff)); kms.append(float(km))
        if geff is not None and math.isfinite(geff):
            gap_effs.append(float(geff))
    raw_drift = _first_last_drift(effs)
    gap_drift = _first_last_drift(gap_effs)
    return splits, raw_drift, gap_drift


def _first_last_drift(values: List[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    first_n = max(1, len(values) // 3)
    last_n = max(1, len(values) // 3)
    first = float(np.mean(values[:first_n]))
    last = float(np.mean(values[-last_n:]))
    if first <= 0:
        return None
    return (last / first - 1.0) * 100.0


class MetricCalculator:
    def calculate(
        self,
        summary: Dict[str, Any],
        detail: Dict[str, Any],
        streams: Dict[str, Any],
        zones: Optional[List[Dict[str, Any]]] = None,
        previous_start: Optional[datetime] = None,
    ) -> ActivityMetrics:
        src = {**summary, **detail}
        activity_id = str(src.get("id"))
        sport_type = src.get("sport_type") or src.get("type") or "Unknown"
        name = src.get("name") or "Unnamed activity"
        start_date_local = src.get("start_date_local") or src.get("start_date") or ""

        distance_m = _safe_float(src.get("distance"))
        moving_time_s = _safe_float(src.get("moving_time"))
        elapsed_time_s = _safe_float(src.get("elapsed_time"))
        elevation_gain_m = _safe_float(src.get("total_elevation_gain"))
        avg_speed_mps = _safe_float(src.get("average_speed"))
        avg_pace_min_km = _pace_min_km_from_distance_time(distance_m, moving_time_s)
        avg_hr = _safe_float(src.get("average_heartrate"))
        max_hr = _safe_float(src.get("max_heartrate"))
        avg_cadence = _safe_float(src.get("average_cadence"))
        avg_power = _safe_float(src.get("average_watts"))
        max_power = _safe_float(src.get("max_watts"))
        calories = _safe_float(src.get("calories"))

        time_since_previous_h = None
        try:
            current_start = datetime.fromisoformat(start_date_local.replace("Z", "+00:00"))
            if previous_start is not None:
                prev = previous_start
                if getattr(prev, "tzinfo", None) is not None and getattr(current_start, "tzinfo", None) is None:
                    prev = prev.replace(tzinfo=None)
                if getattr(prev, "tzinfo", None) is None and getattr(current_start, "tzinfo", None) is not None:
                    current_start = current_start.replace(tzinfo=None)
                time_since_previous_h = abs((current_start - prev).total_seconds()) / 3600.0
        except Exception:
            pass

        t = _stream_array(streams, "time")
        distance = _stream_array(streams, "distance")
        hr = _stream_array(streams, "heartrate")
        speed = _stream_array(streams, "velocity_smooth")
        altitude = _stream_array(streams, "altitude")
        grade = _stream_array(streams, "grade_smooth")
        cadence_stream = _stream_array(streams, "cadence")
        power = _stream_array(streams, "watts")
        temp = _stream_array(streams, "temp")
        moving = _stream_array(streams, "moving")

        if avg_hr is None:
            avg_hr = _nanmean(hr)
        if max_hr is None:
            max_hr = _nanmax(hr)
        if avg_speed_mps is None:
            avg_speed_mps = _nanmean(speed)
        max_speed_mps = _nanmax(speed)
        if avg_power is None:
            avg_power = _nanmean(power)
        if max_power is None:
            max_power = _nanmax(power)
        if avg_cadence is None:
            avg_cadence = _nanmean(cadence_stream)
        max_cadence = _nanmax(cadence_stream)
        avg_temp_c = _nanmean(temp)

        if elevation_gain_m is None or elevation_gain_m == 0:
            gain, loss = _elevation_gain_loss(altitude)
            elevation_gain_m = gain if gain is not None else elevation_gain_m
            elevation_loss_m = loss
        else:
            _, elevation_loss_m = _elevation_gain_loss(altitude)

        moving_ratio = None
        if moving.size:
            f = moving[np.isfinite(moving)]
            if f.size:
                moving_ratio = float(np.mean(f > 0))
        elif moving_time_s is not None and elapsed_time_s and elapsed_time_s > 0:
            moving_ratio = moving_time_s / elapsed_time_s

        avg_vertical_speed_m_per_h = None
        if elevation_gain_m is not None and moving_time_s and moving_time_s > 0:
            avg_vertical_speed_m_per_h = elevation_gain_m / (moving_time_s / 3600.0)

        first_half_avg_hr = second_half_avg_hr = None
        first_half_avg_speed = second_half_avg_speed = None
        first_eff = second_eff = drift_pct = None
        first_half_avg_gap_speed = second_half_avg_gap_speed = None
        first_gap_eff = second_gap_eff = gap_drift_pct = None

        avg_grade_pct = _nanmean(grade) if grade.size else None
        min_grade_pct = _nanmin(grade) if grade.size else None
        max_grade_pct = _nanmax(grade) if grade.size else None
        avg_gap_speed_mps = None
        avg_gap_pace_min_km = None
        vo2_demand = None
        gap_speed_stream = np.array([], dtype=float)

        if _is_run(sport_type):
            if speed.size and grade.size:
                n = min(speed.size, grade.size)
                vo2_stream = _estimate_running_vo2_array(speed[:n], grade[:n])
                gap_speed_stream = _gap_speed_array_from_vo2(vo2_stream)
                avg_gap_speed_mps = _nanmean(gap_speed_stream)
                avg_gap_pace_min_km = _pace_min_km_from_speed(avg_gap_speed_mps)
                vo2_demand = _nanmean(vo2_stream)
            else:
                avg_grade_fraction = (avg_grade_pct / 100.0) if avg_grade_pct is not None else None
                vo2_demand = _estimate_running_vo2(avg_speed_mps, avg_grade_fraction)
                avg_gap_speed_mps = _gap_speed_from_vo2(vo2_demand)
                avg_gap_pace_min_km = _pace_min_km_from_speed(avg_gap_speed_mps)

        if t.size > 2:
            split_t = float(np.nanmin(t) + (np.nanmax(t) - np.nanmin(t)) / 2.0)
            first_mask = t <= split_t
            second_mask = t > split_t
            if hr.size == t.size:
                first_half_avg_hr = _mean_where(hr, first_mask)
                second_half_avg_hr = _mean_where(hr, second_mask)
            if speed.size == t.size:
                first_half_avg_speed = _mean_where(speed, first_mask)
                second_half_avg_speed = _mean_where(speed, second_mask)
            if gap_speed_stream.size == t.size:
                first_half_avg_gap_speed = _mean_where(gap_speed_stream, first_mask)
                second_half_avg_gap_speed = _mean_where(gap_speed_stream, second_mask)

            if first_half_avg_hr and first_half_avg_hr > 0 and first_half_avg_speed is not None:
                first_eff = first_half_avg_speed / first_half_avg_hr
            if second_half_avg_hr and second_half_avg_hr > 0 and second_half_avg_speed is not None:
                second_eff = second_half_avg_speed / second_half_avg_hr
            if first_eff and first_eff > 0 and second_eff is not None:
                drift_pct = (second_eff / first_eff - 1.0) * 100.0

            if first_half_avg_hr and first_half_avg_hr > 0 and first_half_avg_gap_speed is not None:
                first_gap_eff = first_half_avg_gap_speed / first_half_avg_hr
            if second_half_avg_hr and second_half_avg_hr > 0 and second_half_avg_gap_speed is not None:
                second_gap_eff = second_half_avg_gap_speed / second_half_avg_hr
            if first_gap_eff and first_gap_eff > 0 and second_gap_eff is not None:
                gap_drift_pct = (second_gap_eff / first_gap_eff - 1.0) * 100.0

        km_splits, km_raw_drift, km_gap_drift = _km_splits(t, distance, hr, speed, gap_speed_stream, grade)
        best_efforts = _build_best_efforts(t, speed, power)

        # Run step metrics and heuristic impact estimate.
        run_step_frequency_spm = max_step_frequency_spm = estimated_total_steps = None
        avg_impact_bw = max_impact_bw = impact_load_index = None
        if _is_run(sport_type):
            step_stream = _normalise_run_cadence_spm(cadence_stream)
            if step_stream.size == 0 and speed.size:
                step_stream = _estimate_cadence_from_speed(speed)
            run_step_frequency_spm = _nanmean(step_stream)
            max_step_frequency_spm = _nanmax(step_stream)
            if run_step_frequency_spm is not None and moving_time_s is not None:
                estimated_total_steps = run_step_frequency_spm * (moving_time_s / 60.0)
            impact = _impact_bw_index(speed, step_stream, grade)
            avg_impact_bw = _nanmean(impact)
            max_impact_bw = _nanmax(impact)
            if avg_impact_bw is not None and estimated_total_steps is not None:
                impact_load_index = avg_impact_bw * estimated_total_steps / 1000.0

        normalized_power = _normalised_power(power, t)
        variability_index = None
        if normalized_power is not None and avg_power and avg_power > 0:
            variability_index = normalized_power / avg_power

        local_zone_info = _infer_hr_zones(t, hr)
        z1_s, z2_s, z3_s, z4_s, z5_s = _zone_seconds_tuple(local_zone_info)
        training_load, trimp_score, easy_s, hard_s, easy_fraction, hard_fraction = _training_load_from_zones(z1_s, z2_s, z3_s, z4_s, z5_s, moving_time_s)

        point_count = int(t.size or max([arr.size for arr in [distance, hr, speed, altitude, cadence_stream, power] if arr.size] or [0]))
        gps_count = len(_latlng_stream(streams))
        flags = self._make_flags(sport_type, distance_m, moving_time_s, avg_hr, max_hr, drift_pct, streams)
        flags.update({
            "hr_zones_source": "local_hr_stream_thresholds",
            "has_gap_efficiency": gap_drift_pct is not None,
            "has_apple_vo2max_manual": False,
            "impact_force_note": "avg_impact_bw/max_impact_bw are heuristic bodyweight-multiple estimates, not measured ground-reaction force.",
            "metrics_version": "v5_extended",
        })

        return ActivityMetrics(
            activity_id=activity_id,
            sport_type=str(sport_type),
            name=str(name),
            start_date_local=str(start_date_local),
            distance_m=distance_m,
            moving_time_s=moving_time_s,
            elapsed_time_s=elapsed_time_s,
            elevation_gain_m=elevation_gain_m,
            elevation_loss_m=elevation_loss_m,
            avg_speed_mps=avg_speed_mps,
            max_speed_mps=max_speed_mps,
            avg_pace_min_km=avg_pace_min_km,
            avg_hr=avg_hr,
            max_hr=max_hr,
            avg_cadence=avg_cadence,
            max_cadence=max_cadence,
            run_step_frequency_spm=run_step_frequency_spm,
            max_step_frequency_spm=max_step_frequency_spm,
            estimated_total_steps=estimated_total_steps,
            avg_vertical_speed_m_per_h=avg_vertical_speed_m_per_h,
            avg_power=avg_power,
            max_power=max_power,
            normalized_power=normalized_power,
            variability_index=variability_index,
            avg_temp_c=avg_temp_c,
            calories=calories,
            time_since_previous_h=time_since_previous_h,
            moving_ratio=moving_ratio,
            first_half_avg_hr=first_half_avg_hr,
            second_half_avg_hr=second_half_avg_hr,
            first_half_avg_speed_mps=first_half_avg_speed,
            second_half_avg_speed_mps=second_half_avg_speed,
            first_half_eff_speed_per_hr=first_eff,
            second_half_eff_speed_per_hr=second_eff,
            hr_efficiency_drift_pct=drift_pct,
            km_hr_efficiency_drift_pct=km_raw_drift,
            km_gap_hr_efficiency_drift_pct=km_gap_drift,
            avg_grade_pct=avg_grade_pct,
            min_grade_pct=min_grade_pct,
            max_grade_pct=max_grade_pct,
            avg_gap_speed_mps=avg_gap_speed_mps,
            avg_gap_pace_min_km=avg_gap_pace_min_km,
            first_half_avg_gap_speed_mps=first_half_avg_gap_speed,
            second_half_avg_gap_speed_mps=second_half_avg_gap_speed,
            first_half_eff_gap_speed_per_hr=first_gap_eff,
            second_half_eff_gap_speed_per_hr=second_gap_eff,
            gap_hr_efficiency_drift_pct=gap_drift_pct,
            vo2_demand_est_ml_kg_min=vo2_demand,
            best_1min_pace_min_km=best_efforts.get("best_1min_pace_min_km"),
            best_5min_pace_min_km=best_efforts.get("best_5min_pace_min_km"),
            best_10min_pace_min_km=best_efforts.get("best_10min_pace_min_km"),
            best_20min_pace_min_km=best_efforts.get("best_20min_pace_min_km"),
            best_1min_power=best_efforts.get("best_1min_power_w"),
            best_5min_power=best_efforts.get("best_5min_power_w"),
            best_20min_power=best_efforts.get("best_20min_power_w"),
            avg_impact_bw=avg_impact_bw,
            max_impact_bw=max_impact_bw,
            impact_load_index=impact_load_index,
            training_load_score=training_load,
            trimp_score=trimp_score,
            easy_zone_s=easy_s,
            hard_zone_s=hard_s,
            easy_zone_fraction=easy_fraction,
            hard_zone_fraction=hard_fraction,
            z1_s=z1_s,
            z2_s=z2_s,
            z3_s=z3_s,
            z4_s=z4_s,
            z5_s=z5_s,
            data_point_count=point_count,
            gps_point_count=gps_count,
            has_gps=1 if gps_count > 0 else 0,
            has_altitude=1 if altitude.size > 0 else 0,
            has_hr=1 if hr.size > 0 else 0,
            has_power=1 if power.size > 0 else 0,
            zones_json=json.dumps(local_zone_info, indent=2),
            km_splits_json=json.dumps(km_splits, indent=2),
            best_efforts_json=json.dumps(best_efforts, indent=2),
            flags_json=json.dumps(flags, indent=2),
        )

    @staticmethod
    def _make_flags(sport_type: str, distance_m: Optional[float], moving_time_s: Optional[float], avg_hr: Optional[float], max_hr: Optional[float], drift_pct: Optional[float], streams: Dict[str, Any]) -> Dict[str, Any]:
        flags: Dict[str, Any] = {}
        flags["has_hr_stream"] = bool(_stream_array(streams, "heartrate").size)
        flags["has_gps_stream"] = bool(_latlng_stream(streams))
        flags["has_altitude_stream"] = bool(_stream_array(streams, "altitude").size)
        flags["has_power_stream"] = bool(_stream_array(streams, "watts").size)
        flags["has_grade_stream"] = bool(_stream_array(streams, "grade_smooth").size)
        flags["sport_type"] = sport_type
        if drift_pct is not None:
            flags["high_negative_hr_efficiency_drift"] = drift_pct < -5.0
        if distance_m is not None and moving_time_s is not None and moving_time_s > 0:
            flags["distance_km"] = distance_m / 1000.0
        if avg_hr is not None and max_hr is not None:
            flags["avg_to_max_hr_ratio"] = avg_hr / max_hr if max_hr > 0 else None
        return flags


class StreamUtils:
    @staticmethod
    def latlng_points(streams: Dict[str, Any]) -> List[Tuple[float, float]]:
        return _latlng_stream(streams)
