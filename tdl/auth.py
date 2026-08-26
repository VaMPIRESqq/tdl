"""TIDAL device and PKCE authentication flows."""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from .api import TidalApi, TidalApiError
from .models import Token
from .storage import delete_token, load_token, save_token

SCOPE = "r_usr w_usr w_sub"
REDIRECT_URI = "https://tidal.com/android/login/auth"

# These values intentionally follow the existing application credentials.
def _decode_credential(first: str, second: str) -> str:
    # The original client stores each half as base64, then base64-encodes the
    # concatenated intermediate string once more.
    intermediate = base64.b64decode(first) + base64.b64decode(second)
    return base64.b64decode(intermediate).decode()


OAUTH_CLIENT_ID = _decode_credential("WmxneVNuaGtiVzUw", "V2xkTE1HbDRWQT09")
OAUTH_CLIENT_SECRET = _decode_credential(
    "TVU1dU9VRm1SRUZxZUhKblNrWktZa3RPVjB4bFFY", "bExSMVpIYlVsT2RWaFFVRXhJVmxoQmRuaEJaejA9"
)
PKCE_CLIENT_ID = _decode_credential("TmtKRVUxSmtjRXM=", "NWFIRkZRbFJuVlE9PQ==")
PKCE_CLIENT_SECRET = _decode_credential(
    "ZUdWMVVHMVpOMjVpY0ZvNVNVbGlURUZqVVQ=", "a3pjMmhyWVRGV1RtaGxWVUZ4VGpaSlkzTjZhbFJIT0QwPQ=="
)


def _apply_token_response(token: Token, payload: dict) -> None:
    if payload.get("access_token"):
        token.access_token = payload["access_token"]
    if payload.get("refresh_token"):
        token.refresh_token = payload["refresh_token"]
    token.token_type = payload.get("token_type", token.token_type)
    if payload.get("expires_in"):
        token.expiry_time = time.time() + float(payload["expires_in"])
    save_token(token)


class TidalAuth:
    def __init__(self, api: TidalApi) -> None:
        self.api = api
        self.token = load_token()
        self.api.set_token(self.token)

    def validate(self) -> bool:
        if not self.token.valid:
            return False
        response = self.api.get_json("sessions")
        self._set_session_info(response)
        return True

    def _set_session_info(self, payload: dict) -> None:
        self.token.session_id = payload.get("sessionId", self.token.session_id)
        self.token.country_code = payload.get("countryCode", self.token.country_code)
        self.token.user_id = payload.get("userId", self.token.user_id)
        save_token(self.token)

    def refresh(self) -> None:
        if not self.token.refresh_token:
            raise TidalApiError("No refresh token available")
        client_id, client_secret = (PKCE_CLIENT_ID, PKCE_CLIENT_SECRET) if self.token.is_pkce else (OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET)
        payload = self.api.post_auth("token", {
            "grant_type": "refresh_token", "refresh_token": self.token.refresh_token,
            "client_id": client_id, "client_secret": client_secret,
        })
        if payload.get("error"):
            raise TidalApiError(payload.get("error_description", payload["error"]))
        _apply_token_response(self.token, payload)
        self.api.set_token(self.token)

    def restore(self) -> bool:
        try:
            if self.validate():
                return True
        except TidalApiError:
            pass
        try:
            if self.token.restorable:
                self.refresh()
                self._set_session_info(self.api.get_json("sessions"))
                return True
        except TidalApiError:
            pass
        return False

    def login_device(self, callback: Callable[[str, str], None] | None = None) -> None:
        payload = self.api.post_auth("device_authorization", {"client_id": OAUTH_CLIENT_ID, "scope": SCOPE})
        if payload.get("error"):
            raise TidalApiError(payload.get("error_description", payload["error"]))
        url = payload.get("verification_uri_complete") or f"{payload['verification_uri'].rstrip('/')}/{payload['user_code']}"
        if not url.startswith("http"):
            url = f"https://{url}"
        (callback or (lambda u, c: print(f"Open {u} and enter {c}")))(url, payload["user_code"])
        webbrowser.open(url)
        deadline = time.time() + payload["expires_in"]
        form = {"client_id": OAUTH_CLIENT_ID, "client_secret": OAUTH_CLIENT_SECRET,
                "device_code": payload["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "scope": SCOPE}
        interval = payload.get("interval", 5)
        while time.time() < deadline:
            time.sleep(interval)
            result = self.api.post_auth("token", form)
            if result.get("error") == "authorization_pending":
                continue
            if result.get("error") == "slow_down":
                time.sleep(5)
                continue
            if result.get("error"):
                raise TidalApiError(result.get("error_description", result["error"]))
            _apply_token_response(self.token, result)
            self.api.set_token(self.token)
            self._set_session_info(self.api.get_json("sessions"))
            return
        raise TidalApiError("Device authorization timed out")

    def build_pkce_url(self) -> tuple[str, str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        unique_key = secrets.token_hex(16)
        query = {"response_type": "code", "redirect_uri": REDIRECT_URI, "client_id": PKCE_CLIENT_ID,
                 "lang": "EN", "appMode": "android", "client_unique_key": unique_key,
                 "code_challenge": challenge, "code_challenge_method": "S256"}
        return "https://login.tidal.com/authorize?" + urlencode(query), verifier, unique_key

    def exchange_pkce(self, redirect_url: str, verifier: str, unique_key: str) -> None:
        parsed = urlparse(redirect_url)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if not code:
            raise TidalApiError("Could not extract authorization code from redirect URL")
        payload = self.api.post_auth("token", {"code": code, "client_id": PKCE_CLIENT_ID,
            "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "code_verifier": verifier, "client_unique_key": unique_key})
        if payload.get("error"):
            raise TidalApiError(payload.get("error_description", payload["error"]))
        self.token.is_pkce = True
        _apply_token_response(self.token, payload)
        self.api.set_token(self.token)
        self._set_session_info(self.api.get_json("sessions"))

    def logout(self) -> None:
        self.token = Token()
        delete_token()
        self.api.set_token(self.token)

    def login_for_cli(self, pkce: bool = False) -> None:
        # A regular device token cannot be upgraded to Hi-Res; explicitly
        # requested PKCE must always create a PKCE session.
        if not pkce and self.restore():
            return
        if pkce:
            url, verifier, unique_key = self.build_pkce_url()
            print("Open this URL in a browser:\n", url)
            print("Paste the full redirect URL: ", end="", flush=True)
            self.exchange_pkce(sys.stdin.readline().strip(), verifier, unique_key)
        else:
            self.login_device()
