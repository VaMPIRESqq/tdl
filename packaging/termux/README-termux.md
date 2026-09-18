# tdl on Termux (Android)

Terminal-only package: CLI and curses TUI. The Qt GUI does not run on
Android and is not included.

## Requirements

- Termux from F-Droid or GitHub (the Play Store build is outdated)
- Termux packages: `python`, `openssl`, optionally `ffmpeg` for FLAC
  extraction, embedded tags, artwork, and lyrics

## Install

1. Download `tdl-termux-aarch64.tar.gz` from the latest GitHub release.
2. In Termux:

```bash
cd ~
tar -xzf tdl-termux-aarch64.tar.gz
cd tdl-termux-aarch64
bash install.sh
```

The script installs Python dependencies, places `tdl.pyz` in
`$PREFIX/share/tdl/`, and links it as `tdl` in `$PREFIX/bin`.

## Manual install (no script)

```bash
pkg install -y python openssl
python -m pip install requests pycryptodome
install -m 0755 tdl.pyz $PREFIX/bin/tdl
```

## Login and download

```bash
tdl login --pkce
tdl tui
tdl https://tidal.com/browse/track/12345
```

Hi-Res Lossless requires the PKCE login, exactly as on desktop. The token
is stored in `~/.tdl/token.json` on the device.

## Notes

- Termux can't produce a single-file binary: PyInstaller does not support
  Android targets, so the package ships a zipped pure-Python bundle that
  runs with any recent Python from the Termux repos.
- Storage outside the app sandbox requires `termux-setup-storage`; set
  `download_base_path` in `~/.tdl/settings.json` to a shared folder if you
  want files visible to other apps.
