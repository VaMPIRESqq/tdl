<div align="center">

# tdl

> TIDAL downloader with Hi-Res Lossless support, PKCE authentication, and a Qt6 desktop GUI

</div>

> **WARNING: Unauthorized distribution of copyrighted music is illegal.**
> This tool is for personal use only. Downloaded content must not be shared, redistributed, or made publicly available. Respect artists, copyright law, and TIDAL terms.

## Features

- Hi-Res Lossless playback requests through `audioquality=HI_RES_LOSSLESS`
- PKCE login with a browser redirect URL exchange
- Device authorization login for non-Hi-Res use
- BTS JSON and MPEG-DASH manifest parsing
- Concurrent segment downloads with retry and exponential backoff
- AES-128-CTR media decryption and TIDAL security-token decoding
- FLAC extraction, FFmpeg metadata tags, cover art, lyrics, and collision-safe paths
- PySide6/Qt6 GUI with background search, authentication, settings, and download queue
- CLI for scripted track, album, and playlist downloads

## Install

Python 3.11+ is required.

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
```

Install FFmpeg separately if you want embedded tags, cover art, lyrics, or FLAC extraction.

## Authentication

PKCE is the required login method for Hi-Res Lossless:

```bash
python -m tdl login --pkce
```

Open the URL, finish TIDAL login, then paste the full redirect URL from the browser address bar. The credentials are saved locally in `~/.tdl/token.json`; they are never printed or sent to another service by this application.

The Qt6 GUI provides the same flow under **Account**. The terminal UI is available with `python -m tdl tui`. Both interfaces prevent Hi-Res downloads until a PKCE token is active.

## Download

```bash
# Launch GUI
python -m tdl

# Launch terminal UI
python -m tdl tui

# CLI download
python -m tdl https://tidal.com/browse/track/12345
python -m tdl --quality hi_res_lossless https://tidal.com/browse/track/12345
python -m tdl https://tidal.com/browse/album/67890
python -m tdl https://tidal.com/browse/playlist/playlist-id
```

Supported URL types are tracks, albums, playlists, artists, videos, and mixes where the corresponding endpoint provides downloadable media. The Python migration currently prioritizes the Hi-Res audio path.

## Configuration

Settings are stored in `~/.tdl/settings.json`. Existing settings files from the previous version are read, including `path_binary_ffmpeg`, `metadata_cover_dimension`, and the `quality_audio` enum values.

Important settings include:

| Setting | Default | Purpose |
|---|---:|---|
| `quality_audio` | `low320k` | `low96k`, `low320k`, `high_lossless`, or `hi_res_lossless` |
| `download_base_path` | `~/download` | Destination root |
| `skip_existing` | `true` | Avoid duplicate downloads |
| `extract_flac` | `true` | Extract FLAC from compatible fragmented streams |
| `downloads_simultaneous_per_track_max` | `20` | Segment concurrency |
| `metadata_cover_embed` | `true` | Embed downloaded cover art |
| `lyrics_embed` | `false` | Write downloaded lyrics into the audio tags |
| `lyrics_file` | `false` | Save downloaded lyrics as a `.lrc` file |

When both `lyrics_embed` and `lyrics_file` are enabled, the lyrics are stored in both places. FFmpeg is required for embedded tags; the `.lrc` file can be saved without FFmpeg.

## Development

```bash
python -m unittest discover -v
python -m compileall -q tdl tests run.py
```

## License

[Apache-2.0](LICENSE)
