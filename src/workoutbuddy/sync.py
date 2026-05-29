from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from .config import AppConfig
from .database import WorkoutDatabase
from .metrics import MetricCalculator
from .weather import update_activity_weather_from_archive
from .strava_client import StravaClient, StravaRateLimitError

ProgressCallback = Callable[[str], None]


class StravaSyncService:
    def __init__(self, config: AppConfig, db: WorkoutDatabase):
        self.config = config
        self.db = db
        self.client = StravaClient(config)
        self.calculator = MetricCalculator()

    def sync_recent(self, max_pages: int = 1, skip_known: bool = True, per_page: int = 30, progress: Optional[ProgressCallback] = None) -> int:
        """
        Conservative Strava API sync.

        Strava rate limits can be hit quickly if we fetch details and streams for hundreds
        of activities. Therefore the GUI calls this with max_pages=1, per_page=30,
        skip_known=True. TCX import remains the preferred full-data import path.
        """
        known = self.db.get_known_activity_ids() if skip_known else set()
        count = 0
        previous_start_by_activity: Dict[str, Optional[datetime]] = {}

        summaries = list(self.client.iter_activities(per_page=per_page, max_pages=max_pages))
        # Process from oldest to newest so time_since_previous_h is meaningful.
        summaries.sort(key=lambda x: x.get("start_date_local") or x.get("start_date") or "")

        previous_start: Optional[datetime] = None
        for summary in summaries:
            activity_id = str(summary.get("id"))
            if not activity_id or activity_id in known:
                continue
            if progress:
                progress(f"Fetching activity {activity_id}: {summary.get('name', 'Unnamed')} ...")

            try:
                detail = self.client.get_activity(activity_id)
                streams = self.client.get_activity_streams(activity_id)
            except StravaRateLimitError:
                if progress:
                    progress(f"Strava rate limit reached. Stopped after importing/updating {count} activities. Use TCX import for bulk data.")
                break
            # Do not call /activities/{id}/zones. Strava exposes that as a paid/Summit feature
            # for some accounts, and we infer HR zones locally from the HR stream/TCX data.
            zones = []

            start_str = detail.get("start_date_local") or summary.get("start_date_local") or ""
            start_dt = self._parse_datetime(start_str)

            # If the same workout was already imported from TCX/custom files, keep
            # the TCX row because it usually contains richer point streams. This
            # duplicate check deliberately ignores activity names.
            duplicate_id = self.db.find_similar_activity_id(
                sport_type=detail.get("sport_type") or summary.get("sport_type"),
                start_date_local=start_str,
                moving_time_s=detail.get("moving_time") or summary.get("moving_time"),
                distance_m=detail.get("distance") or summary.get("distance"),
                exclude_activity_id=activity_id,
            )
            if duplicate_id and str(self.db.get_activity_source(duplicate_id) or "").startswith("tcx"):
                if progress:
                    progress(f"Skipping Strava duplicate {activity_id}; keeping richer TCX activity {duplicate_id}.")
                if start_dt is not None:
                    previous_start = start_dt
                continue

            raw_path = self.config.raw_dir / f"{activity_id}.json"
            streams_path = self.config.streams_dir / f"{activity_id}.json"
            self._write_json(raw_path, {"summary": summary, "detail": detail, "zones_source": "local_inferred"})
            self._write_json(streams_path, streams)

            metrics = self.calculator.calculate(summary, detail, streams, zones=None, previous_start=previous_start)
            self.db.upsert_activity(metrics, raw_path, streams_path)
            try:
                weather = update_activity_weather_from_archive(self.db, metrics.activity_id)
                if weather and progress:
                    progress(f"Weather archive {metrics.activity_id}: {weather.get('temp_c')} °C")
            except Exception as weather_error:
                if progress:
                    progress(f"Weather archive skipped for {metrics.activity_id}: {weather_error}")
            if start_dt is not None:
                previous_start = start_dt
            count += 1

        if progress:
            progress(f"Sync complete. Imported/updated {count} activities.")
        return count

    @staticmethod
    def _write_json(path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
