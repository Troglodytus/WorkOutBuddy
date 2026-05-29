from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .config import AppConfig
from .database import WorkoutDatabase
from .metrics import MetricCalculator
from .weather import update_activity_weather_from_archive

ProgressCallback = Callable[[str], None]


def _text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value if value else None


def _direct_text(parent: ET.Element, local_name: str) -> Optional[str]:
    return _text(parent.find(f"{{*}}{local_name}"))


def _desc_text(parent: ET.Element, local_name: str) -> Optional[str]:
    return _text(parent.find(f".//{{*}}{local_name}"))


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        # TCX usually stores UTC timestamps like 2026-05-19T18:30:01Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_mean(values: List[Optional[float]]) -> Optional[float]:
    arr = np.array([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if arr.size == 0:
        return None
    return float(np.mean(arr))


def _safe_max(values: List[Optional[float]]) -> Optional[float]:
    arr = np.array([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if arr.size == 0:
        return None
    return float(np.max(arr))


def _sum_positive_gain(values: List[Optional[float]]) -> Optional[float]:
    arr = np.array([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if arr.size < 2:
        return None
    diff = np.diff(arr)
    gain = diff[diff > 0].sum()
    return float(gain)


def _stream(data: List[Any], original_size: Optional[int] = None, resolution: str = "high") -> Dict[str, Any]:
    return {"data": data, "series_type": "time", "original_size": original_size or len(data), "resolution": resolution}


def _tcx_sport_to_strava_like(sport: Optional[str]) -> str:
    if not sport:
        return "Unknown"
    s = sport.lower()
    if s in {"running", "run"}:
        return "Run"
    if s in {"biking", "cycling", "bike", "ride"}:
        return "Ride"
    if "walk" in s:
        return "Walk"
    if "hike" in s:
        return "Hike"
    return sport


class TCXActivityParser:
    def parse_file(self, path: Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        """
        Parse one TCX file and return (summary, detail, streams).
        The summary/detail keys intentionally mimic Strava's activity JSON so the same metric
        code can be used for Strava streams and local TCX imports.
        """
        content = path.read_bytes()
        activity_id = "tcx_" + hashlib.sha1(content).hexdigest()[:16]
        root = ET.fromstring(content)

        activity = root.find(".//{*}Activity")
        if activity is None:
            raise ValueError(f"No <Activity> element found in {path}")

        sport = _tcx_sport_to_strava_like(activity.attrib.get("Sport"))
        tcx_id = _direct_text(activity, "Id")

        trackpoints = activity.findall(".//{*}Trackpoint")
        if not trackpoints:
            raise ValueError(f"No <Trackpoint> elements found in {path}")

        times: List[Optional[datetime]] = []
        elapsed_s: List[float] = []
        distances_m: List[Optional[float]] = []
        latlng: List[List[float]] = []
        altitudes_m: List[Optional[float]] = []
        hr_bpm: List[Optional[float]] = []
        cadence: List[Optional[float]] = []
        watts: List[Optional[float]] = []
        speeds_mps: List[Optional[float]] = []

        first_dt: Optional[datetime] = None
        for tp in trackpoints:
            t_txt = _direct_text(tp, "Time")
            dt = _parse_iso_datetime(t_txt) if t_txt else None
            if first_dt is None and dt is not None:
                first_dt = dt
            times.append(dt)
            if first_dt is not None and dt is not None:
                elapsed_s.append(max(0.0, (dt - first_dt).total_seconds()))
            else:
                elapsed_s.append(float(len(elapsed_s)))

            distances_m.append(_float_or_none(_direct_text(tp, "DistanceMeters")))
            altitudes_m.append(_float_or_none(_direct_text(tp, "AltitudeMeters")))
            hr_bpm.append(_float_or_none(_desc_text(tp, "Value") if tp.find("{*}HeartRateBpm") is not None else None))

            # TCX can store cadence directly or under extensions. Prefer the direct Trackpoint cadence.
            cadence_val = _float_or_none(_direct_text(tp, "Cadence"))
            if cadence_val is None:
                cadence_val = _float_or_none(_desc_text(tp, "RunCadence"))
            cadence.append(cadence_val)

            watts.append(_float_or_none(_desc_text(tp, "Watts")))
            speeds_mps.append(_float_or_none(_desc_text(tp, "Speed")))

            pos = tp.find("{*}Position")
            if pos is not None:
                lat = _float_or_none(_direct_text(pos, "LatitudeDegrees"))
                lon = _float_or_none(_direct_text(pos, "LongitudeDegrees"))
                if lat is not None and lon is not None:
                    latlng.append([lat, lon])

        distance_m = self._infer_distance(activity, distances_m)
        moving_time_s = self._infer_moving_time(activity, elapsed_s)
        elapsed_time_s = max(elapsed_s) if elapsed_s else None
        avg_speed_mps = distance_m / moving_time_s if distance_m and moving_time_s and moving_time_s > 0 else _safe_mean(speeds_mps)
        if not any(v is not None for v in speeds_mps):
            speeds_mps = self._derive_speed_from_distance_time(distances_m, elapsed_s)

        avg_hr = _safe_mean(hr_bpm)
        max_hr = _safe_max(hr_bpm)
        avg_cadence = _safe_mean(cadence)
        avg_watts = _safe_mean(watts)
        max_watts = _safe_max(watts)
        elevation_gain_m = self._infer_elevation_gain(activity, altitudes_m)
        calories = self._infer_calories(activity)
        start_date = (first_dt or _parse_iso_datetime(tcx_id or "") or datetime.now(timezone.utc)).isoformat()

        valid_count = len(elapsed_s)
        streams: Dict[str, Any] = {
            "time": _stream(elapsed_s, valid_count),
        }
        if any(v is not None for v in distances_m):
            streams["distance"] = _stream([float(v) if v is not None else None for v in distances_m], valid_count)
        if latlng:
            streams["latlng"] = _stream(latlng, len(latlng))
        if any(v is not None for v in altitudes_m):
            streams["altitude"] = _stream([float(v) if v is not None else None for v in altitudes_m], valid_count)
        if any(v is not None for v in speeds_mps):
            streams["velocity_smooth"] = _stream([float(v) if v is not None else None for v in speeds_mps], valid_count)
        if any(v is not None for v in hr_bpm):
            streams["heartrate"] = _stream([float(v) if v is not None else None for v in hr_bpm], valid_count)
        if any(v is not None for v in cadence):
            streams["cadence"] = _stream([float(v) if v is not None else None for v in cadence], valid_count)
        if any(v is not None for v in watts):
            streams["watts"] = _stream([float(v) if v is not None else None for v in watts], valid_count)
        grade = self._derive_grade(distances_m, altitudes_m)
        if any(v is not None for v in grade):
            streams["grade_smooth"] = _stream([float(v) if v is not None else None for v in grade], valid_count)

        summary = {
            "id": activity_id,
            "name": path.stem,
            "sport_type": sport,
            "type": sport,
            "start_date": start_date,
            "start_date_local": start_date,
            "distance": distance_m,
            "moving_time": moving_time_s,
            "elapsed_time": elapsed_time_s,
            "total_elevation_gain": elevation_gain_m,
            "average_speed": avg_speed_mps,
            "average_heartrate": avg_hr,
            "max_heartrate": max_hr,
            "average_cadence": avg_cadence,
            "average_watts": avg_watts,
            "max_watts": max_watts,
            "calories": calories,
            "source_file": str(path),
            "source_format": "tcx",
        }
        detail = dict(summary)
        detail["tcx_activity_id"] = tcx_id
        return summary, detail, streams

    @staticmethod
    def _infer_distance(activity: ET.Element, distances_m: List[Optional[float]]) -> Optional[float]:
        lap_vals = [_float_or_none(_direct_text(lap, "DistanceMeters")) for lap in activity.findall("{*}Lap")]
        lap_sum = sum(v for v in lap_vals if v is not None)
        if lap_sum > 0:
            return float(lap_sum)
        finite = [v for v in distances_m if v is not None and math.isfinite(v)]
        return float(max(finite)) if finite else None

    @staticmethod
    def _infer_moving_time(activity: ET.Element, elapsed_s: List[float]) -> Optional[float]:
        lap_vals = [_float_or_none(_direct_text(lap, "TotalTimeSeconds")) for lap in activity.findall("{*}Lap")]
        lap_sum = sum(v for v in lap_vals if v is not None)
        if lap_sum > 0:
            return float(lap_sum)
        return float(max(elapsed_s)) if elapsed_s else None

    @staticmethod
    def _infer_elevation_gain(activity: ET.Element, altitudes_m: List[Optional[float]]) -> Optional[float]:
        lap_vals = [_float_or_none(_direct_text(lap, "TotalElevationGain")) for lap in activity.findall("{*}Lap")]
        lap_sum = sum(v for v in lap_vals if v is not None)
        if lap_sum > 0:
            return float(lap_sum)
        return _sum_positive_gain(altitudes_m)

    @staticmethod
    def _infer_calories(activity: ET.Element) -> Optional[float]:
        lap_vals = [_float_or_none(_direct_text(lap, "Calories")) for lap in activity.findall("{*}Lap")]
        lap_sum = sum(v for v in lap_vals if v is not None)
        return float(lap_sum) if lap_sum > 0 else None

    @staticmethod
    def _derive_speed_from_distance_time(distances_m: List[Optional[float]], elapsed_s: List[float]) -> List[Optional[float]]:
        if len(distances_m) != len(elapsed_s) or len(distances_m) < 2:
            return [None for _ in distances_m]
        out: List[Optional[float]] = [None]
        for i in range(1, len(distances_m)):
            d0, d1 = distances_m[i - 1], distances_m[i]
            t0, t1 = elapsed_s[i - 1], elapsed_s[i]
            if d0 is None or d1 is None or t1 <= t0:
                out.append(None)
            else:
                v = (d1 - d0) / (t1 - t0)
                out.append(float(v) if math.isfinite(v) and v >= 0 else None)
        return out

    @staticmethod
    def _derive_grade(distances_m: List[Optional[float]], altitudes_m: List[Optional[float]]) -> List[Optional[float]]:
        if len(distances_m) != len(altitudes_m) or len(distances_m) < 2:
            return [None for _ in distances_m]
        out: List[Optional[float]] = [None]
        for i in range(1, len(distances_m)):
            d0, d1 = distances_m[i - 1], distances_m[i]
            a0, a1 = altitudes_m[i - 1], altitudes_m[i]
            if d0 is None or d1 is None or a0 is None or a1 is None:
                out.append(None)
                continue
            dd = d1 - d0
            da = a1 - a0
            if dd <= 1.0:
                out.append(None)
            else:
                grade_pct = 100.0 * da / dd
                # Clamp GPS spikes.
                out.append(float(max(min(grade_pct, 30.0), -30.0)))
        return out


class TCXImportService:
    def __init__(self, config: AppConfig, db: WorkoutDatabase):
        self.config = config
        self.db = db
        self.parser = TCXActivityParser()
        self.calculator = MetricCalculator()

    def import_folder(self, folder: Path, recursive: bool = True, progress: Optional[ProgressCallback] = None) -> int:
        pattern = "**/*.tcx" if recursive else "*.tcx"
        files = sorted(folder.glob(pattern))
        return self.import_files(files, progress=progress)

    def import_files(self, files: Iterable[Path], progress: Optional[ProgressCallback] = None) -> int:
        parsed: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Path]] = []
        for path in files:
            try:
                if progress:
                    progress(f"Parsing TCX: {path.name}")
                summary, detail, streams = self.parser.parse_file(path)
                parsed.append((summary, detail, streams, path))
            except Exception as e:
                if progress:
                    progress(f"Skipping {path.name}: {e}")

        parsed.sort(key=lambda item: item[1].get("start_date_local") or "")
        previous_start: Optional[datetime] = None
        count = 0
        skipped_duplicates = 0
        seen_signatures: List[Dict[str, Any]] = []

        for summary, detail, streams, path in parsed:
            start = detail.get("start_date_local") or summary.get("start_date_local")
            duration = _float_or_none(detail.get("moving_time") or summary.get("moving_time"))
            distance = _float_or_none(detail.get("distance") or summary.get("distance"))
            sport = detail.get("sport_type") or summary.get("sport_type")

            # First remove duplicates within the selected import batch itself. This is
            # intentionally NOT based on activity names/file names, because those differ
            # across Strava/Apple/Garmin exports.
            if self._matches_seen_duplicate(sport, start, duration, distance, seen_signatures):
                skipped_duplicates += 1
                if progress:
                    progress(f"Skipping duplicate TCX in import batch: {path.name}")
                continue
            seen_signatures.append({"sport": sport, "start": start, "duration": duration, "distance": distance})

            # Then check the persistent DB. If a Strava/API row or an older TCX row is
            # already present, update that canonical row instead of creating a second row.
            original_id = str(summary["id"])
            duplicate_id = self.db.find_similar_activity_id(
                sport_type=sport,
                start_date_local=start,
                moving_time_s=duration,
                distance_m=distance,
                exclude_activity_id=original_id,
            )
            activity_id = duplicate_id or original_id
            if duplicate_id and duplicate_id != original_id:
                if progress:
                    progress(f"Detected duplicate of existing activity {duplicate_id}; updating it from TCX: {path.name}")
                summary["id"] = activity_id
                detail["id"] = activity_id

            raw_path = self.config.raw_dir / f"{activity_id}.json"
            streams_path = self.config.streams_dir / f"{activity_id}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            streams_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({"summary": summary, "detail": detail, "source_file": str(path), "duplicate_of": duplicate_id}, indent=2), encoding="utf-8")
            streams_path.write_text(json.dumps(streams, indent=2), encoding="utf-8")

            metrics = self.calculator.calculate(summary, detail, streams, zones=None, previous_start=previous_start)
            self.db.upsert_activity(metrics, raw_path, streams_path, source="tcx_file")
            try:
                weather = update_activity_weather_from_archive(self.db, metrics.activity_id)
                if weather and progress:
                    progress(f"Weather archive {metrics.activity_id}: {weather.get('temp_c')} °C")
            except Exception as weather_error:
                if progress:
                    progress(f"Weather archive skipped for {metrics.activity_id}: {weather_error}")
            previous_start = self._parse_datetime(detail.get("start_date_local") or "") or previous_start
            count += 1

        if progress:
            progress(f"TCX import complete. Imported/updated {count} activities; skipped {skipped_duplicates} duplicate files from this batch.")
        return count

    def _matches_seen_duplicate(self, sport: Any, start: Any, duration: Optional[float], distance: Optional[float], seen: List[Dict[str, Any]]) -> bool:
        start_dt = self._parse_datetime(str(start or ""))
        if start_dt is None or duration is None or duration <= 0:
            return False
        sport_norm = self._norm_sport(sport)
        for s in seen:
            other_start = self._parse_datetime(str(s.get("start") or ""))
            other_duration = _float_or_none(s.get("duration"))
            if other_start is None or other_duration is None:
                continue
            if sport_norm and self._norm_sport(s.get("sport")) and sport_norm != self._norm_sport(s.get("sport")):
                continue
            if abs((start_dt.replace(tzinfo=None) - other_start.replace(tzinfo=None)).total_seconds()) > 300:
                continue
            duration_tol = max(180.0, 0.07 * max(float(duration), float(other_duration)))
            if abs(float(duration) - float(other_duration)) > duration_tol:
                continue
            other_distance = _float_or_none(s.get("distance"))
            if distance is not None and other_distance is not None and distance > 0 and other_distance > 0:
                dist_tol = self._distance_tolerance_m(sport_norm, max(float(distance), float(other_distance)))
                if abs(float(distance) - float(other_distance)) > dist_tol:
                    continue
            return True
        return False

    @staticmethod
    def _norm_sport(value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    @staticmethod
    def _distance_tolerance_m(norm_sport: str, distance_m: float) -> float:
        if norm_sport in {"run", "running", "trailrun", "virtualrun"}:
            return max(200.0, distance_m * 0.03)
        if norm_sport in {"ride", "cycling", "bike", "virtualride", "gravelride", "mountainbikeride", "ebikeride"}:
            return max(1000.0, distance_m * 0.04)
        return max(300.0, distance_m * 0.05)

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
