"""Playback manifest decoding for TIDAL audio streams."""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests

from .api import TidalApi
from .models import PlaybackInfo, Quality, StreamManifest


def _extension(codecs: str | None) -> str | None:
    value = (codecs or "").lower()
    if "flac" in value:
        return "flac"
    if "mp4a" in value or "aac" in value or "eac3" in value or "ac4" in value:
        return "m4a"
    if "mp3" in value:
        return "mp3"
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attrs(element: ET.Element) -> dict[str, str]:
    return {_local(key): value for key, value in element.attrib.items()}


def parse_bts(data: bytes) -> StreamManifest:
    raw = json.loads(data)
    codecs = raw.get("codecs")
    encryption_type = raw.get("encryptionType")
    return StreamManifest(
        urls=list(raw.get("urls") or []), codecs=codecs, mime_type=raw.get("mimeType"),
        encrypted=bool(encryption_type and encryption_type != "NONE"),
        encryption_key=raw.get("keyId"), sample_rate=raw.get("sampleRate"),
        file_extension=_extension(codecs), is_mpd=False,
    )


def _expand(template: str, representation: str, number: int) -> str:
    value = template.replace("$RepresentationID$", representation).replace("$Number$", str(number))
    # TIDAL occasionally emits a printf-like width in DASH templates.
    match = re.search(r"\$Number%0(\d+)d\$", value)
    if match:
        value = value.replace(match.group(0), str(number).zfill(int(match.group(1))))
    return value


def _timeline(parent: ET.Element) -> list[tuple[int, int]]:
    timeline = next((child for child in parent if _local(child.tag) == "SegmentTimeline"), None)
    if timeline is None:
        return []
    result: list[tuple[int, int]] = []
    for segment in timeline:
        if _local(segment.tag) == "S":
            attrs = _attrs(segment)
            result.append((int(attrs.get("d", "0")), int(attrs.get("r", "0"))))
    return result


def parse_mpd(data: bytes) -> StreamManifest:
    root = ET.fromstring(data)
    urls: list[str] = []
    codecs = None
    period_base = next((child.text.strip() for child in root if _local(child.tag) == "BaseURL" and child.text), None)
    mime_type = None
    sample_rate = None

    for adaptation in root.iter():
        if _local(adaptation.tag) != "AdaptationSet":
            continue
        adaptation_attrs = _attrs(adaptation)
        adapt_template = next((child for child in adaptation if _local(child.tag) == "SegmentTemplate"), None)
        adapt_template_attrs = _attrs(adapt_template) if adapt_template is not None else {}
        adapt_timeline = _timeline(adapt_template) if adapt_template is not None else []
        representation = next((child for child in adaptation if _local(child.tag) == "Representation"), None)
        if representation is None:
            continue
        rep_attrs = _attrs(representation)
        representation_id = rep_attrs.get("id", "")
        codecs = rep_attrs.get("codecs", adaptation_attrs.get("codecs"))
        mime_type = rep_attrs.get("mimeType", adaptation_attrs.get("mimeType"))
        sample_rate_raw = rep_attrs.get("audioSamplingRate", adaptation_attrs.get("audioSamplingRate"))
        sample_rate = int(sample_rate_raw) if sample_rate_raw and sample_rate_raw.isdigit() else None

        template = next((child for child in representation if _local(child.tag) == "SegmentTemplate"), None)
        template_attrs = dict(adapt_template_attrs)
        if template is not None:
            template_attrs.update(_attrs(template))
        timeline = _timeline(template) if template is not None else adapt_timeline
        start_number = int(template_attrs.get("startNumber", "1"))
        initialization = template_attrs.get("initialization")
        media = template_attrs.get("media")
        if media:
            if initialization:
                urls.append(_expand(initialization, representation_id, start_number))
            segment_number = start_number
            if timeline:
                for _duration, repeat in timeline:
                    for _ in range(repeat + 1):
                        urls.append(_expand(media, representation_id, segment_number))
                        segment_number += 1
            else:
                # Live-like manifests without a timeline are bounded by the API's
                # finite audio resource; stop at a conservative maximum.
                urls.extend(_expand(media, representation_id, number) for number in range(start_number, start_number + 200))
        else:
            base = next((child.text.strip() for child in representation if _local(child.tag) == "BaseURL" and child.text), None)
            segment_list = next((child for child in representation if _local(child.tag) == "SegmentList"), None)
            if base:
                urls.append(base)
            if segment_list is not None:
                urls.extend(_attrs(child).get("media", "") for child in segment_list if _local(child.tag) == "SegmentURL")
        if urls:
            break

    if not urls:
        raise ValueError("MPD manifest contained no downloadable URLs")
    if period_base:
        urls = [urljoin(period_base, url) for url in urls]
    return StreamManifest(urls=urls, codecs=codecs, mime_type=mime_type, sample_rate=sample_rate,
                          file_extension=_extension(codecs), is_mpd=True)


def fetch_track_stream(api: TidalApi, track_id: int, quality: Quality) -> tuple[StreamManifest, PlaybackInfo]:
    playback = api.playback_info(track_id, quality.api_value)
    if not playback.manifest:
        raise ValueError(f"No manifest in playback info for track {track_id}")
    try:
        data = base64.b64decode(playback.manifest)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Failed to base64-decode manifest") from exc
    mime = (playback.manifest_mime_type or "").lower()
    manifest = parse_mpd(data) if "dash" in mime or "mpd" in mime else parse_bts(data)
    return manifest, playback


def parse_m3u8(content: str, base_url: str) -> list[str]:
    """Return media segment URLs, selecting the highest-bandwidth variant."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        variants: list[tuple[int, str]] = []
        bandwidth = 0
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF"):
                bandwidth = int(re.search(r"BANDWIDTH=(\d+)", line).group(1)) if re.search(r"BANDWIDTH=(\d+)", line) else 0
            elif not line.startswith("#"):
                variants.append((bandwidth, urljoin(base_url, line)))
        if not variants:
            raise ValueError("Master playlist contains no variants")
        return [max(variants)[1]]
    segments = [urljoin(base_url, line) for line in lines if not line.startswith("#")]
    if not segments:
        raise ValueError("Media playlist contains no segments")
    return segments


def fetch_video_segments(api: TidalApi, video_id: int, quality: str) -> list[str]:
    response = api.video_url(video_id, quality)
    playlist = api.raw(response, authenticated=False)
    playlist.raise_for_status()
    selected = parse_m3u8(playlist.text, response)
    if len(selected) == 1 and selected[0].endswith((".m3u8", ".m3u")):
        media = api.raw(selected[0], authenticated=False)
        media.raise_for_status()
        return parse_m3u8(media.text, selected[0])
    return selected


def resolve_urls(manifest: StreamManifest, base_url: str | None = None) -> list[str]:
    if not base_url:
        return manifest.urls
    return [urljoin(base_url, url) for url in manifest.urls]
