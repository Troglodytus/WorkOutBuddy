from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

import requests

from .config import AppConfig
from .strava_oauth import StravaAuthenticator, TokenBundle


class StravaRateLimitError(RuntimeError):
    def __init__(self, message: str, short_limit: str | None = None, long_limit: str | None = None, short_usage: str | None = None, long_usage: str | None = None):
        super().__init__(message)
        self.short_limit = short_limit
        self.long_limit = long_limit
        self.short_usage = short_usage
        self.long_usage = long_usage


class StravaClient:
    BASE_URL = "https://www.strava.com/api/v3"

    def __init__(self, config: AppConfig):
        self.config = config
        self.auth = StravaAuthenticator(config)
        self._token: Optional[TokenBundle] = None

    @property
    def token(self) -> TokenBundle:
        if self._token is None or self._token.expires_at <= int(time.time()) + 90:
            self._token = self.auth.get_valid_token()
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token.access_token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.BASE_URL}{path}"
        response = requests.request(method, url, headers=self._headers(), timeout=60, **kwargs)
        if response.status_code == 401:
            # Token may have expired; force refresh and retry once.
            self._token = self.auth.get_valid_token()
            response = requests.request(method, url, headers=self._headers(), timeout=60, **kwargs)
        if response.status_code == 429:
            raise StravaRateLimitError(
                "Strava rate limit reached. Wait until the current 15-minute window resets, or use the TCX folder import instead.",
                short_limit=response.headers.get("X-RateLimit-Limit"),
                long_limit=response.headers.get("X-RateLimit-Limit"),
                short_usage=response.headers.get("X-RateLimit-Usage"),
                long_usage=response.headers.get("X-RateLimit-Usage"),
            )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def get_logged_in_athlete(self) -> Dict[str, Any]:
        return self.request("GET", "/athlete")

    def iter_activities(
        self,
        after_epoch: Optional[int] = None,
        before_epoch: Optional[int] = None,
        per_page: int = 100,
        max_pages: Optional[int] = None,
    ) -> Iterable[Dict[str, Any]]:
        page = 1
        while True:
            params: Dict[str, Any] = {"page": page, "per_page": per_page}
            if after_epoch is not None:
                params["after"] = after_epoch
            if before_epoch is not None:
                params["before"] = before_epoch

            activities = self.request("GET", "/athlete/activities", params=params)
            if not activities:
                break
            for activity in activities:
                yield activity
            page += 1
            if max_pages is not None and page > max_pages:
                break

    def get_activity(self, activity_id: int | str) -> Dict[str, Any]:
        return self.request("GET", f"/activities/{activity_id}")

    def get_activity_streams(self, activity_id: int | str) -> Dict[str, Any]:
        # key_by_type=true returns a dict keyed by stream type.
        keys = [
            "time",
            "distance",
            "latlng",
            "altitude",
            "velocity_smooth",
            "heartrate",
            "cadence",
            "watts",
            "temp",
            "moving",
            "grade_smooth",
        ]
        try:
            return self.request(
                "GET",
                f"/activities/{activity_id}/streams",
                params={"keys": ",".join(keys), "key_by_type": "true"},
            ) or {}
        except requests.HTTPError as e:
            # Some activity types or older imports may not provide streams.
            if e.response is not None and e.response.status_code in {400, 404}:
                return {}
            raise

    def get_activity_zones(self, activity_id: int | str) -> List[Dict[str, Any]]:
        try:
            return self.request("GET", f"/activities/{activity_id}/zones") or []
        except requests.HTTPError as e:
            # Strava marks zones as a Summit/SUBSCRIPTION feature; we infer zones locally.
            if e.response is not None and e.response.status_code in {400, 401, 402, 403, 404}:
                return []
            raise
