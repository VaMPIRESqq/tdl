"""Data models shared by the API, downloader, and Qt frontend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Quality(str, Enum):
    LOW96K = "low96k"
    LOW320K = "low320k"
    LOSSLESS = "high_lossless"
    HIRES = "hi_res_lossless"

    @property
    def api_value(self) -> str:
        return {
            Quality.LOW96K: "LOW",
            Quality.LOW320K: "HIGH",
            Quality.LOSSLESS: "LOSSLESS",
            Quality.HIRES: "HI_RES_LOSSLESS",
        }[self]


class VideoQuality(str, Enum):
    P360 = "p360"
    P480 = "p480"
    P720 = "p720"
    P1080 = "p1080"

    @property
    def api_value(self) -> str:
        return {
            VideoQuality.P360: "LOW",
            VideoQuality.P480: "MEDIUM",
            VideoQuality.P720: "HIGH",
            VideoQuality.P1080: "HIGH",
        }[self]


@dataclass
class Settings:
    skip_existing: bool = True
    lyrics_embed: bool = False
    lyrics_file: bool = False
    video_download: bool = True
    download_delay: bool = True
    download_base_path: str = "~/download"
    quality_audio: Quality = Quality.LOW320K
    quality_video: VideoQuality = VideoQuality.P480
    track_num_pad_zero: bool = True
    playlist_folder: bool = True
    playlist_format: str = "m3u8"
    video_convert_mp4: bool = True
    ffmpeg_path: str = ""
    cover_dimension: str = "320x320"
    metadata_cover_embed: bool = True
    cover_album_file: bool = True
    extract_flac: bool = True
    downloads_simultaneous_per_track_max: int = 20
    download_delay_sec_min: float = 3.0
    download_delay_sec_max: float = 5.0
    downloads_concurrent_max: int = 3
    symlink_to_track: bool = False
    metadata_replay_gain: bool = True
    log_level: str = "info"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Settings:
        data = dict(raw)
        quality = data.get("quality_audio", cls.quality_audio.value)
        video_quality = data.get("quality_video", cls.quality_video.value)
        try:
            data["quality_audio"] = Quality(quality)
        except ValueError:
            data["quality_audio"] = cls.quality_audio
        try:
            data["quality_video"] = VideoQuality(video_quality)
        except ValueError:
            data["quality_video"] = cls.quality_video
        if "path_binary_ffmpeg" in data and "ffmpeg_path" not in data:
            data["ffmpeg_path"] = data.pop("path_binary_ffmpeg")
        if "metadata_cover_dimension" in data and "cover_dimension" not in data:
            dimension = str(data.pop("metadata_cover_dimension")).replace("px", "")
            data["cover_dimension"] = f"{dimension}x{dimension}"
        # The old JSON used `p360` style values for video quality and retained
        # several optional controls that were absent from the first Python UI.
        if "playlist_format" not in data and "playlistFormat" in data:
            data["playlist_format"] = data.pop("playlistFormat")
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["quality_audio"] = self.quality_audio.value
        data["quality_video"] = self.quality_video.value
        data["path_binary_ffmpeg"] = data.pop("ffmpeg_path")
        dimension = self.cover_dimension.split("x", 1)[0]
        data["metadata_cover_dimension"] = f"px{dimension}"
        return data


@dataclass
class Token:
    token_type: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expiry_time: float = 0.0
    session_id: str | None = None
    country_code: str | None = None
    user_id: int | None = None
    is_pkce: bool = False

    @property
    def valid(self) -> bool:
        import time

        return bool(self.access_token) and self.expiry_time > time.time()

    @property
    def restorable(self) -> bool:
        return bool(self.refresh_token)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Token:
        return cls(**{key: value for key, value in raw.items() if key in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Artist:
    id: int = 0
    name: str = ""
    role: str | None = None
    picture: str | None = None


@dataclass
class Album:
    id: int = 0
    name: str = ""
    cover: str | None = None
    duration: int | None = None
    num_tracks: int | None = None
    num_volumes: int | None = None
    release_date: str | None = None
    artist: Artist | None = None
    artists: list[Artist] = field(default_factory=list)
    explicit: bool | None = None
    year: int | None = None

    @property
    def primary_artist(self) -> str:
        for artist in self.artists:
            if artist.role == "MAIN":
                return artist.name
        return (self.artist.name.split(",", 1)[0].strip() if self.artist else "")

    @property
    def album_artist(self) -> str:
        main = [artist.name for artist in self.artists if artist.role == "MAIN"]
        return ", ".join(main) or (self.artist.name if self.artist else "")

    @property
    def year_string(self) -> str | None:
        if self.release_date:
            return self.release_date[:4]
        return str(self.year) if self.year else None

    def image_url(self, dimension: str = "320x320") -> str | None:
        if not self.cover:
            return None
        return f"https://resources.tidal.com/images/{self.cover.replace('-', '/')}/{dimension}.jpg"


@dataclass
class Track:
    id: int = 0
    title: str = ""
    name: str = ""
    full_name: str = ""
    duration: int | None = None
    artist: Artist | None = None
    artists: list[Artist] = field(default_factory=list)
    album: Album | None = None
    audio_quality: str | None = None
    media_metadata_tags: list[str] = field(default_factory=list)
    isrc: str | None = None
    copyright: str | None = None
    track_num: int | None = None
    volume_num: int | None = None
    share_url: str | None = None
    explicit: bool | None = None
    replay_gain: float | None = None
    peak: float | None = None

    @property
    def display_title(self) -> str:
        return self.full_name or self.title or self.name

    @property
    def artist_name(self) -> str:
        return ", ".join(a.name for a in self.artists) or (self.artist.name if self.artist else "")


@dataclass
class StreamManifest:
    urls: list[str]
    codecs: str | None = None
    mime_type: str | None = None
    encrypted: bool = False
    encryption_key: str | None = None
    sample_rate: int | None = None
    file_extension: str | None = None
    is_mpd: bool = False


@dataclass
class PlaybackInfo:
    track_id: int
    audio_quality: str | None = None
    manifest_mime_type: str | None = None
    manifest: str | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    album_replay_gain: float | None = None
    album_peak_amplitude: float | None = None
    track_replay_gain: float | None = None
    track_peak_amplitude: float | None = None
