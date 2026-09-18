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
from tdl.api import TidalApi, TidalApiError
from tdl.crypto import decrypt_file
from tdl.downloader import DownloadError, HiResDownloader, load_downloaded_file_info
from tdl.models import DownloadedFileInfo, PlaybackInfo, Quality, Settings, StreamManifest, Track
from tdl.paths import extension_for, parse_media_url, sanitize_filename
from tdl.stream import parse_bts, parse_m3u8, parse_mpd
from tdl.tui import ACTION_ORDER, QUALITY_LABELS, TuiApp


def _pyside6_importable() -> bool:
    """GUI tests need the PySide6 native libraries, not just the package.

    On headless CI runners the pip install succeeds but importing
    PySide6.QtGui fails on missing system libraries (libEGL.so.1); skip
    instead of failing the whole suite.
    """
    try:
        import PySide6.QtCore  # noqa: F401

        return True
    except Exception:
        return False


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

    def test_downloaded_file_info_formats_size_and_technical_data(self) -> None:
        info = DownloadedFileInfo(path="song.flac", size_bytes=1536, format="FLAC", quality="LOSSLESS", bit_depth=16, sample_rate=44100)
        self.assertEqual(info.size_display, "1.5 KiB")
        self.assertEqual(info.technical_display, "FLAC · LOSSLESS · 16-bit · 44.1 kHz")

    def test_downloaded_file_info_reads_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.flac"
            path.write_bytes(b"audio")
            path.with_name(path.name + ".json").write_text(json.dumps({
                "quality": "LOSSLESS", "bit_depth": 16, "sample_rate": 44100,
                "title": "Song", "artist": "Artist", "album": "Album",
            }), encoding="utf-8")
            info = load_downloaded_file_info(path)
        self.assertEqual(info.title, "Song")
        self.assertEqual(info.artist, "Artist")
        self.assertEqual(info.technical_display, "FLAC · LOSSLESS · 16-bit · 44.1 kHz")

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

    @unittest.skipUnless(_pyside6_importable(), "PySide6 system libraries unavailable")
    def test_gui_imports_context_menu_support(self) -> None:
        from tdl import gui

        self.assertTrue(hasattr(gui, "QMenu"))
        self.assertTrue(hasattr(gui, "QGuiApplication"))

    @unittest.skipUnless(_pyside6_importable(), "PySide6 system libraries unavailable")
    def test_gui_normalises_bulk_urls(self) -> None:
        from tdl.gui import MainWindow

        self.assertEqual(
            MainWindow._normalise_urls(" https://a.test/track/1\ninvalid\nhttps://a.test/track/1\nhttps://a.test/album/2 "),
            ["https://a.test/track/1", "https://a.test/album/2"],
        )

    def test_tui_quality_labels_cover_all_audio_options(self) -> None:
        self.assertEqual(set(QUALITY_LABELS), set(Quality))
        self.assertTrue(all(label for label in QUALITY_LABELS.values()))
        self.assertNotIn("search", ACTION_ORDER)

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

    def test_tui_file_info_formatter_includes_catalog_and_technical_data(self) -> None:
        info = DownloadedFileInfo(path="song.flac", size_bytes=2048, format="FLAC", quality="HI_RES_LOSSLESS", bit_depth=24, sample_rate=96000, title="Song", artist="Artist", album="Album")
        lines = TuiApp._file_info_lines(info, 100)
        self.assertIn("Size: 2.0 KiB", lines)
        self.assertIn("Technical: FLAC · HI_RES_LOSSLESS · 24-bit · 96 kHz", lines)
        self.assertIn("Track: Artist - Song", lines)

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
        self.assertEqual(app.focused_action, len(ACTION_ORDER) - 1)
        app._move_action(1)
        self.assertEqual(app.focused_action, 0)

    def test_missing_collection_track_is_skipped_without_traceback(self) -> None:
        settings = Settings(async_downloads=True, downloads_concurrent_max=2, download_delay=False)
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        missing = Track(id=70449861, title="Removed track")
        error = TidalApiError('API tracks/70449861 returned 404: {"status":404,"subStatus":2001,"userMessage":"Track [70449861] not found"}')
        with patch.object(api, "get_track", side_effect=error):
            with patch.object(downloader, "_download_track_data") as download:
                result = downloader._download_tracks_concurrent([missing], None)
        self.assertEqual(result, [])
        download.assert_not_called()
        self.assertTrue(downloader._is_missing_track_error(error))

    def test_collection_download_hydrates_each_track_before_download(self) -> None:
        settings = Settings(quality_audio=Quality.HIRES, download_delay=False)
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        references = [Track(id=101, title="Reference one"), Track(id=202, title="Reference two")]
        canonical = [Track(id=101, title="Canonical one"), Track(id=202, title="Canonical two")]
        downloaded = [Path("one.flac"), Path("two.flac")]
        with patch.object(api, "get_track", side_effect=canonical) as get_track:
            with patch.object(downloader, "_download_track_data", side_effect=downloaded) as download_track:
                result = downloader._download_tracks(references)
        self.assertEqual(result, downloaded)
        self.assertEqual(get_track.call_args_list[0].args, (101,))
        self.assertEqual(get_track.call_args_list[1].args, (202,))
        self.assertEqual(download_track.call_args_list[0].args, (canonical[0],))
        self.assertEqual(download_track.call_args_list[1].args, (canonical[1],))

    def test_collection_track_data_requests_hires_for_each_track(self) -> None:
        settings = Settings(quality_audio=Quality.HIRES, download_delay=False)
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        tracks = [Track(id=401, title="First"), Track(id=402, title="Second")]
        playback = PlaybackInfo(track_id=0)
        manifest = StreamManifest(urls=["https://cdn.test/segment"], codecs="aac")
        with patch.object(api, "get_track", side_effect=tracks):
            with patch("tdl.downloader.fetch_track_stream", return_value=(manifest, playback)) as fetch_stream:
                with patch.object(downloader, "_absolute_urls", return_value=["https://cdn.test/segment"]):
                    with patch.object(downloader, "_download_segments", side_effect=lambda _urls, path: path.write_bytes(b"audio")):
                        with patch.object(downloader, "_report_saved"):
                            with patch.object(downloader, "_write_track_artifacts"):
                                with tempfile.TemporaryDirectory() as directory:
                                    downloader.settings.download_base_path = directory
                                    downloader._download_tracks(tracks)
        self.assertEqual(fetch_stream.call_count, 2)
        self.assertEqual([call.args[1:] for call in fetch_stream.call_args_list], [(401, Quality.HIRES), (402, Quality.HIRES)])

    def test_hires_download_warns_but_accepts_lower_bit_depth(self) -> None:
        settings = Settings(quality_audio=Quality.HIRES)
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        track = Track(id=501, title="Fallback track")
        playback = PlaybackInfo(track_id=501, audio_quality=Quality.LOSSLESS.api_value, bit_depth=16)
        manifest = StreamManifest(urls=["https://cdn.test/segment"], codecs="flac")
        with tempfile.TemporaryDirectory() as directory:
            downloader.settings.download_base_path = directory
            with patch("tdl.downloader.fetch_track_stream", return_value=(manifest, playback)):
                with patch("tdl.downloader.log.warning") as warning:
                    with patch.object(downloader, "_download_segments", side_effect=lambda _urls, path: path.write_bytes(b"audio")):
                        with patch.object(downloader, "_report_saved"):
                            with patch.object(downloader, "_write_track_artifacts"):
                                result = downloader._download_track_data(track)
            warning.assert_called_once_with(
                "Track %s returned %s-bit audio; downloading the highest available quality",
                501,
                16,
            )
            self.assertTrue(result.name.endswith(".flac"))
            self.assertEqual(result.read_bytes(), b"audio")

    def test_hires_download_accepts_24_bit_lossless_label(self) -> None:
        settings = Settings(quality_audio=Quality.HIRES)
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        track = Track(id=502, title="24-bit track")
        playback = PlaybackInfo(track_id=502, audio_quality=Quality.LOSSLESS.api_value, bit_depth=24, sample_rate=44100)
        manifest = StreamManifest(urls=["https://cdn.test/segment"], codecs="flac")
        with tempfile.TemporaryDirectory() as directory:
            downloader.settings.download_base_path = directory
            with patch("tdl.downloader.fetch_track_stream", return_value=(manifest, playback)):
                with patch.object(downloader, "_download_segments", side_effect=lambda _urls, path: path.write_bytes(b"audio")):
                    with patch.object(downloader, "_report_saved"):
                        with patch.object(downloader, "_write_track_artifacts"):
                            result = downloader._download_track_data(track)
            self.assertTrue(result.name.endswith(".flac"))
            self.assertEqual(result.read_bytes(), b"audio")

    def test_single_track_download_uses_same_hydrated_pipeline(self) -> None:
        settings = Settings()
        api = TidalApi()
        downloader = HiResDownloader(api, settings)
        track = Track(id=303, title="Canonical track")
        expected = Path("track.flac")
        with patch.object(api, "get_track", return_value=track) as get_track:
            with patch.object(downloader, "_download_track_data", return_value=expected) as download_track:
                result = downloader.download_track(303)
        self.assertEqual(result, expected)
        get_track.assert_called_once_with(303)
        download_track.assert_called_once_with(track, playlist_name=None)

    def test_partial_file_is_not_reported_as_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.flac.part"
            path.write_bytes(b"partial")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
