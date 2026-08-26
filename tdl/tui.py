"""Dependency-free terminal interface for tdl."""

from __future__ import annotations

import curses
import textwrap
from collections.abc import Callable
from typing import Any

from .api import TidalApi
from .auth import TidalAuth
from .downloader import HiResDownloader
from .models import Quality, Settings
from .storage import load_settings, save_settings


QUALITY_LABELS = {
    Quality.LOW96K: "Low 96 kbps",
    Quality.LOW320K: "High 320 kbps",
    Quality.LOSSLESS: "Lossless",
    Quality.HIRES: "Hi-Res Lossless",
}


class TuiApp:
    """Small curses application that uses the same core as the Qt GUI."""

    def __init__(self, screen: Any) -> None:
        self.screen = screen
        self.settings: Settings = load_settings()
        self.api = TidalApi()
        self.auth = TidalAuth(self.api)
        self.message = ""
        self.running = True
        self.search_items: list[tuple[str, str, str, Any]] = []

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        while self.running:
            self._draw_menu()
            key = self.screen.getch()
            if key in (ord("1"), ord("d"), ord("D")):
                self.download_url()
            elif key in (ord("2"), ord("s"), ord("S")):
                self.search()
            elif key in (ord("3"), ord("a"), ord("A")):
                self.login_pkce()
            elif key in (ord("4"), ord("c"), ord("C")):
                self.settings_menu()
            elif key in (ord("q"), ord("Q"), 27):
                self.running = False

    def _draw_menu(self) -> None:
        self._clear()
        self._title("tdl | TIDAL downloader")
        logged_in = self.auth.token.valid or self.auth.token.restorable
        auth_text = "PKCE authenticated" if self.auth.token.is_pkce and logged_in else "Not authenticated"
        self._line(2, f"Account: {auth_text}")
        self._line(3, f"Quality: {QUALITY_LABELS[self.settings.quality_audio]}")
        self._line(4, f"Lyrics in tags: {'on' if self.settings.lyrics_embed else 'off'}")
        self._line(5, f"Lyrics .lrc file: {'on' if self.settings.lyrics_file else 'off'}")
        self._line(7, "1  Download URL")
        self._line(8, "2  Search TIDAL")
        self._line(9, "3  Login with PKCE")
        self._line(10, "4  Settings")
        self._line(11, "q  Quit")
        self._footer()

    def _clear(self) -> None:
        self.screen.erase()

    def _title(self, value: str) -> None:
        self._line(0, value, curses.A_BOLD)
        self._line(1, "=" * min(self._width(), max(1, len(value))), curses.A_DIM)

    def _line(self, row: int, value: str, attributes: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height:
            return
        try:
            self.screen.addnstr(row, 0, value, max(1, width - 1), attributes)
        except curses.error:
            pass

    def _footer(self) -> None:
        height, _width = self.screen.getmaxyx()
        if height > 1:
            self._line(height - 1, self.message or "Press a number or letter to choose an action", curses.A_DIM)
        self.screen.refresh()

    def _width(self) -> int:
        return self.screen.getmaxyx()[1]

    def _pause(self, message: str | None = None) -> None:
        if message:
            self.message = message
        self._line(max(0, self.screen.getmaxyx()[0] - 2), "Press any key to continue", curses.A_DIM)
        self.screen.refresh()
        self.screen.getch()

    def _prompt(self, label: str) -> str:
        height, width = self.screen.getmaxyx()
        row = max(0, height - 3)
        self._line(row, " " * max(1, width - 1))
        self._line(row, label)
        self.screen.refresh()
        curses.echo()
        try:
            value = self.screen.getstr(row, min(len(label), max(0, width - 2)), max(1, width - len(label) - 2))
        finally:
            curses.noecho()
        return value.decode(errors="replace").strip()

    def _with_terminal(self, callback: Callable[[], Any]) -> Any:
        """Temporarily restore the real terminal for browser/CLI auth prompts."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            return callback()
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()

    def _ensure_auth(self) -> bool:
        if self.auth.restore():
            return True
        self.message = "No active session. Choose PKCE login first."
        self._pause()
        return False

    def login_pkce(self) -> None:
        self.message = "PKCE login is handled in the normal terminal prompt."
        try:
            self._with_terminal(lambda: self.auth.login_for_cli(pkce=True))
            self.message = "PKCE login successful"
        except Exception as exc:
            self.message = f"Login failed: {exc}"
        self._pause()

    def download_url(self) -> None:
        url = self._prompt("TIDAL URL: ")
        if not url:
            return
        if not self._ensure_auth():
            return
        self.message = "Starting download..."
        self._clear()
        self._title("Download")
        self._line(3, url)
        self.screen.refresh()

        def progress(current: int, total: int, message: str) -> None:
            value = f"[{current}/{total}] {message}"
            self.message = value
            self._line(5, value)
            self.screen.refresh()

        try:
            paths = HiResDownloader(self.api, self.settings, progress).download_url(url)
            self._line(7, "Saved files:", curses.A_BOLD)
            for index, path in enumerate(paths, 8):
                self._line(index, str(path))
            self._pause("Download complete")
        except Exception as exc:
            self._pause(f"Download failed: {exc}")

    def search(self) -> None:
        query = self._prompt("Search: ")
        if not query:
            return
        if not self._ensure_auth():
            return
        try:
            payload = self.api.search(query)
            tracks = payload.get("tracks", {}).get("items", [])
            albums = payload.get("albums", {}).get("items", [])
            self.search_items = [
                ("track", item.get("title", item.get("name", "")), self._artist(item), item.get("id"))
                for item in tracks
            ]
            self.search_items += [
                ("album", item.get("title", item.get("name", "")), self._artist(item), item.get("id"))
                for item in albums
            ]
            self._search_results()
        except Exception as exc:
            self._pause(f"Search failed: {exc}")

    def _search_results(self) -> None:
        selected = 0
        while True:
            self._clear()
            self._title("Search results")
            self._line(2, "Up/Down select, Enter download, b back")
            height = self.screen.getmaxyx()[0]
            visible = max(1, height - 6)
            start = max(0, min(selected - visible + 1, len(self.search_items) - visible))
            for offset, (media_type, title, artist, media_id) in enumerate(self.search_items[start:start + visible]):
                row = offset + 4
                prefix = "> " if start + offset == selected else "  "
                value = f"{prefix}{media_type.title():5} {title} | {artist}"
                self._line(row, value, curses.A_REVERSE if start + offset == selected else 0)
            self._footer()
            key = self.screen.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")) and self.search_items:
                selected = (selected - 1) % len(self.search_items)
            elif key in (curses.KEY_DOWN, ord("j")) and self.search_items:
                selected = (selected + 1) % len(self.search_items)
            elif key in (curses.KEY_ENTER, 10, 13) and self.search_items:
                media_type, _title, _artist, media_id = self.search_items[selected]
                self.url_download(f"https://tidal.com/browse/{media_type}/{media_id}")
                return

    def url_download(self, url: str) -> None:
        self.screen.clear()
        self._title("Download")
        self._line(3, url)
        self.screen.refresh()
        if not self._ensure_auth():
            return

        def progress(current: int, total: int, message: str) -> None:
            self._line(5, f"[{current}/{total}] {message}")
            self.screen.refresh()

        try:
            paths = HiResDownloader(self.api, self.settings, progress).download_url(url)
            self._line(7, "Saved files:", curses.A_BOLD)
            for index, path in enumerate(paths, 8):
                self._line(index, str(path))
            self._pause("Download complete")
        except Exception as exc:
            self._pause(f"Download failed: {exc}")

    def settings_menu(self) -> None:
        while True:
            self._clear()
            self._title("Settings")
            self._line(3, "1-4  Audio quality")
            for index, quality in enumerate(Quality, 1):
                marker = "*" if quality is self.settings.quality_audio else " "
                self._line(3 + index, f"{index}  [{marker}] {QUALITY_LABELS[quality]}")
            self._line(9, f"e  Lyrics in audio tags: {'on' if self.settings.lyrics_embed else 'off'}")
            self._line(10, f"l  Save separate .lrc file: {'on' if self.settings.lyrics_file else 'off'}")
            self._line(11, f"p  Download folder: {self.settings.download_base_path}")
            self._line(12, f"k  Skip existing files: {'on' if self.settings.skip_existing else 'off'}")
            self._line(13, "s  Save settings")
            self._line(14, "b  Back")
            self._footer()
            key = self.screen.getch()
            quality_keys = {ord(str(index)): quality for index, quality in enumerate(Quality, 1)}
            if key in quality_keys:
                self.settings.quality_audio = quality_keys[key]
                self.message = f"Quality: {QUALITY_LABELS[self.settings.quality_audio]}"
            elif key in (ord("e"), ord("E")):
                self.settings.lyrics_embed = not self.settings.lyrics_embed
            elif key in (ord("l"), ord("L")):
                self.settings.lyrics_file = not self.settings.lyrics_file
            elif key in (ord("p"), ord("P")):
                value = self._prompt("Download folder: ")
                if value:
                    self.settings.download_base_path = value
            elif key in (ord("k"), ord("K")):
                self.settings.skip_existing = not self.settings.skip_existing
            elif key in (ord("s"), ord("S")):
                save_settings(self.settings)
                self.message = "Settings saved"
            elif key in (ord("b"), ord("B"), 27):
                return

    @staticmethod
    def _artist(item: dict[str, Any]) -> str:
        artists = item.get("artists") or []
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        if names:
            return ", ".join(names)
        artist = item.get("artist") or item.get("author") or {}
        if isinstance(artist, dict):
            return artist.get("name", "") or artist.get("title", "")
        return str(artist) if artist else ""


def run_tui() -> int:
    """Start the curses interface and return a process exit code."""
    curses.wrapper(lambda screen: TuiApp(screen).run())
    return 0
