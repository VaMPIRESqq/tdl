"""Filename and destination-path helpers."""

from __future__ import annotations

import re
from pathlib import Path

from .models import Quality, Track

_INVALID = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(value: str) -> str:
    value = _INVALID.sub("_", value).strip(". \t\r\n")
    return (value or "_")[:255]


def parse_media_url(url: str) -> tuple[str, str]:
    parts = url.split("?", 1)[0].rstrip("/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() in {"track", "video", "album", "artist", "playlist", "mix"}:
            return part.lower(), parts[index + 1]
    raise ValueError(f"Could not extract media type and ID from URL: {url}")


def extension_for(quality: Quality, codec: str | None, extract_flac: bool) -> str:
    codec = (codec or "").lower()
    if extract_flac and quality in {Quality.LOSSLESS, Quality.HIRES}:
        return ".flac"
    if quality in {Quality.LOSSLESS, Quality.HIRES} and "flac" in codec:
        return ".flac"
    if "mp3" in codec:
        return ".mp3"
    return ".m4a"


def track_path(track: Track, base: Path, settings: object, playlist_name: str | None = None) -> Path:
    artist = sanitize_filename(track.album.primary_artist if track.album and track.album.primary_artist else track.artist_name or "Unknown Artist")
    album = sanitize_filename(track.album.name if track.album and track.album.name else "Unknown Album")
    title = sanitize_filename(track.display_title or "Unknown Title")
    track_artist = sanitize_filename(track.artist_name or "Unknown Artist")
    number = track.track_num or 0
    total = track.album.num_tracks if track.album else 0
    prefix = ""
    if number:
        prefix = f"{number:0{3 if total and total >= 100 else 2}d}. " if getattr(settings, "track_num_pad_zero", True) else f"{number}. "
    if playlist_name and getattr(settings, "playlist_folder", True):
        return base / "Playlists" / sanitize_filename(playlist_name) / f"{prefix}{track_artist} - {title}"
    return base / artist / album / f"{prefix}{title}"


def existing_file(stem: Path) -> Path | None:
    if stem.exists():
        return stem
    for extension in (".flac", ".m4a", ".mp3", ".mp4", ".ts"):
        path = stem.with_name(stem.name + extension)
        if path.exists():
            return path
    return None


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 100):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path
