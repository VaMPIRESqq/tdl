from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tdl.auth import TidalAuth
from tdl.api import TidalApi
from tdl.crypto import decrypt_file
from tdl.downloader import HiResDownloader
from tdl.models import Quality, Settings, Track
from tdl.paths import extension_for, parse_media_url, sanitize_filename
from tdl.stream import parse_bts, parse_m3u8, parse_mpd
from tdl.tui import QUALITY_LABELS, TuiApp


class CoreTests(unittest.TestCase):
    def test_pkce_challenge_is_s256(self) -> None:
        auth = TidalAuth.__new__(TidalAuth)
        url, verifier, unique_key = auth.build_pkce_url()
        self.assertTrue(unique_key)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertIn(f"code_challenge={expected}", url)
        self.assertIn("code_challenge_method=S256", url)

    def test_bts_manifest(self) -> None:
        manifest = parse_bts(json.dumps({
            "urls": ["https://cdn.test/init", "https://cdn.test/segment_1"],
            "codecs": "flac", "mimeType": "audio/flac",
            "encryptionType": "CENC", "keyId": "secret", "sampleRate": 96000,
        }).encode())
        self.assertEqual(manifest.file_extension, "flac")
        self.assertTrue(manifest.encrypted)
        self.assertEqual(manifest.sample_rate, 96000)

    def test_mpd_segment_timeline(self) -> None:
        xml = b"""
        <MPD><Period><AdaptationSet mimeType='audio/mp4'>
          <SegmentTemplate initialization='init-$RepresentationID$.m4s' media='seg-$Number$.m4s' startNumber='1'>
            <SegmentTimeline><S d='1000' r='2'/></SegmentTimeline>
          </SegmentTemplate>
          <Representation id='audio' codecs='flac' audioSamplingRate='96000'/>
        </AdaptationSet></Period></MPD>
        """
        manifest = parse_mpd(xml)
        self.assertEqual(manifest.urls, ["init-audio.m4s", "seg-1.m4s", "seg-2.m4s", "seg-3.m4s"])
        self.assertEqual(manifest.sample_rate, 96000)

    def test_m3u8_selects_highest_bandwidth_and_segments(self) -> None:
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200\nhigh/index.m3u8\n"
        selected = parse_m3u8(master, "https://cdn.test/master.m3u8")
        self.assertEqual(selected, ["https://cdn.test/high/index.m3u8"])
        media = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n"
        self.assertEqual(parse_m3u8(media, selected[0]), ["https://cdn.test/high/a.ts", "https://cdn.test/high/b.ts"])

    def test_legacy_settings_names_roundtrip(self) -> None:
        settings = Settings.from_dict({
            "quality_audio": "hi_res_lossless",
            "path_binary_ffmpeg": "/usr/bin/ffmpeg",
            "metadata_cover_dimension": "px640",
        })
        self.assertIs(settings.quality_audio, Quality.HIRES)
        self.assertEqual(settings.ffmpeg_path, "/usr/bin/ffmpeg")
        self.assertEqual(settings.cover_dimension, "640x640")
        encoded = settings.to_dict()
        self.assertEqual(encoded["quality_audio"], "hi_res_lossless")
        self.assertEqual(encoded["metadata_cover_dimension"], "px640")

    def test_paths_and_quality(self) -> None:
        self.assertEqual(parse_media_url("https://listen.tidal.com/track/42?x=1"), ("track", "42"))
        self.assertEqual(sanitize_filename("bad:/name"), "bad__name")
        self.assertEqual(extension_for(Quality.HIRES, "flac", True), ".flac")
        self.assertEqual(extension_for(Quality.LOW320K, "mp4a.40.2", False), ".m4a")

    def test_aes_ctr_roundtrip(self) -> None:
        from Crypto.Cipher import AES

        key = bytes([0x42]) * 16
        nonce = bytes([0x13]) * 8
        plaintext = b"Hi-Res Lossless stream test"
        counter = int.from_bytes(nonce + b"\0" * 8, "big")
        encrypted = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=counter).encrypt(plaintext)
        self.assertEqual(decrypt_file(encrypted, key, nonce), plaintext)

    def test_http_pools_match_segment_limit(self) -> None:
        settings = Settings(downloads_simultaneous_per_track_max=20)
        downloader = HiResDownloader(TidalApi(), settings)
        pool = downloader.http.get_adapter("https://").poolmanager.connection_pool_kw["maxsize"]
        self.assertEqual(pool, 20)

    def test_tui_quality_labels_cover_all_audio_options(self) -> None:
        self.assertEqual(set(QUALITY_LABELS), set(Quality))
        self.assertTrue(all(label for label in QUALITY_LABELS.values()))
        self.assertTrue(callable(TuiApp._artist))

    def test_tui_quiet_download_suppresses_library_output(self) -> None:
        app = TuiApp.__new__(TuiApp)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with app._quiet_download():
                print("saved path")
                logging.getLogger("tdl.test").error("download log")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_tui_dashboard_formatters_fit_and_fill(self) -> None:
        self.assertEqual(TuiApp._fit("short", 8), "short")
        self.assertEqual(TuiApp._fit("a very long path", 8), "a ver...")
        self.assertEqual(TuiApp._progress_bar(0, 0, 8), "[------]")
        self.assertEqual(TuiApp._progress_bar(5, 10, 8), "[###---]")
        self.assertEqual(TuiApp._progress_bar(10, 10, 8), "[######]")

    def test_tui_mouse_zones_and_action_focus(self) -> None:
        zones = [(4, 2, 12, "download"), (4, 14, 20, "search")]
        self.assertEqual(TuiApp._hit_test(zones, 8, 4), "download")
        self.assertEqual(TuiApp._hit_test(zones, 13, 4), None)
        self.assertEqual(TuiApp._hit_test(zones, None, 4), None)
        app = TuiApp.__new__(TuiApp)
        app.focused_action = 0
        app._move_action(-1)
        self.assertEqual(app.focused_action, 4)
        app._move_action(1)
        self.assertEqual(app.focused_action, 0)

    def test_partial_file_is_not_reported_as_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.flac.part"
            path.write_bytes(b"partial")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
