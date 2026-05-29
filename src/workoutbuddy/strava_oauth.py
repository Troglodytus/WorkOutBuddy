from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .config import AppConfig


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: int
    scope: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "TokenBundle":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
            scope=data.get("scope", ""),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @property
    def expires_in_s(self) -> int:
        return max(0, int(self.expires_at) - int(time.time()))


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[TokenBundle]:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as f:
            return TokenBundle.from_json(json.load(f))

    def save(self, token: TokenBundle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(token.to_json(), f, indent=2)

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()


class StravaAuthenticator:
    AUTH_URL = "https://www.strava.com/oauth/authorize"
    TOKEN_URL = "https://www.strava.com/oauth/token"

    def __init__(self, config: AppConfig):
        self.config = config
        self.store = TokenStore(config.token_path)

    def has_token(self) -> bool:
        return self.store.load() is not None

    def authorization_url(self, force: bool = True) -> str:
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "approval_prompt": "force" if force else "auto",
            "scope": "read,activity:read_all",
        }
        return f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"

    def get_valid_token(self) -> TokenBundle:
        token = self.store.load()
        if token is None:
            raise RuntimeError(
                "No Strava token is stored yet. Open Settings/Auth in the web app and click 'Connect Strava'."
            )
        if token.expires_at <= int(time.time()) + 90:
            token = self.refresh_token(token.refresh_token)
        return token

    def exchange_code(self, code: str, accepted_scope: str = "") -> TokenBundle:
        response = requests.post(
            self.TOKEN_URL,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = TokenBundle(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
            scope=accepted_scope,
        )
        self.store.save(token)
        return token

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        response = requests.post(
            self.TOKEN_URL,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = TokenBundle(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
            scope=data.get("scope", ""),
        )
        self.store.save(token)
        return token
