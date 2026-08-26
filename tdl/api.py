"""TIDAL API client and response helpers."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

from .models import Album, Artist, PlaybackInfo, Track

log = logging.getLogger(__name__)

API_V1 = "https://api.tidal.com/v1/"
AUTH_URL = "https://auth.tidal.com/v1/oauth2/"
USER_AGENT = "Mozilla/5.0 (Linux; Android 12; wv) AppleWebKit/537.36 Chrome/91.0.4472.114 Safari/537.36"
CLIENT_VERSION = "2025.7.16"


class TidalApiError(RuntimeError):
    """Raised when a TIDAL request cannot be completed."""


class TidalApi:
    def __init__(self, token: Any | None = None, timeout: float = 45.0) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "x-tidal-client-version": CLIENT_VERSION})
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, pool_block=True)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.token = token
        self.timeout = timeout

    def set_token(self, token: Any) -> None:
        self.token = token

    def request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                data: dict[str, Any] | None = None, authenticated: bool = True,
                retries: int = 5) -> requests.Response:
        params = dict(params or {})
        if authenticated and self.token:
            if getattr(self.token, "session_id", None):
                params["sessionId"] = self.token.session_id
            if getattr(self.token, "country_code", None):
                params["countryCode"] = self.token.country_code
        headers: dict[str, str] = {}
        if authenticated and self.token and self.token.access_token:
            headers["Authorization"] = f"{self.token.token_type or 'Bearer'} {self.token.access_token}"

        for attempt in range(retries + 1):
            try:
                response = self.session.request(method, url, params=params, data=data,
                                                headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= retries:
                    raise TidalApiError(f"Request failed for {url}: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= retries:
                    raise TidalApiError(f"Request failed after retries ({response.status_code}): {url}")
                time.sleep(2 ** (attempt + 1))
                continue
            return response
        raise TidalApiError(f"Request failed: {url}")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.request("GET", urljoin(API_V1, path), params=params)
        if not response.ok:
            raise TidalApiError(f"API {path} returned {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TidalApiError(f"Invalid JSON from {path}: {response.text[:300]}") from exc

    def post_auth(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        response = self.request("POST", urljoin(AUTH_URL, path), data=data, authenticated=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TidalApiError(f"Invalid auth response ({response.status_code})") from exc
        if not response.ok and "error" not in payload:
            raise TidalApiError(f"Auth endpoint returned {response.status_code}: {response.text[:500]}")
        return payload

    def raw(self, url: str, *, authenticated: bool = False) -> requests.Response:
        return self.request("GET", url, authenticated=authenticated)

    def playback_info(self, track_id: int, quality: str) -> PlaybackInfo:
        raw = self.get_json(f"tracks/{track_id}/playbackinfopostpaywall", {
            "playbackmode": "STREAM", "audioquality": quality, "assetpresentation": "FULL",
        })
        return PlaybackInfo(track_id=track_id, audio_quality=raw.get("audioQuality"),
            manifest_mime_type=raw.get("manifestMimeType"), manifest=raw.get("manifest"),
            bit_depth=raw.get("bitDepth"), sample_rate=raw.get("sampleRate"),
            album_replay_gain=raw.get("albumReplayGain"), album_peak_amplitude=raw.get("albumPeakAmplitude"),
            track_replay_gain=raw.get("trackReplayGain"), track_peak_amplitude=raw.get("trackPeakAmplitude"))

    def get_track(self, track_id: int) -> Track:
        return track_from_dict(self.get_json(f"tracks/{track_id}"))

    def get_album(self, album_id: int) -> Album:
        return album_from_dict(self.get_json(f"albums/{album_id}"))

    def search(self, query: str, limit: int = 50) -> dict[str, Any]:
        return self.get_json("search", {"query": query, "type": "TRACKS,ALBUMS,ARTISTS,VIDEOS,PLAYLISTS", "limit": limit})

    def paginated(self, path: str, *, wrapped: bool = False) -> Iterator[dict[str, Any]]:
        offset = 0
        limit = 100
        while True:
            page = self.get_json(path, {"limit": limit, "offset": offset})
            items = page.get("items", [])
            for item in items:
                value = item.get("item") if wrapped else item
                if isinstance(value, dict):
                    yield value
            if len(items) < limit or (page.get("totalNumRows", 0) and offset + len(items) >= page["totalNumRows"]):
                return
            offset += limit

    def album_tracks(self, album_id: int) -> list[Track]:
        try:
            values = self.paginated(f"albums/{album_id}/tracks")
            return [track_from_dict(value) for value in values]
        except TidalApiError as exc:
            if "404" not in str(exc):
                raise
            return [track_from_dict(value) for value in self.paginated(f"albums/{album_id}/items", wrapped=True)]

    def playlist(self, playlist_id: str) -> dict[str, Any]:
        return self.get_json(f"playlists/{playlist_id}")

    def playlist_tracks(self, playlist_id: str) -> list[Track]:
        return [track_from_dict(value) for value in self.paginated(f"playlists/{playlist_id}/items", wrapped=True)]

    def favorite_tracks(self, user_id: int) -> list[Track]:
        return [track_from_dict(value) for value in self.paginated(f"users/{user_id}/favorites/tracks", wrapped=True)]

    def favorite_albums(self, user_id: int) -> list[Album]:
        return [album_from_dict(value) for value in self.paginated(f"users/{user_id}/favorites/albums", wrapped=True)]

    def favorite_artists(self, user_id: int) -> list[Artist]:
        return [artist_from_dict(value) for value in self.paginated(f"users/{user_id}/favorites/artists", wrapped=True) if artist_from_dict(value)]

    def favorite_videos(self, user_id: int) -> list[dict[str, Any]]:
        return list(self.paginated(f"users/{user_id}/favorites/videos", wrapped=True))

    def artist_albums(self, artist_id: int) -> list[Album]:
        return [album_from_dict(value) for value in self.paginated(f"artists/{artist_id}/albums") if album_from_dict(value)]

    def mix_tracks(self, mix_id: str) -> list[Track]:
        page = self.get_json("pages/mix", {"mixId": mix_id, "deviceType": "BROWSER"})
        categories = page.get("categories", [])
        category = categories[1] if len(categories) > 1 else {}
        values = (category.get("pagedList") or {}).get("items", [])
        tracks = []
        for item in values:
            raw = item.get("item") if isinstance(item, dict) else None
            if isinstance(raw, dict):
                tracks.append(track_from_dict(raw))
        if not tracks:
            raise TidalApiError(f"Mix {mix_id} contains no downloadable tracks")
        return tracks

    def video(self, video_id: int) -> dict[str, Any]:
        return self.get_json(f"videos/{video_id}")

    def user_playlists(self, user_id: int) -> list[dict[str, Any]]:
        return list(self.paginated(f"users/{user_id}/playlists"))

    def video_url(self, video_id: int, quality: str) -> str:
        payload = self.get_json(f"videos/{video_id}/urlpostpaywall", {
            "urlusagemode": "STREAM", "videoquality": quality, "assetpresentation": "FULL",
        })
        urls = payload.get("urls") or []
        if not urls:
            raise TidalApiError(f"No URL returned for video {video_id}")
        return urls[0]


def flexible_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def artist_from_dict(raw: dict[str, Any] | None) -> Artist | None:
    if not raw:
        return None
    return Artist(id=flexible_id(raw.get("id")), name=raw.get("name", ""), role=raw.get("role"), picture=raw.get("picture"))


def album_from_dict(raw: dict[str, Any] | None) -> Album | None:
    if not raw:
        return None
    artists = [artist_from_dict(item) for item in raw.get("artists", [])]
    return Album(id=flexible_id(raw.get("id")), name=raw.get("name", raw.get("title", "")), cover=raw.get("cover"),
        duration=raw.get("duration"), num_tracks=raw.get("numberOfTracks", raw.get("numTracks")),
        num_volumes=raw.get("numberOfVolumes", raw.get("numVolumes")), release_date=raw.get("releaseDate"),
        artist=artist_from_dict(raw.get("artist")), artists=[a for a in artists if a], explicit=raw.get("explicit"), year=raw.get("year"))


def track_from_dict(raw: dict[str, Any]) -> Track:
    artists = [artist_from_dict(item) for item in raw.get("artists", [])]
    return Track(id=flexible_id(raw.get("id")), title=raw.get("title", ""), name=raw.get("name", ""),
        full_name=raw.get("fullName", ""), duration=raw.get("duration"), artist=artist_from_dict(raw.get("artist")),
        artists=[a for a in artists if a], album=album_from_dict(raw.get("album")), audio_quality=raw.get("audioQuality"),
        media_metadata_tags=raw.get("mediaMetadataTags", []), isrc=raw.get("isrc"), copyright=raw.get("copyright"),
        track_num=raw.get("trackNumber", raw.get("trackNum")), volume_num=raw.get("volumeNumber", raw.get("volumeNum")),
        share_url=raw.get("shareUrl"), explicit=raw.get("explicit"), replay_gain=raw.get("replayGain"), peak=raw.get("peak"))
