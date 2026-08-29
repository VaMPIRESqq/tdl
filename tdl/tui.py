"""Dependency-free terminal interface for tdl."""

from __future__ import annotations

import curses
import io
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from .api import TidalApi
from .auth import TidalAuth
from .downloader import HiResDownloader, load_downloaded_file_info
from .models import DownloadedFileInfo, Quality, Settings
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
        self.download_status = "IDLE"
        self.download_current = 0
        self.download_total = 0
        self.download_message = "Ready for a TIDAL URL"
        self.download_track = ""
        self.last_saved: list[str] = []
        self.session_files: list[Path] = []
        self.focused_action = 0
        self.action_zones: list[tuple[int, int, int, int]] = []
        self.search_zones: list[tuple[int, int, int, Any]] = []
        self.search_action_zones: list[tuple[int, int, int, Any]] = []
        self.settings_zones: list[tuple[int, int, int, Any]] = []
        self.settings_action_zones: list[tuple[int, int, int, Any]] = []
        self.settings_selected = 0
        self._init_colors()

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass
        while self.running:
            self._draw_dashboard()
            key, x, y, bstate = self._read_event()
            if key == getattr(curses, "KEY_MOUSE", -1):
                if self._mouse_wheel(bstate):
                    self._move_action(-1 if self._mouse_wheel_up(bstate) else 1)
                elif self._mouse_clicked(bstate):
                    action = self._hit_test(self.action_zones, x, y)
                    if action is not None:
                        self.focused_action = action
                        self._activate_action(action)
                continue
            self._handle_dashboard_key(key)

    def _read_event(self) -> tuple[int, int | None, int | None, int | None]:
        key = self.screen.getch()
        if key != getattr(curses, "KEY_MOUSE", -1):
            return key, None, None, None
        try:
            _event_id, x, y, _z, bstate = curses.getmouse()
            return key, x, y, bstate
        except curses.error:
            return key, None, None, None

    @staticmethod
    def _mouse_clicked(bstate: int | None) -> bool:
        if bstate is None:
            return False
        mask = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_RELEASED", 0)
        return bool(mask and bstate & mask)

    @staticmethod
    def _mouse_wheel(bstate: int | None) -> bool:
        if bstate is None:
            return False
        mask = getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)
        return bool(mask and bstate & mask)

    @staticmethod
    def _mouse_wheel_up(bstate: int | None) -> bool:
        return bool(bstate is not None and bstate & getattr(curses, "BUTTON4_PRESSED", 0))

    @staticmethod
    def _hit_test(zones: list[tuple[int, int, int, Any]], x: int | None, y: int | None) -> Any:
        if x is None or y is None:
            return None
        for row, left, right, action in zones:
            if y == row and left <= x <= right:
                return action
        return None

    def _handle_dashboard_key(self, key: int) -> None:
        if key in (curses.KEY_UP, curses.KEY_LEFT, ord("k")):
            self._move_action(-1)
        elif key in (curses.KEY_DOWN, curses.KEY_RIGHT, ord("j")):
            self._move_action(1)
        elif key in (curses.KEY_HOME,):
            self.focused_action = 0
        elif key in (curses.KEY_END,):
            self.focused_action = 4
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._activate_action(self.focused_action)
        elif key in (ord("1"), ord("d"), ord("D")):
            self.focused_action = 0
            self._activate_action(0)
        elif key in (ord("2"), ord("s"), ord("S")):
            self.focused_action = 1
            self._activate_action(1)
        elif key in (ord("3"), ord("a"), ord("A")):
            self.focused_action = 2
            self._activate_action(2)
        elif key in (ord("4"), ord("c"), ord("C")):
            self.focused_action = 3
            self._activate_action(3)
        elif key in (ord("q"), ord("Q"), 27):
            self.running = False

    def _move_action(self, delta: int) -> None:
        self.focused_action = (self.focused_action + delta) % 5

    def _activate_action(self, action: int) -> None:
        if action == 0:
            self.download_url()
        elif action == 1:
            self.search()
        elif action == 2:
            self.login_pkce()
        elif action == 3:
            self.settings_menu()
        elif action == 4:
            self.show_library()

    def _init_colors(self) -> None:
        self.color_title = 0
        self.color_accent = 0
        self.color_selected = 0
        self.color_error = 0
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            self.color_title = curses.color_pair(1)
            self.color_accent = curses.color_pair(2)
            self.color_selected = curses.color_pair(3)
            self.color_error = curses.color_pair(4)
        except curses.error:
            self.color_title = self.color_accent = self.color_selected = self.color_error = 0

    def _draw_dashboard(self) -> None:
        self._clear()
        height, width = self.screen.getmaxyx()
        self._header("tdl", "TIDAL DOWNLOADER")
        self.action_zones = []
        if height < 12 or width < 58:
            self._draw_compact_dashboard()
            self._draw_actions(max(7, height - 4), width)
            self._footer("arrows move  enter select  mouse click  q quit")
            return

        gap = 1
        left_width = min(34, max(28, width // 3))
        right_width = width - left_width - gap
        panel_height = max(8, min(13, height - 10))
        self._panel(3, 0, panel_height, left_width, "SYSTEM")
        self._panel(3, left_width + gap, panel_height, right_width, "DOWNLOAD PIPELINE")
        self._draw_system_panel(3, 0, panel_height, left_width)
        self._draw_pipeline_panel(3, left_width + gap, panel_height, right_width)
        self._draw_actions(5 + panel_height, width)
        self._draw_recent(8 + panel_height, width, height - (8 + panel_height) - 2)
        self._footer("arrows move  enter select  mouse click  q quit")

    def _draw_compact_dashboard(self) -> None:
        height, width = self.screen.getmaxyx()
        self._line(3, "ACCOUNT", curses.A_BOLD | self.color_title)
        logged_in = self.auth.token.valid or self.auth.token.restorable
        auth_text = "PKCE / active" if self.auth.token.is_pkce and logged_in else "offline"
        self._line(4, f"  Session  {auth_text}")
        self._line(5, f"  Quality  {QUALITY_LABELS[self.settings.quality_audio]}")
        self._line(7, "DOWNLOAD PIPELINE", curses.A_BOLD | self.color_title)
        self._line(8, f"  {self.download_status}: {self._fit(self.download_message, max(1, width - 5))}")
        if height > 11:
            self._line(10, self._progress_bar(self.download_current, self.download_total, max(10, width - 4)), self.color_accent)

    def _header(self, name: str, subtitle: str) -> None:
        width = self._width()
        state = self._account_state()
        state_attr = self.color_accent if state == "ONLINE" else self.color_error
        self._text(0, 0, f" {name} ", curses.A_BOLD | self.color_title)
        right = f"{subtitle}  [{state}]"
        self._text(0, max(0, width - len(right) - 1), right, curses.A_BOLD | state_attr)
        self._line(1, "=" * max(1, width - 1), self.color_title)
        self._line(2, "TIDAL MEDIA CONTROL", curses.A_DIM)

    def _account_state(self) -> str:
        logged_in = self.auth.token.valid or self.auth.token.restorable
        return "ONLINE" if self.auth.token.is_pkce and logged_in else "OFFLINE"

    def _draw_system_panel(self, row: int, col: int, height: int, width: int) -> None:
        logged_in = self.auth.token.valid or self.auth.token.restorable
        account = "PKCE authenticated" if self.auth.token.is_pkce and logged_in else "Not authenticated"
        values = (
            ("ACCOUNT", account),
            ("QUALITY", QUALITY_LABELS[self.settings.quality_audio]),
            ("LYRICS TAGS", self._switch(self.settings.lyrics_embed)),
            ("LYRICS FILE", self._switch(self.settings.lyrics_file)),
            ("SKIP EXISTING", self._switch(self.settings.skip_existing)),
        )
        for offset, (label, value) in enumerate(values, 1):
            self._panel_value(row + offset, col, width, label, value)
        self._text(row + height - 2, col + 2, "[3] auth   [4] configure", curses.A_DIM)

    def _draw_pipeline_panel(self, row: int, col: int, height: int, width: int) -> None:
        inner = max(1, width - 4)
        self._text(row + 1, col + 2, self.download_status, curses.A_BOLD | self._status_color())
        self._text(row + 2, col + 2, self._fit(self.download_message, inner))
        self._text(row + 4, col + 2, self._progress_bar(self.download_current, self.download_total, inner), self.color_accent)
        if self.download_total:
            percentage = min(100, int(self.download_current * 100 / self.download_total))
            detail = f"{percentage:3d}%   {self.download_current}/{self.download_total} units"
        else:
            detail = "awaiting input"
        self._text(row + 5, col + 2, detail, curses.A_DIM)
        self._text(row + 6, col + 2, "TRACK", curses.A_BOLD | self.color_title)
        self._text(row + 7, col + 2, self._fit(self.download_track or "-", inner))
        if height > 10:
            self._text(row + height - 2, col + 2, "[1] start download", curses.A_DIM)

    def _draw_actions(self, row: int, width: int) -> None:
        if row >= self.screen.getmaxyx()[0] - 2:
            return
        self._line(row, "ACTIONS", curses.A_BOLD | self.color_title)
        self._draw_button_row(row + 1, width)

    def _draw_button_row(self, row: int, width: int) -> None:
        long_labels = ("DOWNLOAD", "SEARCH", "PKCE LOGIN", "SETTINGS", "FILES")
        short_labels = ("DL", "SEARCH", "LOGIN", "CFG", "FILES")
        labels = long_labels if width >= 72 else short_labels
        x = 1
        for index, (key_name, label) in enumerate(zip("1234Q", labels)):
            display = f"[ {key_name} {label} ]"
            if x + len(display) >= width:
                break
            attributes = curses.A_REVERSE | self.color_selected if index == self.focused_action else self.color_selected
            self._text(row, x, display, attributes)
            self.action_zones.append((row, x, x + len(display) - 1, index))
            x += len(display) + 1

    def _button(self, row: int, col: int, label: str, attributes: int = 0) -> None:
        self._text(row, col, f"[ {label} ]", attributes)

    def _draw_recent(self, row: int, width: int, available: int) -> None:
        if available < 3:
            return
        self._panel(row, 0, available, width, "RECENT OUTPUT")
        if self.last_saved:
            for offset, path in enumerate(self.last_saved[:available - 2], 1):
                self._text(row + offset, 2, self._fit(path, width - 4), self.color_accent)
        else:
            self._text(row + 1, 2, "No downloads in this session.", curses.A_DIM)

    def _status_color(self) -> int:
        return self.color_error if self.download_status == "ERROR" else self.color_accent

    @staticmethod
    def _switch(value: bool) -> str:
        return "ON " if value else "OFF"

    @staticmethod
    def _fit(value: str, width: int) -> str:
        if width <= 0:
            return ""
        if len(value) <= width:
            return value
        if width < 4:
            return value[:width]
        return value[:width - 3] + "..."

    @staticmethod
    def _progress_bar(current: int, total: int, width: int) -> str:
        width = max(3, width)
        if total <= 0:
            return "[" + "-" * (width - 2) + "]"
        filled = max(0, min(width - 2, int((current / total) * (width - 2))))
        return "[" + "#" * filled + "-" * (width - 2 - filled) + "]"

    def _clear(self) -> None:
        self.screen.erase()

    def _panel(self, row: int, col: int, height: int, width: int, title: str) -> None:
        height = min(height, self.screen.getmaxyx()[0] - row)
        width = min(width, self._width() - col)
        if height < 2 or width < 2:
            return
        horizontal = "-" * max(1, width - 2)
        self._text(row, col, "+" + horizontal + "+", self.color_title)
        self._text(row, col + 2, f" {self._fit(title, max(1, width - 6))} ", curses.A_BOLD | self.color_title)
        for line in range(row + 1, row + height - 1):
            self._text(line, col, "|", self.color_title)
            self._text(line, col + width - 1, "|", self.color_title)
        self._text(row + height - 1, col, "+" + horizontal + "+", self.color_title)

    def _panel_value(self, row: int, col: int, width: int, label: str, value: str) -> None:
        label_width = min(15, max(8, width // 2))
        self._text(row, col + 2, self._fit(label, label_width), curses.A_DIM)
        self._text(row, col + 2 + label_width, self._fit(value, width - label_width - 5), curses.A_BOLD)

    def _text(self, row: int, col: int, value: str, attributes: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if row < 0 or row >= height or col < 0 or col >= width:
            return
        try:
            self.screen.addnstr(row, col, value, max(1, width - col - 1), attributes)
        except curses.error:
            pass

    def _line(self, row: int, value: str, attributes: int = 0) -> None:
        self._text(row, 0, value, attributes)

    def _footer(self, value: str | None = None) -> None:
        height, _width = self.screen.getmaxyx()
        if height > 1:
            self._line(height - 1, self.message or value or "arrows move  enter select  mouse click", curses.A_DIM)
        self.screen.refresh()

    def _width(self) -> int:
        return self.screen.getmaxyx()[1]

    @staticmethod
    def _file_info_lines(info: DownloadedFileInfo, width: int) -> list[str]:
        lines = [
            f"Path: {info.path}",
            f"Size: {info.size_display}",
            f"Technical: {info.technical_display}",
        ]
        if info.title or info.artist or info.album:
            lines.append(f"Track: {info.artist or 'Unknown artist'} - {info.title or 'Unknown title'}")
            lines.append(f"Album: {info.album or 'Unknown album'}")
        return [TuiApp._fit(line, width) for line in lines]

    def show_library(self) -> None:
        files = [Path(path) for path in self.session_files if Path(path).is_file()]
        selected = 0
        while True:
            self._clear()
            self._header("tdl", "DOWNLOADED FILES")
            height, width = self.screen.getmaxyx()
            self._panel(4, 0, max(5, height - 7), width, "ALBUMS / PLAYLISTS / DISCOGRAPHY")
            self._text(5, 2, "UP/DOWN select   ENTER details   B back", curses.A_DIM)
            visible = max(1, height - 9)
            if not files:
                self._text(7, 2, "No downloaded media files found.", curses.A_DIM)
            start = max(0, min(selected - visible + 1, len(files) - visible))
            for offset, path in enumerate(files[start:start + visible]):
                row = 7 + offset
                info = load_downloaded_file_info(path, self.settings.ffmpeg_path)
                value = f"{'>' if start + offset == selected else ' '} {path.name} | {info.size_display} | {info.technical_display}"
                self._text(row, 2, self._fit(value, width - 4), (curses.A_REVERSE | self.color_selected) if start + offset == selected else 0)
            self._footer("up/down move  enter details  b back")
            key, _x, _y, _bstate = self._read_event()
            if key in (ord("b"), ord("B"), 27, curses.KEY_BACKSPACE, 127):
                return
            if key in (curses.KEY_UP, ord("k")) and files:
                selected = (selected - 1) % len(files)
            elif key in (curses.KEY_DOWN, ord("j")) and files:
                selected = (selected + 1) % len(files)
            elif key in (curses.KEY_ENTER, 10, 13, ord(" ")) and files:
                self._show_file_info(str(files[selected]))
            elif key in (ord("q"), ord("Q")):
                self.running = False
                return

    def _show_file_info(self, path: str) -> None:
        info = load_downloaded_file_info(Path(path), self.settings.ffmpeg_path)
        self._clear()
        self._header("tdl", "FILE INFORMATION")
        self._panel(4, 0, max(7, self.screen.getmaxyx()[0] - 7), self._width(), "DOWNLOADED FILE")
        for index, line in enumerate(self._file_info_lines(info, self._width() - 4), 6):
            self._text(index, 2, line, self.color_accent if index == 8 else 0)
        self._pause("File information")

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
        self._line(row, label, self.color_accent)
        self.screen.refresh()
        curses.echo()
        try:
            value = self.screen.getstr(row, min(len(label), max(0, width - 2)), max(1, width - len(label) - 2))
        finally:
            curses.noecho()
        return value.decode(errors="replace").strip()

    @contextmanager
    def _quiet_download(self) -> Iterator[None]:
        """Keep library output out of the terminal while curses owns the screen."""
        previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                yield
        finally:
            logging.disable(previous_disable)

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

    def _draw_download(self, url: str) -> None:
        self.download_status = "RUNNING"
        self.download_current = 0
        self.download_total = 0
        self.download_message = "Connecting to TIDAL"
        self.download_track = ""
        self.last_saved = []
        self._clear()
        self._header("tdl", "DOWNLOAD SESSION")
        self._panel(4, 0, 8, self._width(), "DOWNLOAD PIPELINE")
        self._text(5, 2, "URL", curses.A_DIM)
        self._text(5, 14, self._fit(url, self._width() - 17))
        self._text(7, 2, self.download_status, curses.A_BOLD | self.color_accent)
        self._text(8, 2, self._progress_bar(0, 0, max(3, self._width() - 4)), self.color_accent)
        self._text(10, 2, "TRACK", curses.A_DIM)
        self._text(10, 14, self._fit(self.download_track or "-", self._width() - 17))
        self._footer("downloading...",)

    @staticmethod
    def _track_from_message(message: str, previous: str) -> str:
        """Keep the current track name; segment/progress noise must not overwrite it."""
        text = (message or "").strip()
        if not text or text.startswith("Downloading segments") or text.startswith("Saved:"):
            return previous
        if text.startswith("Failed:") or text.startswith("Completed") or text.startswith("Connecting"):
            return previous
        return text

    def _download(self, url: str) -> None:
        self._draw_download(url)

        def progress(current: int, total: int, message: str) -> None:
            self.download_current = current
            self.download_total = total
            self.download_message = message
            self.download_track = self._track_from_message(message, self.download_track)
            self.message = f"{current}/{total}"
            self._text(7, 2, self._fit(self.download_status, 12), curses.A_BOLD | self._status_color())
            self._text(8, 2, self._progress_bar(current, total, max(3, self._width() - 4)), self.color_accent)
            self._text(9, 2, " " * max(1, self._width() - 3))
            self._text(9, 2, self._fit(message, self._width() - 4))
            self._text(10, 2, " " * max(1, self._width() - 3))
            self._text(10, 2, "TRACK", curses.A_DIM)
            self._text(10, 14, self._fit(self.download_track or "-", self._width() - 17), self.color_accent)
            self.screen.refresh()

        try:
            with self._quiet_download():
                paths = HiResDownloader(self.api, self.settings, progress).download_url(url)
            self.download_status = "COMPLETE"
            self.download_message = f"Downloaded {len(paths)} file(s)"
            self.last_saved = [str(path) for path in paths]
            self.session_files = [Path(path) for path in paths]
            self._text(7, 2, self.download_status, curses.A_BOLD | self.color_accent)
            if paths:
                info = load_downloaded_file_info(paths[0], self.settings.ffmpeg_path)
                self.download_message = f"{info.size_display} · {info.technical_display}"
            self._text(9, 2, self._fit(self.download_message, self._width() - 4), self.color_accent)
            self._pause("Download complete")
            if self.last_saved:
                self._show_file_info(self.last_saved[0])
        except Exception as exc:
            self.download_status = "ERROR"
            self.download_message = str(exc)
            self._text(7, 2, self.download_status, curses.A_BOLD | self.color_error)
            self._text(9, 2, self._fit(self.download_message, self._width() - 4), self.color_error)
            self._pause(f"Download failed: {exc}")

    def download_url(self) -> None:
        url = self._prompt("TIDAL URL: ")
        if not url:
            return
        if not self._ensure_auth():
            return
        self._download(url)

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
            self._header("tdl", "SEARCH RESULTS")
            height, width = self.screen.getmaxyx()
            self._panel(4, 0, max(4, height - 7), width, "MEDIA INDEX")
            self._text(5, 2, "UP/DOWN select   ENTER download   B back", curses.A_DIM)
            visible = max(1, height - 10)
            if not self.search_items:
                self._text(7, 2, "No tracks or albums found.", self.color_accent)
            start = max(0, min(selected - visible + 1, len(self.search_items) - visible))
            self.search_zones = []
            self.search_action_zones = []
            self._button(5, 2, "BACK", self.color_selected)
            self.search_action_zones.append((5, 2, 9, "back"))
            for offset, (media_type, title, artist, media_id) in enumerate(self.search_items[start:start + visible]):
                row = offset + 7
                is_selected = start + offset == selected
                prefix = ">" if is_selected else " "
                value = f" {prefix} {media_type.title():5}  {title}  |  {artist}  ({media_id})"
                self._text(row, 2, self._fit(value, width - 4), (curses.A_REVERSE | self.color_selected) if is_selected else 0)
                self.search_zones.append((row, 2, max(2, width - 3), start + offset))
            self._footer("up/down move  enter download  click item  b back")
            key, x, y, bstate = self._read_event()
            if key == getattr(curses, "KEY_MOUSE", -1):
                if self._mouse_wheel(bstate) and self.search_items:
                    selected = (selected - 1 if self._mouse_wheel_up(bstate) else selected + 1) % len(self.search_items)
                elif self._mouse_clicked(bstate):
                    command = self._hit_test(self.search_action_zones, x, y)
                    if command == "back":
                        return
                    clicked = self._hit_test(self.search_zones, x, y)
                    if clicked is not None:
                        selected = clicked
                        media_type, _title, _artist, media_id = self.search_items[selected]
                        self.url_download(f"https://tidal.com/browse/{media_type}/{media_id}")
                        return
                continue
            if key in (ord("b"), ord("B"), 27, curses.KEY_BACKSPACE, 127, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")) and self.search_items:
                selected = (selected - 1) % len(self.search_items)
            elif key in (curses.KEY_DOWN, ord("j")) and self.search_items:
                selected = (selected + 1) % len(self.search_items)
            elif key == curses.KEY_PPAGE and self.search_items:
                selected = max(0, selected - visible)
            elif key == curses.KEY_NPAGE and self.search_items:
                selected = min(len(self.search_items) - 1, selected + visible)
            elif key == curses.KEY_HOME and self.search_items:
                selected = 0
            elif key == curses.KEY_END and self.search_items:
                selected = len(self.search_items) - 1
            elif key in (getattr(curses, "KEY_BTAB", -1), 9) and self.search_items:
                selected = (selected - 1) % len(self.search_items)
            elif key in (curses.KEY_ENTER, 10, 13, ord(" ")) and self.search_items:
                media_type, _title, _artist, media_id = self.search_items[selected]
                self.url_download(f"https://tidal.com/browse/{media_type}/{media_id}")
                return

    def url_download(self, url: str) -> None:
        if not self._ensure_auth():
            return
        self._download(url)

    def settings_menu(self) -> None:
        self.settings_selected = 0
        while True:
            self._clear()
            self._header("tdl", "CONFIGURATION")
            height, width = self.screen.getmaxyx()
            self._panel(4, 0, max(4, height - 7), width, "SETTINGS")
            self._text(5, 2, "AUDIO QUALITY", curses.A_BOLD | self.color_title)
            self.settings_zones = []
            item_count = len(Quality) + 4
            focus_count = item_count + 2
            for index, quality in enumerate(Quality, 1):
                marker = "*" if quality is self.settings.quality_audio else " "
                selected = self.settings_selected == index - 1
                attributes = (curses.A_REVERSE | self.color_selected) if selected else (self.color_accent if marker == "*" else 0)
                row = 6 + index
                self._text(row, 4, f"{index}  [{marker}] {QUALITY_LABELS[quality]}", attributes)
                self.settings_zones.append((row, 4, max(4, width - 3), index - 1))
            options = (
                ("e", f"Lyrics in audio tags       {self._switch(self.settings.lyrics_embed)}"),
                ("l", f"Save separate .lrc file   {self._switch(self.settings.lyrics_file)}"),
                ("p", f"Download folder            {self.settings.download_base_path}"),
                ("k", f"Skip existing files        {self._switch(self.settings.skip_existing)}"),
            )
            self._text(12, 2, "DOWNLOAD", curses.A_BOLD | self.color_title)
            for offset, (key_name, label) in enumerate(options):
                row = 13 + offset
                attributes = self.color_selected if self.settings_selected == len(Quality) + offset else 0
                self._text(row, 4, f"[{key_name}] {self._fit(label, width - 8)}", attributes)
                self.settings_zones.append((row, 4, max(4, width - 3), len(Quality) + offset))
            self.settings_action_zones = []
            self._button(18, 4, "SAVE", (curses.A_REVERSE | self.color_selected) if self.settings_selected == item_count else self.color_accent)
            self._button(19, 4, "BACK", (curses.A_REVERSE | self.color_selected) if self.settings_selected == item_count + 1 else curses.A_DIM)
            self.settings_action_zones.extend(((18, 4, 12, "save"), (19, 4, 12, "back")))
            self._footer("arrows move  enter toggle  click item  s save  b back")
            key, x, y, bstate = self._read_event()
            if key == getattr(curses, "KEY_MOUSE", -1):
                if self._mouse_wheel(bstate):
                    self.settings_selected = (self.settings_selected + (-1 if self._mouse_wheel_up(bstate) else 1)) % focus_count
                elif self._mouse_clicked(bstate):
                    command = self._hit_test(self.settings_action_zones, x, y)
                    if command == "save":
                        save_settings(self.settings)
                        self.message = "Settings saved"
                    elif command == "back":
                        return
                    clicked = self._hit_test(self.settings_zones, x, y)
                    if clicked is not None:
                        self.settings_selected = clicked
                        self._activate_setting(clicked)
                continue
            if key in (ord("b"), ord("B"), 27, curses.KEY_BACKSPACE, 127):
                return
            if key in (curses.KEY_UP, curses.KEY_LEFT, ord("k")):
                self.settings_selected = (self.settings_selected - 1) % focus_count
            elif key in (curses.KEY_DOWN, curses.KEY_RIGHT, ord("j")):
                self.settings_selected = (self.settings_selected + 1) % focus_count
            elif key == curses.KEY_HOME:
                self.settings_selected = 0
            elif key == curses.KEY_END:
                self.settings_selected = focus_count - 1
            elif key == curses.KEY_PPAGE:
                self.settings_selected = max(0, self.settings_selected - 4)
            elif key == curses.KEY_NPAGE:
                self.settings_selected = min(focus_count - 1, self.settings_selected + 4)
            elif key in (getattr(curses, "KEY_BTAB", -1), 353):
                self.settings_selected = (self.settings_selected - 1) % focus_count
            elif key == 9:
                self.settings_selected = (self.settings_selected + 1) % focus_count
            elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
                if self.settings_selected == item_count:
                    save_settings(self.settings)
                    self.message = "Settings saved"
                elif self.settings_selected == item_count + 1:
                    return
                else:
                    self._activate_setting(self.settings_selected)
            elif key in (ord("s"), ord("S")):

                save_settings(self.settings)
                self.message = "Settings saved"

    def _activate_setting(self, selected: int) -> None:
        quality_count = len(Quality)
        if selected < quality_count:
            self.settings.quality_audio = list(Quality)[selected]
            self.message = f"Quality: {QUALITY_LABELS[self.settings.quality_audio]}"
        elif selected == quality_count:
            self.settings.lyrics_embed = not self.settings.lyrics_embed
        elif selected == quality_count + 1:
            self.settings.lyrics_file = not self.settings.lyrics_file
        elif selected == quality_count + 2:
            value = self._prompt("Download folder: ")
            if value:
                self.settings.download_base_path = value
        elif selected == quality_count + 3:
            self.settings.skip_existing = not self.settings.skip_existing

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
