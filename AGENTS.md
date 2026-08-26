# AGENTS.md

## Commands
- Install: `python -m pip install -r requirements.txt`
- GUI: `python -m tdl` or `python -m tdl gui`
- CLI download: `python -m tdl [--quality hi_res_lossless] <URL>`
- PKCE login: `python -m tdl login --pkce`
- Tests: `python -m unittest discover -v`
- Compile check: `python -m compileall -q tdl tests run.py`

## Project Structure
- `tdl/__main__.py` — CLI routing and Qt launch
- `tdl/gui.py` — PySide6/Qt6 window and background workers
- `tdl/api.py` — TIDAL V1/auth HTTP client, pagination, and API models
- `tdl/auth.py` — device authorization and PKCE authentication
- `tdl/stream.py` — BTS JSON and MPEG-DASH parsing
- `tdl/downloader.py` — segment download, decrypt, FLAC extraction, tags, artwork, lyrics
- `tdl/crypto.py` — AES-CBC security-token decoding and AES-128-CTR media decryption
- `tdl/models.py` — settings, token, track, album, playback models
- `tdl/storage.py` — JSON persistence at `~/.tdl/`
- `tdl/paths.py` — URL parsing and filename/path sanitization
- `tests/test_core.py` — unit tests for the critical media/auth path

## Code Style
- Python 3.11+, type hints for public functions, small focused modules
- Keep all network and filesystem work out of the Qt main thread
- Use `logging`, not ad-hoc debug output in library modules
- Keep access/refresh tokens out of logs and error messages
- Sanitize every user/API-derived filename before filesystem operations
- Preserve `~/.tdl/settings.json` and `~/.tdl/token.json` compatibility

## Hi-Res Requirements
- `Quality.HIRES` maps to TIDAL `HI_RES_LOSSLESS`
- Hi-Res downloads require a token obtained through PKCE
- PKCE uses S256 challenge and the existing TIDAL Android redirect URI
- FFmpeg is optional but required for container extraction and embedded tags

## Boundaries
- Do not hardcode or replace OAuth client credentials without explicit approval
- Do not remove path traversal/filename sanitization
- Do not print or commit credentials or downloaded content
- Run unit tests and compile checks after functional changes
