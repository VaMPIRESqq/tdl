"""Download orchestration for tracks, albums, and playlists."""

from __future__ import annotations

import json
import logging
import random
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter

from .api import TidalApi, TidalApiError
from .crypto import decrypt_file, decrypt_security_token
from .models import DownloadedFileInfo, PlaybackInfo, Quality, Settings, Track
from .paths import existing_file, extension_for, parse_media_url, sanitize_filename, track_path, unique_path
from .stream import fetch_track_stream, fetch_video_segments

log = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str], None]


class DownloadError(RuntimeError):
    """Raised when a media item cannot be downloaded."""


class HiResDownloader:
    def __init__(self, api: TidalApi, settings: Settings, progress: ProgressCallback | None = None) -> None:
        self.api = api
        self.settings = settings
        self.progress = progress or (lambda _current, _total, _message: None)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": self.api.session.headers["User-Agent"]})
        pool_size = max(1, min(settings.downloads_simultaneous_per_track_max, 64))
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, pool_block=True)
        self.http.mount("http://", adapter)
        self.http.mount("https://", adapter)

    def download_url(self, url: str) -> list[Path]:
        media_type, media_id = parse_media_url(url)
        if media_type == "track":
            return [self.download_track(int(media_id))]
        if media_type == "video":
            return [self.download_video(int(media_id))]
        if media_type == "album":
            return self._download_tracks(self.api.album_tracks(int(media_id)))
        if media_type == "playlist":
            playlist = self.api.playlist(media_id)
            name = playlist.get("name") or playlist.get("title") or media_id
            return self._download_tracks(self.api.playlist_tracks(media_id), playlist_name=name)
        if media_type == "mix":
            return self._download_tracks(self.api.mix_tracks(media_id), playlist_name=media_id)
        if media_type == "artist":
            return self._download_tracks(
                [track for album in self.api.artist_albums(int(media_id)) for track in self.api.album_tracks(album.id)]
            )
        raise DownloadError(f"Unsupported download type: {media_type}")

    def download_video(self, video_id: int) -> Path:
        raw = self.api.video(video_id)
        title = raw.get("title") or raw.get("name") or str(video_id)
        artist = (raw.get("artist") or {}).get("name", "Unknown Artist")
        destination = unique_path(Path(self.settings.download_base_path).expanduser() / sanitize_filename(artist) / f"{sanitize_filename(title)}.mp4")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.part")
        final_path = destination
        try:
            urls = fetch_video_segments(self.api, video_id, self.settings.quality_video.api_value)
            self._download_segments(urls, partial)
            if self.settings.video_convert_mp4 and self._ffmpeg_available():
                self._convert_video(partial, destination)
                partial.unlink(missing_ok=True)
            else:
                final_path = destination.with_suffix(".ts")
                partial.replace(final_path)
            self._report_saved(final_path)
            return final_path
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def _convert_video(self, source: Path, destination: Path) -> None:
        executable = self.settings.ffmpeg_path or "ffmpeg"
        result = subprocess.run([executable, "-y", "-i", str(source), "-codec", "copy", "-loglevel", "error", str(destination)], capture_output=True, text=True)
        if result.returncode:
            raise DownloadError(f"ffmpeg video conversion failed: {result.stderr.strip()}")

    def _download_tracks(self, tracks: list[Track], playlist_name: str | None = None) -> list[Path]:
        """Download a collection through the same per-track path as a track URL.

        The objects returned by album/playlist/artist endpoints are collection
        references and can contain only partial track metadata. In particular,
        they must not be used to request playback info in bulk: the Hi-Res
        entitlement is resolved by the individual track playback endpoint.
        Hydrate every reference with ``GET /tracks/{id}``, then download it
        sequentially. This deliberately keeps one playback-info request and
        one stream download per song.
        """
        completed: list[Path] = []
        total = len(tracks)
        if self.settings.async_downloads and self.settings.downloads_concurrent_max > 1:
            return self._download_tracks_concurrent(tracks, playlist_name)
        for index, track_reference in enumerate(tracks, 1):
            title = track_reference.display_title or str(track_reference.id)
            self.progress(index - 1, total, f"{track_reference.artist_name} - {title}")
            try:
                if not track_reference.id:
                    raise DownloadError("Collection item has no track ID")
                # Do not pass the collection response further down. Fetch the
                # canonical track resource first so Hi-Res playback is always
                # negotiated as if this were a standalone track URL.
                track = self.api.get_track(track_reference.id)
                completed.append(self._download_track_data(track, playlist_name=playlist_name))
            except Exception as exc:  # continue collection downloads and report the failed track
                log.exception("Track %s failed", track_reference.id)
                self.progress(index, total, f"Failed: {title}: {exc}")
            if self.settings.download_delay and index < total:
                delay = random.uniform(self.settings.download_delay_sec_min, self.settings.download_delay_sec_max)
                time.sleep(max(0.0, delay))
        self._write_playlist(completed, playlist_name)
        self.progress(total, total, f"Completed {len(completed)}/{total}")
        return completed

    def _download_tracks_concurrent(self, tracks: list[Track], playlist_name: str | None) -> list[Path]:
        """Download collection tracks concurrently only when explicitly enabled."""
        completed: list[Path] = []
        total = len(tracks)
        def download(reference: Track) -> Path:
            try:
                track = self.api.get_track(reference.id)
            except TidalApiError as exc:
                if self._is_missing_track_error(exc):
                    raise DownloadError(f"Track {reference.id} is unavailable or was removed; skipped") from exc
                raise
            return self._download_track_data(track, playlist_name=playlist_name)
        with ThreadPoolExecutor(max_workers=max(1, min(self.settings.downloads_concurrent_max, 32))) as pool:
            futures = {pool.submit(download, reference): reference for reference in tracks}
            for index, future in enumerate(as_completed(futures), 1):
                reference = futures[future]
                try:
                    completed.append(future.result())
                    self.progress(index, total, f"Completed {index}/{total}: {reference.display_title}")
                except Exception as exc:
                    if self._is_missing_track_error(exc):
                        log.warning("Track %s is unavailable or was removed; skipping", reference.id)
                    else:
                        log.error("Track %s failed: %s", reference.id, exc)
                    self.progress(index, total, f"Skipped: {reference.display_title}: {exc}")
        self._write_playlist(completed, playlist_name)
        self.progress(total, total, f"Completed {len(completed)}/{total}")
        return completed

    @staticmethod
    def _is_missing_track_error(error: BaseException) -> bool:
        """Return true for TIDAL's permanent missing-track response."""
        text = str(error).lower()
        return "returned 404" in text and ("track" in text or '"substatus":2001' in text)

    def download_track(self, track_id: int, playlist_name: str | None = None) -> Path:
        """Fetch and download one canonical track resource directly."""
        return self._download_track_data(self.api.get_track(track_id), playlist_name=playlist_name)

    def _download_track_data(self, track: Track, playlist_name: str | None = None) -> Path:
        """Download one already-hydrated track using its own playback request."""
        if track.album and not track.album.artists:
            try:
                track.album = self.api.get_album(track.album.id)
            except Exception:
                log.debug("Could not enrich album %s", track.album.id, exc_info=True)
        base = Path(self.settings.download_base_path).expanduser()
        stem = track_path(track, base, self.settings, playlist_name)
        if self.settings.skip_existing:
            found = existing_file(stem)
            if found:
                log.info("Skipped existing song: %s", found)
                print(f"Skipped existing: {found}", flush=True)
                self.progress(0, 1, f"Saved: {found} (already existed)")
                return found

        manifest, playback = fetch_track_stream(self.api, track.id, self.settings.quality_audio)
        if self.settings.quality_audio is Quality.HIRES and playback.bit_depth is not None and playback.bit_depth < 24:
            log.warning(
                "Track %s returned %s-bit audio; downloading the highest available quality",
                track.id,
                playback.bit_depth,
            )
        urls = self._absolute_urls(manifest.urls)
        if not urls:
            raise DownloadError(f"Track {track.id} has no stream segments")
        extension = extension_for(self.settings.quality_audio, manifest.codecs, self.settings.extract_flac)
        destination = unique_path(stem.with_name(stem.name + extension))
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.part")
        try:
            self._download_segments(urls, partial)
            if manifest.encrypted:
                if not manifest.encryption_key:
                    raise DownloadError("Encrypted manifest has no security token")
                key, nonce = decrypt_security_token(manifest.encryption_key)
                partial.write_bytes(decrypt_file(partial.read_bytes(), key, nonce))
            if self.settings.extract_flac and extension == ".flac" and manifest.codecs and "flac" in manifest.codecs.lower():
                if playback.manifest_mime_type and "dash" in playback.manifest_mime_type.lower() and self._ffmpeg_available():
                    extracted = destination.with_suffix(".flac")
                    self._extract_flac(partial, extracted)
                    partial.unlink(missing_ok=True)
                    partial = extracted
            partial.replace(destination)
            # The media file is complete at this point. Report it before
            # optional metadata/artwork work so those failures cannot hide a
            # successfully downloaded song.
            self._report_saved(destination)
            self._write_track_artifacts(destination, track, playback)
            return destination
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    @staticmethod
    def _effective_quality(playback: PlaybackInfo) -> str | None:
        """Prefer the stream bit depth over TIDAL's sometimes stale label."""
        if playback.bit_depth is not None and playback.bit_depth >= 24:
            return Quality.HIRES.api_value
        return playback.audio_quality

    def file_info(
        self,
        path: Path,
        *,
        playback: PlaybackInfo | None = None,
        track: Track | None = None,
        collection: str | None = None,
    ) -> DownloadedFileInfo:
        """Build displayable information without failing a completed download."""
        info = DownloadedFileInfo(
            path=str(path),
            size_bytes=path.stat().st_size if path.exists() else 0,
            format=path.suffix.removeprefix(".").upper() or None,
            quality=self._effective_quality(playback) if playback else None,
            bit_depth=playback.bit_depth if playback else None,
            sample_rate=playback.sample_rate if playback else None,
            title=track.display_title if track else None,
            artist=track.artist_name if track else None,
            album=track.album.name if track and track.album else None,
            collection=collection,
        )
        return info

    def _report_saved(self, path: Path) -> None:
        log.info("Saved song: %s", path)
        self.progress(1, 1, f"Saved: {path}")

    def _absolute_urls(self, urls: list[str]) -> list[str]:
        return [urljoin("https://api.tidal.com/", url) if not url.startswith(("http://", "https://")) else url for url in urls]

    def _download_segments(self, urls: list[str], output: Path) -> None:
        temp_dir = Path(tempfile.mkdtemp(prefix="tdl-segments-"))
        try:
            workers = max(1, min(self.settings.downloads_simultaneous_per_track_max, len(urls)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._fetch_segment_to_file, url, temp_dir / f"segment_{index:08d}"): index for index, url in enumerate(urls)}
                for complete, future in enumerate(as_completed(futures), 1):
                    future.result()
                    self.progress(complete, len(urls), f"Downloading segments {complete}/{len(urls)}")
            with output.open("wb") as handle:
                for index in range(len(urls)):
                    handle.write((temp_dir / f"segment_{index:08d}").read_bytes())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _fetch_segment_to_file(self, url: str, path: Path) -> None:
        path.write_bytes(self._fetch_segment(url))

    def _fetch_segment(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                response = self.http.get(url, timeout=45)
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(2**attempt)
        raise DownloadError(f"Segment request failed: {url}: {last_error}")

    def _ffmpeg_available(self) -> bool:
        executable = self.settings.ffmpeg_path or "ffmpeg"
        return shutil.which(executable) is not None or Path(executable).exists()

    def _extract_flac(self, source: Path, destination: Path) -> None:
        executable = self.settings.ffmpeg_path or "ffmpeg"
        result = subprocess.run([executable, "-y", "-i", str(source), "-map", "0", "-codec", "copy", "-loglevel", "error", str(destination)], capture_output=True, text=True)
        if result.returncode:
            raise DownloadError(f"ffmpeg FLAC extraction failed: {result.stderr.strip()}")

    def _write_track_artifacts(self, path: Path, track: Track, playback: PlaybackInfo) -> None:
        cover = self._fetch_cover(track)
        lyrics = self._fetch_lyrics(track.id)
        if cover and self.settings.cover_album_file and track.album:
            (path.parent / "cover.jpg").write_bytes(cover)
        if lyrics and self.settings.lyrics_file:
            path.with_suffix(".lrc").write_text(lyrics, encoding="utf-8")
        if self._ffmpeg_available():
            self._tag_with_ffmpeg(path, track, playback, cover if self.settings.metadata_cover_embed else None, lyrics if self.settings.lyrics_embed else None)
        else:
            self._write_sidecar_metadata(path, track, playback)

    def _fetch_cover(self, track: Track) -> bytes | None:
        if not track.album or not track.album.cover:
            return None
        url = track.album.image_url(self.settings.cover_dimension)
        try:
            response = self.api.raw(url, authenticated=True)
            return response.content if response.ok else None
        except Exception:
            log.debug("Could not download cover", exc_info=True)
            return None

    def _fetch_lyrics(self, track_id: int) -> str | None:
        try:
            raw = self.api.get_json(f"tracks/{track_id}/lyrics")
        except Exception:
            return None
        if isinstance(raw.get("subtitles"), str):
            return raw["subtitles"] or None
        if isinstance(raw.get("subtitles"), list):
            lines = []
            for item in raw["subtitles"]:
                text = item.get("lrc") or item.get("line") or ""
                if "lrc" in item:
                    lines.append(text)
                elif text:
                    milliseconds = int(item.get("time", 0))
                    lines.append(f"[{milliseconds // 60000:02d}:{(milliseconds % 60000) / 1000:05.2f}]{text}")
            return "\n".join(lines) + ("\n" if lines else "")
        return raw.get("text") or None

    def _tag_with_ffmpeg(self, path: Path, track: Track, playback: PlaybackInfo, cover: bytes | None, lyrics: str | None) -> None:
        executable = self.settings.ffmpeg_path or "ffmpeg"
        # Keep FFmpeg's output on the destination filesystem. Using the
        # default /tmp directory breaks os.replace() for removable drives.
        with tempfile.TemporaryDirectory(prefix=".tdl-tags-", dir=path.parent) as directory:
            cover_path = Path(directory) / "cover.jpg"
            if cover:
                cover_path.write_bytes(cover)
            tagged = Path(directory) / path.name
            command = [executable, "-y", "-i", str(path)]
            if cover:
                command += ["-i", str(cover_path), "-map", "0:a", "-map", "1:v", "-disposition:v", "attached_pic"]
            else:
                command += ["-map", "0"]
            metadata = {
                "title": track.display_title, "artist": track.artist_name,
                "album": track.album.name if track.album else "",
                "album_artist": track.album.album_artist if track.album else track.artist_name,
                "track": str(track.track_num or ""), "disc": str(track.volume_num or ""),
                "date": track.album.year_string if track.album else "", "copyright": track.copyright or "",
                "isrc": track.isrc or "", "comment": self._effective_quality(playback) or "",
                "replaygain_album_gain": f"{playback.album_replay_gain:.2f} dB" if self.settings.metadata_replay_gain and playback.album_replay_gain is not None else "",
                "replaygain_track_gain": f"{playback.track_replay_gain:.2f} dB" if self.settings.metadata_replay_gain and playback.track_replay_gain is not None else "",
                "replaygain_album_peak": f"{playback.album_peak_amplitude:.6f}" if self.settings.metadata_replay_gain and playback.album_peak_amplitude is not None else "",
                "replaygain_track_peak": f"{playback.track_peak_amplitude:.6f}" if self.settings.metadata_replay_gain and playback.track_peak_amplitude is not None else "",
            }
            if lyrics:
                metadata["lyrics"] = lyrics
            for key, value in metadata.items():
                if value:
                    command += ["-metadata", f"{key}={value}"]
            codec = "copy" if path.suffix.lower() in {".m4a", ".mp4"} else "copy"
            command += ["-codec", codec, "-loglevel", "error", str(tagged)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                log.warning("FFmpeg metadata tagging failed: %s", result.stderr.strip())
                self._write_sidecar_metadata(path, track, playback)
                return
            tagged.replace(path)


    def _write_playlist(self, paths: list[Path], playlist_name: str | None) -> None:
        if not playlist_name or not self.settings.playlist_folder or not paths:
            return
        directory = paths[0].parent
        extension = self.settings.playlist_format if self.settings.playlist_format in {"m3u", "m3u8"} else "m3u8"
        playlist = directory / f"{sanitize_filename(playlist_name)}.{extension}"
        playlist.write_text("#EXTM3U\n" + "\n".join(path.name for path in sorted(paths)) + "\n", encoding="utf-8")

    @staticmethod
    def _write_sidecar_metadata(path: Path, track: Track, playback: PlaybackInfo) -> None:
        sidecar = path.with_name(path.name + ".json")
        sidecar.write_text(json.dumps({"track_id": track.id, "title": track.display_title,
            "artist": track.artist_name, "album": track.album.name if track.album else None,
            "quality": HiResDownloader._effective_quality(playback), "bit_depth": playback.bit_depth,
            "sample_rate": playback.sample_rate}, indent=2) + "\n", encoding="utf-8")


def _ffprobe_path(ffmpeg_path: str = "") -> str | None:
    executable = Path(ffmpeg_path).with_name("ffprobe") if ffmpeg_path else Path("ffprobe")
    if executable.is_absolute() and executable.exists():
        return str(executable)
    return str(executable) if shutil.which(str(executable)) else None


def _probe_file(path: Path, ffmpeg_path: str = "") -> dict[str, object]:
    executable = _ffprobe_path(ffmpeg_path)
    if not executable:
        return {}
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            return {}
        raw = json.loads(result.stdout)
        if not isinstance(raw, dict):
            return {}
        format_data = raw.get("format") if isinstance(raw.get("format"), dict) else {}
        streams = raw.get("streams") if isinstance(raw.get("streams"), list) else []
        audio = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), {})
        tags = format_data.get("tags") if isinstance(format_data.get("tags"), dict) else {}
        return {
            "format": format_data.get("format_name"),
            "quality": tags.get("comment") or tags.get("quality"),
            "bit_depth": audio.get("bits_per_raw_sample") or audio.get("bits_per_sample"),
            "sample_rate": audio.get("sample_rate"),
            "title": tags.get("title"),
            "artist": tags.get("artist"),
            "album": tags.get("album"),
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        log.debug("Could not probe media file: %s", path, exc_info=True)
        return {}


def load_downloaded_file_info(path: Path, ffmpeg_path: str = "") -> DownloadedFileInfo:
    """Read filesystem, sidecar, or ffprobe data for an existing download."""
    data: dict[str, object] = {}
    sidecar = path.with_name(path.name + ".json")
    if sidecar.is_file():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            log.debug("Could not read metadata sidecar: %s", sidecar, exc_info=True)
    if not data:
        data = _probe_file(path, ffmpeg_path)
    bit_depth = data.get("bit_depth")
    sample_rate = data.get("sample_rate")
    return DownloadedFileInfo(
        path=str(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        format=str(data.get("format") or path.suffix.removeprefix(".").upper() or "") or None,
        quality=str(data.get("quality")) if data.get("quality") else None,
        bit_depth=int(bit_depth) if isinstance(bit_depth, (int, float, str)) and str(bit_depth).isdigit() else None,
        sample_rate=int(float(sample_rate)) if isinstance(sample_rate, (int, float, str)) and str(sample_rate).replace(".", "", 1).isdigit() else None,
        title=str(data["title"]) if data.get("title") else None,
        artist=str(data["artist"]) if data.get("artist") else None,
        album=str(data["album"]) if data.get("album") else None,
    )


def find_downloaded_files(root: Path) -> list[Path]:
    """Return media files in album, playlist, and discography folders."""
    if not root.is_dir():
        return []
    extensions = {".flac", ".m4a", ".mp3", ".mp4", ".ts", ".wav", ".ogg", ".opus"}
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in extensions and not path.name.startswith(".")),
        key=lambda path: str(path).casefold(),
    )

