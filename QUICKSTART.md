# tdl Quick Start

## Requirements

- Python 3.11 or newer
- A TIDAL subscription that includes the requested quality
- FFmpeg for embedded tags, cover art, and FLAC extraction

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
python -m tdl https://tidal.com/browse/track/12345
python -m tdl --quality hi_res_lossless https://tidal.com/browse/track/12345
```

The GUI exposes the same PKCE flow on the Account page. Select `Hi-Res Lossless` on the Download page only after PKCE authentication has completed.

## Tests

```bash
python -m unittest discover -v
python -m compileall -q tdl tests run.py
```

Settings remain compatible with the previous application at `~/.tdl/settings.json`; credentials remain at `~/.tdl/token.json`.
