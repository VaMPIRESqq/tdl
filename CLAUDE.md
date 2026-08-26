# CLAUDE.md

The application is implemented in Python 3.11+ with a PySide6 Qt6 GUI.

## Architecture

`tdl/__main__.py` is the CLI entry point. `tdl/gui.py` owns the Qt window and dispatches blocking work through `QThread`. `tdl/api.py` wraps TIDAL V1 and auth HTTP requests with retry handling. `tdl/auth.py` implements device authorization and the PKCE flow required for Hi-Res Lossless.

The audio path is:

```
TIDAL URL -> parse_media_url -> metadata -> playbackinfopostpaywall
           -> base64 BTS/MPD manifest -> concurrent segment download
           -> AES-128-CTR decrypt when required -> FLAC extraction/tags
```

Credentials and settings are stored under `~/.tdl/`. Existing JSON setting names are retained for compatibility. Hi-Res is requested with `HI_RES_LOSSLESS` and the GUI refuses that quality until the saved token was obtained through PKCE.

## Checks

```bash
python -m unittest discover -v
python -m compileall -q tdl tests run.py
```
