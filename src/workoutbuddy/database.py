from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

from .metrics import ActivityMetrics


TEXT_COLUMNS = {"activity_id", "sport_type", "name", "start_date_local", "zones_json", "km_splits_json", "best_efforts_json", "flags_json", "weather_source", "weather_fetched_at", "gender", "bmi_category"}
INTEGER_COLUMNS = {"data_point_count", "gps_point_count", "has_gps", "has_altitude", "has_hr", "has_power"}


def _metric_sql_type(name: str) -> str:
    if name in TEXT_COLUMNS:
        return "TEXT"
    if name in INTEGER_COLUMNS:
        return "INTEGER"
    return "REAL"


class WorkoutDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    activity_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'strava_api',
                    name TEXT,
                    sport_type TEXT,
                    start_date_local TEXT,
                    raw_json_path TEXT,
                    streams_json_path TEXT,
                    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS metrics (
                    activity_id TEXT PRIMARY KEY
                );


                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS workout_plan_overrides (
                    plan_date TEXT PRIMARY KEY,
                    override_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS weather_cache (
                    cache_key TEXT PRIMARY KEY,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    local_hour TEXT NOT NULL,
                    temp_c REAL,
                    apparent_temp_c REAL,
                    source TEXT,
                    response_json TEXT,
                    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._migrate_schema(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_start_date ON metrics(start_date_local)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_sport_type ON metrics(sport_type)")

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(metrics)").fetchall()}
        for f in fields(ActivityMetrics):
            if f.name not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {f.name} {_metric_sql_type(f.name)}")
        # Manual metric not part of ActivityMetrics because it must survive recalculation.
        if "apple_vo2max" not in existing:
            conn.execute("ALTER TABLE metrics ADD COLUMN apple_vo2max REAL")

        # Manual per-activity/profile fields. They are intentionally outside
        # ActivityMetrics because they must survive raw metric recalculation.
        manual_columns = {
            "body_weight_kg": "REAL",
            "bmi": "REAL",
            "bmi_category": "TEXT",
            "age_years_at_activity": "REAL",
            "gender": "TEXT",
            "bike_inferred_power_w": "REAL",
            "cycling_power_w": "REAL",
            "run_equivalent_power_w": "REAL",
            "bike_equivalent_power_w": "REAL",
            "power_hr_efficiency": "REAL",
            "power_hr_efficiency_drift_pct": "REAL",
            "effective_power_w": "REAL",
            "own_vo2max_estimate": "REAL",
            "estimated_vo2max": "REAL",
            "cardio_efficiency_index": "REAL",
            "temp_adjusted_efficiency": "REAL",
            "combined_fitness_score": "REAL",
            "fitness_score_ma7": "REAL",
            "fitness_score_ma14": "REAL",
            "fitness_score_ma35": "REAL",
            "vo2max_35d_trend": "REAL",
        }
        existing = {row[1] for row in conn.execute("PRAGMA table_info(metrics)").fetchall()}
        for col, typ in manual_columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")

        # Weather archive fields are intentionally outside ActivityMetrics so older
        # metric recalculation code cannot accidentally erase them unless a new
        # archive lookup succeeds.
        weather_columns = {
            "weather_temp_c": "REAL",
            "weather_apparent_temp_c": "REAL",
            "weather_temp_deviation_from_15_c": "REAL",
            "weather_lat": "REAL",
            "weather_lon": "REAL",
            "weather_source": "TEXT",
            "weather_fetched_at": "TEXT",
        }
        existing = {row[1] for row in conn.execute("PRAGMA table_info(metrics)").fetchall()}
        for col, typ in weather_columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ}")

    def get_known_activity_ids(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT activity_id FROM activities").fetchall()
        return {str(r["activity_id"]) for r in rows}

    def get_activity_source(self, activity_id: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT source FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
        return str(row["source"]) if row else None

    def get_previous_start_for_activity(self, start_date_local: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT start_date_local FROM metrics
                WHERE start_date_local < ?
                ORDER BY start_date_local DESC
                LIMIT 1
                """,
                (start_date_local,),
            ).fetchone()
        return row["start_date_local"] if row else None

    def upsert_activity(
        self,
        metrics: ActivityMetrics,
        raw_json_path: Path,
        streams_json_path: Path,
        source: str = "strava_api",
    ) -> None:
        m = metrics.to_db_dict()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT apple_vo2max, body_weight_kg, bmi, bmi_category,
                       age_years_at_activity, gender,
                       bike_inferred_power_w, cycling_power_w, run_equivalent_power_w,
                       bike_equivalent_power_w, power_hr_efficiency, power_hr_efficiency_drift_pct,
                       effective_power_w, own_vo2max_estimate, estimated_vo2max,
                       cardio_efficiency_index, temp_adjusted_efficiency, combined_fitness_score,
                       fitness_score_ma7, fitness_score_ma14, fitness_score_ma35, vo2max_35d_trend,
                       source
                FROM metrics LEFT JOIN activities USING(activity_id)
                WHERE metrics.activity_id = ?
                """,
                (metrics.activity_id,),
            ).fetchone()
            manual_preserve_cols = [
                "apple_vo2max", "body_weight_kg", "bmi", "bmi_category",
                "age_years_at_activity", "gender",
                "bike_inferred_power_w", "cycling_power_w", "run_equivalent_power_w",
                "bike_equivalent_power_w", "power_hr_efficiency", "power_hr_efficiency_drift_pct",
                "effective_power_w",
                "own_vo2max_estimate", "estimated_vo2max",
                "cardio_efficiency_index", "temp_adjusted_efficiency", "combined_fitness_score",
                "fitness_score_ma7", "fitness_score_ma14", "fitness_score_ma35", "vo2max_35d_trend",
            ]
            for col in manual_preserve_cols:
                if existing is not None and col in existing.keys() and existing[col] is not None:
                    m[col] = existing[col]
                else:
                    m[col] = None

            # Prefer custom/richer TCX over Strava API if the same id is somehow reused.
            if existing is not None and str(existing["source"] or "").startswith("tcx") and source == "strava_api":
                return

            conn.execute(
                """
                INSERT INTO activities (
                    activity_id, source, name, sport_type, start_date_local,
                    raw_json_path, streams_json_path, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(activity_id) DO UPDATE SET
                    source=excluded.source,
                    name=excluded.name,
                    sport_type=excluded.sport_type,
                    start_date_local=excluded.start_date_local,
                    raw_json_path=excluded.raw_json_path,
                    streams_json_path=excluded.streams_json_path,
                    synced_at=CURRENT_TIMESTAMP
                """,
                (
                    metrics.activity_id,
                    source,
                    metrics.name,
                    metrics.sport_type,
                    metrics.start_date_local,
                    str(raw_json_path),
                    str(streams_json_path),
                ),
            )

            columns = list(m.keys())
            placeholders = ",".join("?" for _ in columns)
            update_clause = ",".join(f"{col}=excluded.{col}" for col in columns if col != "activity_id")
            conn.execute(
                f"""
                INSERT INTO metrics ({','.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(activity_id) DO UPDATE SET {update_clause}
                """,
                tuple(m.get(col) for col in columns),
            )

    def read_activities_dataframe(self) -> pd.DataFrame:
        with self.connect() as conn:
            df = pd.read_sql_query(
                """
                SELECT
                    m.*,
                    a.source,
                    a.raw_json_path,
                    a.streams_json_path,
                    a.synced_at
                FROM metrics m
                JOIN activities a ON a.activity_id = m.activity_id
                ORDER BY m.start_date_local DESC
                """,
                conn,
            )
        if df.empty:
            return df
        # Human-friendly derived columns for grids/plots. These do not replace raw SI units.
        if "distance_m" in df:
            df["distance_km"] = df["distance_m"] / 1000.0
        if "moving_time_s" in df:
            df["moving_time_min"] = df["moving_time_s"] / 60.0
        if "elapsed_time_s" in df:
            df["elapsed_time_min"] = df["elapsed_time_s"] / 60.0
        for col in ["z1_s", "z2_s", "z3_s", "z4_s", "z5_s", "easy_zone_s", "hard_zone_s"]:
            if col in df:
                df[col.replace("_s", "_min")] = df[col] / 60.0
        return self._augment_profile_power_metrics(df)


    def _augment_profile_power_metrics(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add profile-, BMI-, and power-derived columns to an activity DataFrame.

        This method is deliberately non-destructive:
        - manual DB fields such as body_weight_kg and apple_vo2max are not
          overwritten in the database here;
        - missing values are filled only in the returned DataFrame so the GUI,
          recommendations, exports and statistics have usable columns;
        - set_activity_body_weight(...) remains the explicit DB write path for
          per-workout weight/BMI.

        It also fixes compatibility for older databases where these columns may
        not have existed when the app started.
        """
        if df is None or df.empty:
            return df

        df = df.copy()

        birthdate = self.get_setting("profile_birthdate", "1993-06-16") or "1993-06-16"
        gender_default = self.get_setting("profile_gender", "Male") or "Male"
        height_cm = self.get_float_setting("profile_height_cm", 178.0)
        profile_weight_kg = self.get_float_setting("profile_current_weight_kg", 75.0)

        def _missing(value: Any) -> bool:
            try:
                return value is None or (isinstance(value, float) and math.isnan(value)) or bool(pd.isna(value))
            except Exception:
                return value is None

        def _row_float(row: Any, *keys: str) -> Optional[float]:
            for key in keys:
                if key in row.index:
                    val = _safe_float(row.get(key))
                    if val is not None:
                        return val
            return None

        def _is_ride_sport(sport: Any) -> bool:
            s = _norm_sport(sport)
            return s in {
                "ride", "cycling", "bike", "virtualride", "gravelride",
                "mountainbikeride", "ebikeride", "indoorcycling", "workout"
            } or "ride" in s or "cycling" in s or "bike" in s

        def _is_virtual_ride_sport(sport: Any) -> bool:
            s = _norm_sport(sport)
            return "virtualride" in s or "indoor" in s or "ergo" in s or "trainer" in s

        def _is_run_sport(sport: Any) -> bool:
            s = _norm_sport(sport)
            return s in {"run", "running", "trailrun", "virtualrun"} or "run" in s

        # Ensure the columns exist before assignment. These may be virtual
        # DataFrame-only columns even if not persisted in SQLite.
        for col in [
            "body_weight_kg_effective",
            "bmi", "bmi_category", "age_years_at_activity", "gender",
            "bike_inferred_power_w", "cycling_power_w", "run_equivalent_power_w",
            "bike_equivalent_power_w", "effective_power_w",
            "power_hr_efficiency", "power_hr_efficiency_drift_pct",
            "own_vo2max_estimate", "estimated_vo2max",
            "cardio_efficiency_index", "temp_adjusted_efficiency",
            "combined_fitness_score", "fitness_score_ma7", "fitness_score_ma14", "fitness_score_ma35", "vo2max_35d_trend",
        ]:
            if col not in df.columns:
                df[col] = None

        # Row-wise augmentation is easier and safer here because each activity can
        # have different sport type, manually entered weight and start time.
        for idx, row in df.iterrows():
            sport = row.get("sport_type")
            weight = _row_float(row, "body_weight_kg")
            if weight is None:
                weight = profile_weight_kg

            if weight is not None:
                df.at[idx, "body_weight_kg_effective"] = round(float(weight), 2)

            # Age/gender at activity.
            if _missing(row.get("gender")):
                df.at[idx, "gender"] = gender_default
            if _missing(row.get("age_years_at_activity")):
                df.at[idx, "age_years_at_activity"] = _age_years_at(birthdate, row.get("start_date_local"))

            # BMI. Do not overwrite an existing manually stored BMI if it exists.
            bmi = _safe_float(row.get("bmi"))
            if bmi is None:
                bmi = _calc_bmi(weight, height_cm)
                if bmi is not None:
                    df.at[idx, "bmi"] = bmi
            if _missing(row.get("bmi_category")):
                df.at[idx, "bmi_category"] = _bmi_category(bmi)

            # Source power. Keep this list broad because TCX/Strava versions
            # can name average power differently.
            avg_power = _row_float(
                row,
                "avg_power", "average_power", "average_power_w", "power_w",
                "weighted_average_watts", "weighted_average_power",
            )
            norm_power = _row_float(row, "normalized_power", "xpower", "weighted_power")
            best_power = _row_float(row, "best_5min_power", "best_20min_power")
            source_power = avg_power or norm_power or best_power

            inferred_bike_power = _safe_float(row.get("bike_inferred_power_w"))
            if inferred_bike_power is None:
                inferred_bike_power = _infer_bike_power_w(dict(row), default_weight_kg=float(weight or profile_weight_kg or 75.0))
                if inferred_bike_power is not None:
                    df.at[idx, "bike_inferred_power_w"] = inferred_bike_power

            cycling_power = _safe_float(row.get("cycling_power_w"))
            run_equiv = _safe_float(row.get("run_equivalent_power_w"))
            bike_equiv = _safe_float(row.get("bike_equivalent_power_w"))

            if _is_ride_sport(sport):
                # For virtual/ergometer rides the measured watts are the main
                # performance signal. For outdoor rides use measured power if
                # available, otherwise infer from speed/elevation/mass.
                if cycling_power is None:
                    cycling_power = source_power if source_power is not None else inferred_bike_power
                if cycling_power is not None:
                    df.at[idx, "cycling_power_w"] = round(float(cycling_power), 1)
                if bike_equiv is None and cycling_power is not None:
                    bike_equiv = float(cycling_power)
                    df.at[idx, "bike_equivalent_power_w"] = round(bike_equiv, 1)
                if run_equiv is None and cycling_power is not None:
                    # User-specific equivalence: 175 W running ~= 210 W cycling.
                    run_equiv = float(cycling_power) * (175.0 / 210.0)
                    df.at[idx, "run_equivalent_power_w"] = round(run_equiv, 1)

            elif _is_run_sport(sport):
                if run_equiv is None and source_power is not None:
                    run_equiv = float(source_power)
                    df.at[idx, "run_equivalent_power_w"] = round(run_equiv, 1)
                if bike_equiv is None and run_equiv is not None:
                    bike_equiv = float(run_equiv) * (210.0 / 175.0)
                    df.at[idx, "bike_equivalent_power_w"] = round(bike_equiv, 1)
                if cycling_power is None and bike_equiv is not None:
                    # For plotting comparison only; not meant as measured cycling power.
                    df.at[idx, "cycling_power_w"] = round(bike_equiv, 1)

            else:
                # Unknown sport: keep source power as effective if present.
                if run_equiv is None and source_power is not None:
                    run_equiv = float(source_power)
                    df.at[idx, "run_equivalent_power_w"] = round(run_equiv, 1)

            effective = _safe_float(row.get("effective_power_w"))
            if effective is None:
                effective = run_equiv
                if effective is None and _is_ride_sport(sport):
                    effective = (cycling_power * (175.0 / 210.0)) if cycling_power is not None else None
                if effective is None:
                    effective = source_power
                if effective is not None:
                    df.at[idx, "effective_power_w"] = round(float(effective), 1)

            # Power/HR efficiency.
            phr = _safe_float(row.get("power_hr_efficiency"))
            hr = _row_float(row, "avg_hr", "average_hr", "average_heartrate")
            if phr is None and effective is not None and hr is not None and hr > 0:
                phr = float(effective) / float(hr)
                df.at[idx, "power_hr_efficiency"] = round(phr, 4)

            # If no explicit power drift exists, expose the existing HR-efficiency
            # drift as a conservative fallback so the column is plottable.
            pdrift = _safe_float(row.get("power_hr_efficiency_drift_pct"))
            if pdrift is None:
                for key in ("hr_efficiency_drift_pct", "gap_hr_efficiency_drift_pct", "km_gap_hr_efficiency_drift_pct"):
                    val = _safe_float(row.get(key))
                    if val is not None:
                        df.at[idx, "power_hr_efficiency_drift_pct"] = val
                        break

        # Own VO2max estimate and combined fitness trend. This is a derived
        # estimate from speed/grade/HR, separate from manually entered Apple VO2max.
        for idx, row in df.iterrows():
            own_vo2 = _safe_float(row.get("own_vo2max_estimate")) or _estimate_own_vo2max_from_row(dict(row))
            if own_vo2 is not None:
                df.at[idx, "own_vo2max_estimate"] = round(float(own_vo2), 2)
                if _safe_float(row.get("estimated_vo2max")) is None:
                    df.at[idx, "estimated_vo2max"] = round(float(own_vo2), 2)

            eff_idx = _cardio_efficiency_index(dict(row))
            if eff_idx is not None:
                df.at[idx, "cardio_efficiency_index"] = round(eff_idx, 4)
                temp_dev = _safe_float(row.get("weather_temp_deviation_from_15_c"))
                temp_penalty = 1.0 + 0.012 * max(0.0, float(temp_dev or 0.0))
                df.at[idx, "temp_adjusted_efficiency"] = round(eff_idx * temp_penalty, 4)

        df = _calculate_combined_fitness_scores(df)
        return df


    def read_activity_row(self, activity_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT m.*, a.source, a.raw_json_path, a.streams_json_path, a.synced_at
                FROM metrics m
                JOIN activities a ON a.activity_id = m.activity_id
                WHERE m.activity_id = ?
                """,
                (activity_id,),
            ).fetchone()
        if not row:
            return None
        df = self._augment_profile_power_metrics(pd.DataFrame([dict(row)]))
        return df.to_dict("records")[0] if not df.empty else dict(row)

    def set_activity_apple_vo2max(self, activity_id: str, value: Optional[float]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE metrics SET apple_vo2max = ? WHERE activity_id = ?", (value, activity_id))

    def set_activity_body_weight(self, activity_id: str, weight_kg: Optional[float]) -> None:
        """Store manually recorded body weight for one workout and calculate BMI.

        BMI requires body height; this is taken from profile_height_cm. Age and
        gender are stored alongside the row so historical exports are self-contained.
        """
        height_cm = self.get_float_setting("profile_height_cm", 178.0)
        birthdate = self.get_setting("profile_birthdate", "1993-06-16") or "1993-06-16"
        gender = self.get_setting("profile_gender", "Male") or "Male"
        bmi = _calc_bmi(weight_kg, height_cm)
        bmi_category = _bmi_category(bmi)
        age_years = None
        with self.connect() as conn:
            row = conn.execute("SELECT start_date_local FROM metrics WHERE activity_id = ?", (activity_id,)).fetchone()
            if row:
                age_years = _age_years_at(birthdate, row["start_date_local"])
            conn.execute(
                """
                UPDATE metrics
                SET body_weight_kg = ?, bmi = ?, bmi_category = ?,
                    age_years_at_activity = ?, gender = ?
                WHERE activity_id = ?
                """,
                (weight_kg, bmi, bmi_category, age_years, gender, activity_id),
            )

    def get_profile(self) -> Dict[str, Any]:
        birthdate = self.get_setting("profile_birthdate", "1993-06-16") or "1993-06-16"
        gender = self.get_setting("profile_gender", "Male") or "Male"
        height_cm = self.get_float_setting("profile_height_cm", 178.0)
        weight_kg = self.get_float_setting("profile_current_weight_kg", 75.0)
        return {
            "birthdate": birthdate,
            "gender": gender,
            "height_cm": height_cm,
            "current_weight_kg": weight_kg,
            "age_years": _age_years_at(birthdate, datetime.now().isoformat()),
            "bmi": _calc_bmi(weight_kg, height_cm),
        }

    def read_recent_activities(self, limit: int = 30, before_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        params: List[Any] = []
        where = ""
        if before_iso:
            where = "WHERE m.start_date_local <= ?"
            params.append(before_iso)
        params.append(int(limit))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, a.source, a.raw_json_path, a.streams_json_path, a.synced_at
                FROM metrics m
                JOIN activities a ON a.activity_id = m.activity_id
                {where}
                ORDER BY m.start_date_local DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        records = [dict(r) for r in rows]
        if not records:
            return records
        df = self._augment_profile_power_metrics(pd.DataFrame(records))
        return df.to_dict("records")

    def read_all_activity_records_ordered(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, m.apple_vo2max
                FROM activities a
                LEFT JOIN metrics m ON m.activity_id = a.activity_id
                ORDER BY a.start_date_local ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def read_sport_types(self) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT sport_type
                FROM metrics
                WHERE sport_type IS NOT NULL AND TRIM(sport_type) <> ''
                ORDER BY sport_type COLLATE NOCASE
                """
            ).fetchall()
        return [str(r["sport_type"]) for r in rows]

    def find_similar_activity_id(
        self,
        sport_type: Optional[str],
        start_date_local: Optional[str],
        moving_time_s: Optional[float],
        distance_m: Optional[float],
        exclude_activity_id: Optional[str] = None,
    ) -> Optional[str]:
        start = _parse_dt(start_date_local)
        if start is None or moving_time_s is None or moving_time_s <= 0:
            return None
        target_sport = _norm_sport(sport_type)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT activity_id, sport_type, start_date_local, moving_time_s, distance_m
                FROM metrics
                """
            ).fetchall()
        best: Optional[tuple[float, str]] = None
        for row in rows:
            other_id = str(row["activity_id"])
            if exclude_activity_id and other_id == exclude_activity_id:
                continue
            if target_sport and _norm_sport(row["sport_type"]) and target_sport != _norm_sport(row["sport_type"]):
                continue
            start_diff_s = _minimum_start_diff_s(start_date_local, row["start_date_local"])
            if start_diff_s is None:
                continue
            # 15 min normally; the multiple-variant comparison also catches the
            # common UTC/Zulu vs local-clock export mismatch.
            if start_diff_s > 900:
                continue
            other_duration = _safe_float(row["moving_time_s"])
            if other_duration is None or other_duration <= 0:
                continue
            duration_tol = max(180.0, 0.07 * max(float(moving_time_s), other_duration))
            if abs(float(moving_time_s) - other_duration) > duration_tol:
                continue
            other_dist = _safe_float(row["distance_m"])
            if distance_m is not None and other_dist is not None and distance_m > 0 and other_dist > 0:
                dist_tol = _distance_tolerance_m(target_sport, max(float(distance_m), other_dist))
                if abs(float(distance_m) - other_dist) > dist_tol:
                    continue
                score = start_diff_s + abs(float(moving_time_s) - other_duration) / 2.0 + abs(float(distance_m) - other_dist) / 20.0
            else:
                score = start_diff_s + abs(float(moving_time_s) - other_duration) / 2.0
            if best is None or score < best[0]:
                best = (score, other_id)
        return best[1] if best else None

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )

    def get_float_setting(self, key: str, default: float) -> float:
        value = self.get_setting(key, None)
        try:
            return float(value) if value is not None and value != "" else float(default)
        except Exception:
            return float(default)

    def get_int_setting(self, key: str, default: int) -> int:
        value = self.get_setting(key, None)
        try:
            return int(float(value)) if value is not None and value != "" else int(default)
        except Exception:
            return int(default)

    def get_json_setting(self, key: str, default: Any) -> Any:
        value = self.get_setting(key, None)
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    def set_json_setting(self, key: str, value: Any) -> None:
        self.set_setting(key, json.dumps(value, indent=2, sort_keys=True))

    def read_plan_overrides(self, start_date_iso: str, days: int = 14) -> Dict[str, Dict[str, Any]]:
        start = date.fromisoformat(start_date_iso)
        end = start + timedelta(days=max(0, int(days) - 1))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT plan_date, override_json
                FROM workout_plan_overrides
                WHERE plan_date >= ? AND plan_date <= ?
                ORDER BY plan_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            try:
                out[str(r["plan_date"])] = json.loads(str(r["override_json"]))
            except Exception:
                pass
        return out

    def upsert_plan_override(self, plan_date: str, override: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workout_plan_overrides (plan_date, override_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(plan_date) DO UPDATE SET
                    override_json=excluded.override_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (plan_date, json.dumps(override, indent=2, sort_keys=True)),
            )

    def delete_plan_override(self, plan_date: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM workout_plan_overrides WHERE plan_date = ?", (plan_date,))


    def set_activity_weather(
        self,
        activity_id: str,
        temp_c: Optional[float],
        apparent_temp_c: Optional[float],
        latitude: Optional[float],
        longitude: Optional[float],
        source: str,
        fetched_at: Optional[str] = None,
    ) -> None:
        """Store historical/archived weather temperature for one activity.

        avg_temp_c is also updated because the recommender/planner already use
        metric rows. The weather_* columns preserve the provenance.
        """
        if fetched_at is None:
            fetched_at = datetime.now().isoformat(timespec="seconds")
        deviation = None
        if temp_c is not None:
            try:
                deviation = abs(float(temp_c) - 15.0)
            except Exception:
                deviation = None
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE metrics
                SET avg_temp_c = COALESCE(?, avg_temp_c),
                    weather_temp_c = ?,
                    weather_apparent_temp_c = ?,
                    weather_temp_deviation_from_15_c = ?,
                    weather_lat = ?,
                    weather_lon = ?,
                    weather_source = ?,
                    weather_fetched_at = ?
                WHERE activity_id = ?
                """,
                (temp_c, temp_c, apparent_temp_c, deviation, latitude, longitude, source, fetched_at, activity_id),
            )

    def get_weather_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM weather_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        return dict(row) if row else None

    def set_weather_cache(
        self,
        cache_key: str,
        latitude: float,
        longitude: float,
        local_hour: str,
        temp_c: Optional[float],
        apparent_temp_c: Optional[float],
        source: str,
        response_json: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO weather_cache
                (cache_key, latitude, longitude, local_hour, temp_c, apparent_temp_c, source, response_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cache_key) DO UPDATE SET
                    temp_c=excluded.temp_c,
                    apparent_temp_c=excluded.apparent_temp_c,
                    source=excluded.source,
                    response_json=excluded.response_json,
                    fetched_at=CURRENT_TIMESTAMP
                """,
                (cache_key, latitude, longitude, local_hour, temp_c, apparent_temp_c, source, response_json),
            )

    def delete_activity(self, activity_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM metrics WHERE activity_id = ?", (activity_id,))
            conn.execute("DELETE FROM activities WHERE activity_id = ?", (activity_id,))

    def cleanup_duplicates(self) -> Dict[str, Any]:
        """Remove duplicate Strava/API activities when a richer local TCX row exists.

        The matcher uses several overlapping signals: sport type, start-time
        overlap with timezone/Zulu variants, duration, distance, starting GPS
        point, and route similarity. When sufficient overlap exists, the TCX row
        is kept and the Strava/API row is removed.
        """
        from .duplicate_cleanup import cleanup_duplicate_activities
        return cleanup_duplicate_activities(self)

    def export_xlsx(self, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = self.read_activities_dataframe()
        df.to_excel(out_path, index=False)
        return out_path



def _age_years_at(birthdate_iso: Optional[str], when_iso: Optional[str]) -> Optional[float]:
    try:
        if not birthdate_iso or not when_iso:
            return None
        b = date.fromisoformat(str(birthdate_iso)[:10])
        w = datetime.fromisoformat(str(when_iso).replace("Z", "+00:00")).date()
        years = w.year - b.year - ((w.month, w.day) < (b.month, b.day))
        day_frac = ((w.month, w.day) >= (b.month, b.day))
        return float(years)
    except Exception:
        return None


def _calc_bmi(weight_kg: Optional[float], height_cm: Optional[float]) -> Optional[float]:
    try:
        w = float(weight_kg)
        h = float(height_cm) / 100.0
        if w <= 0 or h <= 0:
            return None
        return round(w / (h * h), 2)
    except Exception:
        return None


def _bmi_category(bmi: Optional[float]) -> Optional[str]:
    try:
        x = float(bmi)
    except Exception:
        return None
    if x < 18.5:
        return "underweight"
    if x < 25.0:
        return "normal"
    if x < 30.0:
        return "overweight"
    return "obesity"


def _infer_bike_power_w(row: Dict[str, Any], default_weight_kg: float = 75.0) -> Optional[float]:
    """Approximate outdoor cycling power from speed, elevation and mass.

    Assumptions intentionally model a normal/older bike rather than a racing aero
    setup: Crr=0.006, CdA=0.45, bike+gear=15 kg, drivetrain efficiency=0.97.
    This is a rough estimate and should be treated as a trend feature, not a lab value.
    """
    try:
        sport = _norm_sport(row.get("sport_type"))
        if sport not in {"ride", "cycling", "bike", "gravelride", "mountainbikeride", "ebikeride"}:
            return None
        duration_s = _safe_float(row.get("moving_time_s")) or _safe_float(row.get("elapsed_time_s"))
        dist_m = _safe_float(row.get("distance_m"))
        if duration_s is None or duration_s <= 60 or dist_m is None or dist_m <= 500:
            return None
        v = dist_m / duration_s
        if v <= 1.0:
            return None
        weight = _safe_float(row.get("body_weight_kg")) or _safe_float(row.get("body_weight_kg_effective")) or default_weight_kg
        mass = float(weight) + 15.0
        elev_gain = max(0.0, _safe_float(row.get("elevation_gain_m")) or 0.0)
        g = 9.80665
        rho = 1.225
        crr = 0.006
        cda = 0.45
        eta = 0.97
        p_roll = crr * mass * g * v
        p_aero = 0.5 * rho * cda * (v ** 3)
        p_climb = mass * g * elev_gain / duration_s
        p_total = (p_roll + p_aero + p_climb) / eta
        return round(max(0.0, min(600.0, p_total)), 1)
    except Exception:
        return None



def _is_run_sport_name(value: Any) -> bool:
    s = _norm_sport(value)
    return s in {"run", "running", "trailrun", "virtualrun"} or "run" in s


def _is_hike_sport_name(value: Any) -> bool:
    s = _norm_sport(value)
    return s in {"hike", "hiking", "walk", "walking", "trek", "trekking"}


def _estimate_hrmax(age_years: Optional[float], gender: Optional[str] = None) -> float:
    # Tanaka-style estimate. Gender-specific formulas vary more than they help here;
    # the point is a stable internal estimate for HR-normalized VO2.
    age = float(age_years) if age_years is not None and age_years > 0 else 32.0
    return max(160.0, min(205.0, 208.0 - 0.7 * age))


def _estimate_own_vo2max_from_row(row: Dict[str, Any]) -> Optional[float]:
    """Estimate VO2max for run activities from pace/grade/HR.

    This is not a laboratory VO2max and is intentionally stored separately from
    Apple VO2max. It uses ACSM running oxygen cost and scales it by the fraction
    of estimated HRmax used during the activity.
    """
    try:
        if not _is_run_sport_name(row.get("sport_type")):
            return None
        duration_s = _safe_float(row.get("moving_time_s")) or _safe_float(row.get("elapsed_time_s"))
        dist_m = _safe_float(row.get("distance_m"))
        if duration_s is None or duration_s <= 300 or dist_m is None or dist_m < 800:
            return None
        avg_hr = _safe_float(row.get("avg_hr")) or _safe_float(row.get("average_hr"))
        if avg_hr is None or avg_hr < 80:
            return None
        age = _safe_float(row.get("age_years_at_activity"))
        hrmax = _estimate_hrmax(age, row.get("gender"))
        hr_frac = max(0.52, min(0.97, float(avg_hr) / hrmax))
        speed_m_min = float(dist_m) / (float(duration_s) / 60.0)
        elev_gain = max(0.0, _safe_float(row.get("elevation_gain_m")) or 0.0)
        grade = max(0.0, min(0.12, elev_gain / max(float(dist_m), 1.0)))
        vo2_demand = 3.5 + 0.2 * speed_m_min + 0.9 * speed_m_min * grade
        # If an activity is very easy, this estimate can be noisy; keep it plausible.
        est = vo2_demand / hr_frac
        temp_dev = _safe_float(row.get("weather_temp_deviation_from_15_c"))
        if temp_dev is not None and temp_dev > 0:
            # Temperature away from 15 C can raise HR for the same mechanical output;
            # compensate mildly so hot/cold runs do not look like pure fitness loss.
            est *= 1.0 + min(0.10, 0.004 * float(temp_dev))
        return round(max(20.0, min(75.0, est)), 2)
    except Exception:
        return None


def _cardio_efficiency_index(row: Dict[str, Any]) -> Optional[float]:
    try:
        dur_s = _safe_float(row.get("moving_time_s")) or _safe_float(row.get("elapsed_time_s"))
        dist_m = _safe_float(row.get("distance_m"))
        avg_hr = _safe_float(row.get("avg_hr")) or _safe_float(row.get("average_hr"))
        if dur_s is None or dur_s <= 60 or dist_m is None or dist_m <= 100 or avg_hr is None or avg_hr <= 0:
            return None
        speed_kmh = (dist_m / 1000.0) / (dur_s / 3600.0)
        power_eff = _safe_float(row.get("power_hr_efficiency"))
        base = speed_kmh / avg_hr * 100.0
        if power_eff is not None:
            base = 0.65 * base + 0.35 * float(power_eff)
        return max(0.0, min(20.0, base))
    except Exception:
        return None


def _calculate_combined_fitness_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    dt = pd.to_datetime(out.get("start_date_local"), errors="coerce", utc=True)
    out["_sort_dt_for_fitness"] = dt
    out = out.sort_values("_sort_dt_for_fitness").reset_index(drop=False)

    vo2 = pd.to_numeric(out.get("own_vo2max_estimate"), errors="coerce")
    apple = pd.to_numeric(out.get("apple_vo2max"), errors="coerce") if "apple_vo2max" in out else pd.Series(np.nan, index=out.index)
    eff = pd.to_numeric(out.get("temp_adjusted_efficiency"), errors="coerce")
    drift = pd.to_numeric(out.get("hr_efficiency_drift_pct"), errors="coerce") if "hr_efficiency_drift_pct" in out else pd.Series(np.nan, index=out.index)
    p_eff = pd.to_numeric(out.get("power_hr_efficiency"), errors="coerce") if "power_hr_efficiency" in out else pd.Series(np.nan, index=out.index)

    def scale_series(s: pd.Series, lo: float, hi: float) -> pd.Series:
        return ((s - lo) / max(hi - lo, 1e-9) * 100.0).clip(0, 100)

    comp = pd.DataFrame({
        "vo2": scale_series(vo2.combine_first(apple), 25, 55),
        "eff": scale_series(eff, 3.0, 8.0),
        "power_hr": scale_series(p_eff, 0.8, 1.8),
        "drift": (70.0 - drift.fillna(0.0) * 2.0).clip(0, 100),
    })
    score = comp.mean(axis=1, skipna=True)
    out["combined_fitness_score"] = score.round(2)
    out["fitness_score_ma7"] = score.rolling(7, min_periods=2).mean().round(2)
    out["fitness_score_ma14"] = score.rolling(14, min_periods=3).mean().round(2)
    out["fitness_score_ma35"] = score.rolling(35, min_periods=5).mean().round(2)

    # 35-day VO2 trend in VO2 units over 35 days.
    trend_vals = []
    vo2_for_trend = vo2.combine_first(apple)
    days = (out["_sort_dt_for_fitness"] - out["_sort_dt_for_fitness"].min()).dt.total_seconds() / 86400.0
    for i in range(len(out)):
        start = max(0, i - 34)
        xs = days.iloc[start:i+1].to_numpy(dtype=float)
        ys = vo2_for_trend.iloc[start:i+1].to_numpy(dtype=float)
        mask = np.isfinite(xs) & np.isfinite(ys)
        if mask.sum() >= 3 and (np.nanmax(xs[mask]) - np.nanmin(xs[mask])) >= 7:
            slope = float(np.polyfit(xs[mask], ys[mask], 1)[0])
            trend_vals.append(round(slope * 35.0, 2))
        else:
            trend_vals.append(None)
    out["vo2max_35d_trend"] = trend_vals

    out = out.sort_values("index").drop(columns=["index", "_sort_dt_for_fitness"], errors="ignore")
    return out

def _dt_variants(value: Optional[str]) -> List[datetime]:
    if not value:
        return []
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return []
    local_tz = ZoneInfo("Europe/Vienna")
    variants: List[datetime] = []
    if dt.tzinfo is not None:
        variants.append(dt.astimezone(timezone.utc).replace(tzinfo=None))
        variants.append(dt.astimezone(local_tz).replace(tzinfo=None))
        variants.append(dt.replace(tzinfo=None))
    else:
        variants.append(dt)
        variants.append(dt.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None))
        variants.append(dt.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None))
    out: List[datetime] = []
    for v in variants:
        if not any(abs((v - u).total_seconds()) < 1 for u in out):
            out.append(v)
    return out


def _minimum_start_diff_s(a: Optional[str], b: Optional[str]) -> Optional[float]:
    va = _dt_variants(a)
    vb = _dt_variants(b)
    if not va or not vb:
        return None
    return min(abs((x - y).total_seconds()) for x in va for y in vb)

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _norm_sport(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _distance_tolerance_m(norm_sport: str, distance_m: float) -> float:
    if norm_sport in {"run", "running", "trailrun", "virtualrun"}:
        return max(200.0, distance_m * 0.03)
    if norm_sport in {"ride", "cycling", "bike", "virtualride", "gravelride", "mountainbikeride", "ebikeride"}:
        return max(1000.0, distance_m * 0.04)
    return max(300.0, distance_m * 0.05)
