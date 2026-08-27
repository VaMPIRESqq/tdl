# tdl Quick Start

## Requirements

- Python 3.11 or newer
- A TIDAL subscription that includes the requested quality
- FFmpeg for embedded tags, cover art, and FLAC extraction

## Install

For a normal GUI launch without Python, download the ready-made archive from GitHub Releases:

- Linux x86_64: `tdl-gui-linux-x86_64.tar.gz`, then run `tdl-gui/tdl-gui`.
- Windows x86_64: `tdl-gui-windows-x86_64.zip`, then run `tdl-gui/tdl-gui.exe`.

The archive already includes Python, PySide6, and the application. FFmpeg is distributed separately and is required for FLAC extraction, embedded tags, artwork, and lyrics. Add it to `PATH` or set its path in Settings.

## Install from source

```bash
git clone https://github.com/epicsagas/tdl.git
cd tdl
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
```

## Authenticate

Use PKCE when downloading Hi-Res Lossless:

```bash
python -m tdl login --pkce
```

Open the printed TIDAL URL, complete login, and paste the full redirect URL shown by the browser. The token is saved in `~/.tdl/token.json` with owner-only permissions.

## Run

```bash
python -m tdl                         # Qt6 GUI
python -m tdl gui                     # Qt6 GUI
python -m tdl tui                     # terminal UI
python -m tdl https://tidal.com/browse/track/12345
python -m tdl --quality hi_res_lossless https://tidal.com/browse/track/12345
```

The GUI exposes the same PKCE flow on the Account page. The TUI exposes PKCE under menu item `3`. Select `Hi-Res Lossless` only after PKCE authentication has completed.

## Lyrics

Open Settings in either interface and enable `Lyrics in audio tags` and/or `Save separate .lrc file`. If both are enabled, lyrics are written into the audio file and saved beside it as an `.lrc` file. FFmpeg is required for embedded audio tags.

## Tests

```bash
python -m unittest discover -v
python -m compileall -q tdl tests run.py
```

Settings remain compatible with the previous application at `~/.tdl/settings.json`; credentials remain at `~/.tdl/token.json`.
