"""Command-line entry point for tdl."""

from __future__ import annotations

import argparse
import logging
import sys

from .api import TidalApi
from .auth import TidalAuth
from .downloader import HiResDownloader
from .models import Quality
from .storage import load_settings


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    commands = {"login", "logout", "gui", "tui"}
    # Keep bare URL downloads ergonomic while still allowing argparse
    # subcommands. A URL is otherwise consumed as a subcommand value when it
    # follows a global option such as --quality.
    if any(argument.startswith(("http://", "https://")) for argument in raw_args) and not any(argument in commands for argument in raw_args):
        quality = None
        urls: list[str] = []
        index = 0
        while index < len(raw_args):
            argument = raw_args[index]
            if argument == "--quality" and index + 1 < len(raw_args):
                quality = raw_args[index + 1]
                index += 2
                continue
            if argument.startswith(("http://", "https://")):
                urls.append(argument)
            else:
                raise SystemExit(f"unknown argument for URL download: {argument}")
            index += 1
        return _download_urls(urls, quality)

    parser = argparse.ArgumentParser(prog="tdl", description="TIDAL Hi-Res downloader")
    parser.add_argument("urls", nargs="*", help="TIDAL URLs to download")
    parser.add_argument("--quality", choices=[quality.value for quality in Quality], help="audio quality")
    subparsers = parser.add_subparsers(dest="command")
    login = subparsers.add_parser("login", help="authenticate with TIDAL")
    login.add_argument("--pkce", action="store_true", help="use PKCE authentication required for Hi-Res")
    subparsers.add_parser("logout", help="remove saved credentials")
    subparsers.add_parser("gui", help="launch the Qt6 interface")
    subparsers.add_parser("tui", help="launch the terminal interface")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "gui" or (args.command is None and not args.urls):
        from .gui import run
        return run()
    if args.command == "tui":
        from .tui import run_tui
        return run_tui()

    settings = load_settings()
    if args.quality:
        settings.quality_audio = Quality(args.quality)
    api = TidalApi()
    auth = TidalAuth(api)
    if args.command == "login":
        auth.login_for_cli(args.pkce)
        print("Login successful.")
        return 0
    if args.command == "logout":
        auth.logout()
        print("Logged out.")
        return 0
    if not auth.restore():
        auth.login_for_cli(settings.quality_audio is Quality.HIRES)
    if not args.urls:
        parser.error("provide a TIDAL URL or use the gui/login command")
    return _download_urls(args.urls, settings.quality_audio.value, auth=auth, api=api, settings=settings)


def _download_urls(urls: list[str], quality: str | None, *, auth: TidalAuth | None = None,
                   api: TidalApi | None = None, settings: object | None = None) -> int:
    settings = settings or load_settings()
    if quality:
        settings.quality_audio = Quality(quality)
    api = api or TidalApi()
    auth = auth or TidalAuth(api)
    if auth.token.valid is False and not auth.restore():
        auth.login_for_cli(settings.quality_audio is Quality.HIRES)
    downloader = HiResDownloader(api, settings, lambda current, total, message: print(f"[{current}/{total}] {message}"))
    failures = 0
    for url in urls:
        try:
            paths = downloader.download_url(url)
            for path in paths:
                print(f"Saved: {path}", flush=True)
        except Exception as exc:
            failures += 1
            print(f"Error downloading {url}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
