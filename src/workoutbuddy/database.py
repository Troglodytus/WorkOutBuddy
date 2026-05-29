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

from .metrics import ActivityMetrics


TEXT_COLUMNS = {"activity_id", "sport_type", "name", "start_date_local", "zones_json", "km_splits_json", "best_efforts_json", "flags_json", "weather_source", "weather_fetched_at"}
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
                "SELECT apple_vo2max, source FROM metrics LEFT JOIN activities USING(activity_id) WHERE metrics.activity_id = ?",
                (metrics.activity_id,),
            ).fetchone()
            if existing is not None and existing["apple_vo2max"] is not None:
                m["apple_vo2max"] = existing["apple_vo2max"]
            else:
                m["apple_vo2max"] = None

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
        return dict(row) if row else None

    def set_activity_apple_vo2max(self, activity_id: str, value: Optional[float]) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE metrics SET apple_vo2max = ? WHERE activity_id = ?", (value, activity_id))

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
        return [dict(r) for r in rows]

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
