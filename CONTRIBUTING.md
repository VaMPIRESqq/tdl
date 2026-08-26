# Contributing to tdl

## Development setup

```bash
git clone https://github.com/epicsagas/tdl.git
cd tdl
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
```

Run the checks before opening a pull request:

```bash
python -m unittest discover -v
python -m compileall -q tdl tests run.py
```

## Architecture

- `tdl/api.py`: authenticated TIDAL V1 HTTP client and response models
- `tdl/auth.py`: device authorization and PKCE token lifecycle
- `tdl/stream.py`: BTS JSON and MPEG-DASH manifest parsing
- `tdl/crypto.py`: security-token AES-CBC and media AES-128-CTR decryption
- `tdl/downloader.py`: segment download, Hi-Res FLAC extraction, tags, cover, and lyrics
- `tdl/gui.py`: PySide6 GUI and background workers
- `tdl/models.py`, `tdl/storage.py`, `tdl/paths.py`: shared state and safe local persistence

Keep network and filesystem work out of the Qt main thread. Do not log or commit access tokens, refresh tokens, client secrets, or downloaded media. Preserve path sanitization and the explicit PKCE requirement for `hi_res_lossless`.

## Commit format

Use a concise conventional-style prefix where practical, such as `feat:`, `fix:`, `docs:`, `refactor:`, or `test:`.
