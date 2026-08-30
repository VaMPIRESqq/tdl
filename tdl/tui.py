"""Ncurses terminal interface for tdl.

The renderer deliberately redraws the complete dashboard from state on every
loop. This avoids stale cells and disappearing panels after terminal resize.
"""

from __future__ import annotations

import curses
import io
import logging
import threading
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
    """Compact ncurses dashboard with resize-safe rendering."""

    def __init__(self, screen: Any) -> None:
        self.screen = screen
        self.settings: Settings = load_settings()
        self.api = TidalApi()
        self.auth = TidalAuth(self.api)
        self.running = True
        self.message = "Ready"
        self.url = ""
        self.download_status = "IDLE"
        self.download_current = 0
        self.download_total = 0
        self.download_track = ""
        self.last_saved: list[str] = []
        self.session_files: list[Path] = []
        self.recent_output: list[str] = []
        self.downloads: dict[str, dict[str, Any]] = {}
        self.search_items: list[tuple[str, str, str, Any]] = []
        self.button_zones: list[tuple[int, int, int, str]] = []
        self._download_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._colors()

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(1000)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        while self.running:
            self._draw_dashboard()
            key = self.screen.getch()
            if key in (-1, curses.KEY_RESIZE):
                self.screen.clear()
                continue
            if key == getattr(curses, "KEY_MOUSE", -1):
                self._handle_mouse()
                continue
            self._handle_key(key)

    def _colors(self) -> None:
        self.title_attr = self.accent_attr = self.good_attr = self.error_attr = self.selected_attr = 0
        try:
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
                curses.init_pair(1, curses.COLOR_RED, -1)
                curses.init_pair(2, curses.COLOR_RED, -1)
                curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_RED)
                curses.init_pair(4, curses.COLOR_RED, -1)
                self.title_attr = curses.color_pair(1) | curses.A_BOLD
                self.accent_attr = curses.color_pair(1)
                self.good_attr = curses.color_pair(2)
                self.selected_attr = curses.color_pair(3) | curses.A_BOLD
                self.error_attr = curses.color_pair(4) | curses.A_BOLD
        except curses.error:
            pass

    def _handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q"), 27):
            self.running = False
        elif key in (ord("d"), ord("D"), 10, 13):
            self.download_url()
        elif key in (ord("s"), ord("S")):
            self.search()
        elif key in (ord("l"), ord("L")):
            self.login_pkce()
        elif key in (ord("c"), ord("C")):
            self.settings_menu()
        elif key in (ord("f"), ord("F")):
            self.show_library()

    def _draw_dashboard(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self._text(0, 1, "tdl", self.title_attr)
        state = "ONLINE" if (self.auth.token.valid or self.auth.token.restorable) else "OFFLINE"
        self._text(0, max(1, width - len(state) - 2), state, self.good_attr if state == "ONLINE" else self.error_attr)
        self._hline(1, width, self.accent_attr)
        self._text(2, 1, "TIDAL DOWNLOAD MONITOR", curses.A_DIM)
        if height < 12 or width < 60:
            self._draw_compact(height, width)
        else:
            self._draw_full(height, width)
        self._text(height - 1, 1, "ENTER/D download  S search  L login  C config  F files  Q quit", curses.A_DIM)
        self.screen.refresh()

    def _draw_buttons(self, row: int) -> None:
        labels = (("DOWNLOAD", "download"), ("SEARCH", "search"), ("LOGIN", "login"), ("SETTINGS", "settings"), ("FILES", "files"), ("QUIT", "quit"))
        self.button_zones = []
        column = 1
        for label, action in labels:
            text = f"[ {label} ]"
            if column + len(text) >= self._width() - 1:
                break
            self._text(row, column, text, self.selected_attr if action == "download" else self.accent_attr)
            self.button_zones.append((row, column, column + len(text) - 1, action))
            column += len(text) + 1

    def _handle_mouse(self) -> None:
        try:
            _event, x, y, _z, state = curses.getmouse()
        except curses.error:
            return
        clicked = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_RELEASED", 0)
        if not state & clicked:
            return
        for row, left, right, action in self.button_zones:
            if y == row and left <= x <= right:
                if action == "download":
                    self.download_url()
                elif action == "search":
                    self.search()
                elif action == "login":
                    self.login_pkce()
                elif action == "settings":
                    self.settings_menu()
                elif action == "files":
                    self.show_library()
                elif action == "quit":
                    self.running = False
                return

    def _draw_full(self, height: int, width: int) -> None:
        left = min(33, max(27, width // 3))
        right = width - left - 1
        top = 4
        panel_h = 10
        self._box(top, 0, panel_h, left, "SYSTEM")
        self._box(top, left + 1, panel_h, right, "DOWNLOAD PIPELINE")
        self._system_panel(top, left)
        self._pipeline_panel(top, left + 1, right)
        actions = top + panel_h + 1
        self._draw_buttons(actions)
        current = actions + 2
        self._box(current, 0, max(5, min(10, height - current - 5)), width, "CURRENT DOWNLOADS")
        row = current + 1
        for item in list(self.downloads.values())[: max(1, min(8, height - current - 7))]:
            line = f"{item['status']:<9} {item['current']:>4}/{item['total']:<4} {item['track']}"
            self._text(row, 2, self._fit(line, width - 4), self.good_attr if item['status'] == 'DONE' else self.accent_attr)
            row += 1
        if not self.downloads:
            self._text(row, 2, "No active downloads.", curses.A_DIM)
        recent = current + max(4, min(10, height - current - 5)) + 1
        self._box(recent, 0, max(3, height - recent - 2), width, "RECENT OUTPUT")
        recent_items = self.recent_output or self.last_saved
        for index, path in enumerate(recent_items[-max(1, height - recent - 4):][::-1], 1):
            self._text(recent + index, 2, self._fit(path, width - 4), self.good_attr)
        if not recent_items:
            self._text(recent + 2, 2, "No downloads in this session.", curses.A_DIM)

    def _draw_compact(self, height: int, width: int) -> None:
        self._text(4, 1, "SYSTEM", self.title_attr)
        logged = self.auth.token.valid or self.auth.token.restorable
        self._text(5, 2, f"Account: {'PKCE' if logged and self.auth.token.is_pkce else 'offline'}")
        self._text(6, 2, f"Quality: {QUALITY_LABELS[self.settings.quality_audio]}")
        self._text(8, 1, "PIPELINE", self.title_attr)
        self._text(9, 2, self._fit(f"{self.download_status}: {self.message}", width - 4))
        if height > 11:
            self._text(10, 2, self._bar(self.download_current, self.download_total, width - 4), self.accent_attr)

    def _system_panel(self, row: int, width: int) -> None:
        logged = self.auth.token.valid or self.auth.token.restorable
        values = [
            f"ACCOUNT   {'PKCE' if logged and self.auth.token.is_pkce else 'OFFLINE'}",
            f"QUALITY   {QUALITY_LABELS[self.settings.quality_audio]}",
            f"TRACKS    {'ASYNC' if self.settings.async_downloads else 'SYNC'} / {self.settings.downloads_concurrent_max}",
            f"SEGMENTS  {self.settings.downloads_simultaneous_per_track_max}",
            f"SKIP      {'ON' if self.settings.skip_existing else 'OFF'}",
        ]
        for index, value in enumerate(values, 1):
            self._text(row + index, 2, self._fit(value, width - 4))

    def _pipeline_panel(self, row: int, col: int, width: int) -> None:
        self._text(row + 1, col + 2, self.download_status, self.error_attr if self.download_status == "ERROR" else self.good_attr)
        self._text(row + 2, col + 2, self._fit(self.message, width - 4))
        self._text(row + 4, col + 2, self._bar(self.download_current, self.download_total, width - 4), self.accent_attr)
        detail = f"{self.download_current}/{self.download_total}" if self.download_total else "waiting"
        self._text(row + 5, col + 2, detail, curses.A_DIM)
        self._text(row + 7, col + 2, self._fit(self.download_track or "No active track", width - 4), self.accent_attr)

    def _box(self, row: int, col: int, height: int, width: int, title: str) -> None:
        if height < 2 or width < 3:
            return
        width = min(width, self._width() - col)
        self._text(row, col, "╭" + "─" * max(1, width - 2) + "╮", self.accent_attr)
        self._text(row, col + 2, f" {self._fit(title, width - 6)} ", self.title_attr)
        for current in range(row + 1, min(row + height - 1, self._height() - 1)):
            self._text(current, col, "│", self.accent_attr)
            self._text(current, col + width - 1, "│", self.accent_attr)
        if row + height - 1 < self._height():
            self._text(row + height - 1, col, "╰" + "─" * max(1, width - 2) + "╯", self.accent_attr)

    def _text(self, row: int, col: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if not (0 <= row < height and 0 <= col < width):
            return
        try:
            self.screen.addnstr(row, col, text, max(1, width - col - 1), attr)
        except curses.error:
            pass

    def _hline(self, row: int, width: int, attr: int = 0) -> None:
        self._text(row, 0, "─" * max(1, width - 1), attr)

    def _width(self) -> int:
        return self.screen.getmaxyx()[1]

    def _height(self) -> int:
        return self.screen.getmaxyx()[0]

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if width <= 0:
            return ""
        return text if len(text) <= width else (text[: max(0, width - 3)] + "..." if width >= 4 else text[:width])

    @staticmethod
    def _bar(current: int, total: int, width: int) -> str:
        width = max(3, width)
        inner = width - 2
        filled = inner if total and current >= total else int(inner * current / total) if total else 0
        return "[" + "█" * max(0, filled) + "·" * max(0, inner - filled) + "]"

    @staticmethod
    def _progress_bar(current: int, total: int, width: int) -> str:
        return TuiApp._bar(current, total, width).replace("█", "#").replace("·", "-")

    def _move_action(self, delta: int) -> None:
        # Compatibility with the previous curses TUI navigation helper.
        self.focused_action = (getattr(self, "focused_action", 0) + delta) % 5

    def _prompt(self, label: str) -> str:
        """Read a line without clearing it when Enter is pressed."""
        row = max(0, self._height() - 2)
        width = self._width()
        self._text(row, 0, " " * max(1, width - 1))
        self._text(row, 1, label, self.accent_attr)
        self.screen.refresh()
        curses.curs_set(1)
        curses.echo()
        try:
            value = self.screen.getstr(row, min(len(label) + 1, width - 2), max(1, width - len(label) - 3))
        except curses.error:
            value = b""
        finally:
            curses.noecho()
            curses.curs_set(0)
        return value.decode(errors="replace").strip()

    def _ensure_auth(self) -> bool:
        if self.auth.restore():
            return True
        self.message = "No active session; use L for PKCE login"
        return False

    def download_url(self) -> None:
        if self._download_thread and self._download_thread.is_alive():
            self.message = "A download is already running"
            return
        url = self._prompt("TIDAL URL: ")
        if not url or not self._ensure_auth():
            return
        self.url = url
        self.download_status = "RUNNING"
        self.download_current = self.download_total = 0
        self.download_track = "Resolving track..."
        self.message = "Connecting to TIDAL"
        with self._lock:
            self.downloads[url] = {"status": "RUNNING", "current": 0, "total": 0, "track": "Resolving track...", "paths": []}
        self._download_thread = threading.Thread(target=self._download, args=(url,), daemon=True)
        self._download_thread.start()

    def _download(self, url: str) -> None:
        def progress(current: int, total: int, message: str) -> None:
            with self._lock:
                self.download_current = current
                self.download_total = total
                self.message = message
                item = self.downloads.get(url)
                if item:
                    item["current"] = current
                    item["total"] = total
                    item["status"] = "RUNNING"
                if message and not message.startswith(("Downloading segments", "Saved:", "Completed", "Failed", "Skipped", "Connecting")):
                    self.download_track = message
                    self.message = f"Downloading: {message}"
                    if item:
                        item["track"] = message
                elif message.startswith("Saved:") and self.download_track == "Resolving track...":
                    self.download_track = message.removeprefix("Saved:").strip()
        try:
            with self._quiet_download():
                paths = HiResDownloader(self.api, self.settings, progress).download_url(url)
            with self._lock:
                self.download_status = "COMPLETE"
                self.message = f"Downloaded {len(paths)} file(s)"
                new_paths = [str(path) for path in paths if str(path) not in self.recent_output]
                self.recent_output.extend(new_paths)
                self.last_saved.extend(new_paths)
                self.recent_output.extend(path for path in new_paths if path not in self.recent_output)
                self.session_files.extend(path for path in paths if path not in self.session_files)
                item = self.downloads.get(url)
                if item:
                    item["status"] = "DONE"
                    item["paths"] = new_paths
                    item["track"] = new_paths[-1] if new_paths else item["track"]
        except Exception as exc:
            with self._lock:
                self.download_status = "ERROR"
                self.message = str(exc)
                item = self.downloads.get(url)
                if item:
                    item["status"] = "ERROR"
                    item["track"] = str(exc)

    def search(self) -> None:
        query = self._prompt("Search: ")
        if not query or not self._ensure_auth():
            return
        try:
            payload = self.api.search(query)
            self.search_items = []
            for item in payload.get("tracks", {}).get("items", []):
                self.search_items.append(("track", item.get("title", item.get("name", "")), self._artist(item), item.get("id")))
            for item in payload.get("albums", {}).get("items", []):
                self.search_items.append(("album", item.get("title", item.get("name", "")), self._artist(item), item.get("id")))
            self.message = f"Found {len(self.search_items)} result(s)"
        except Exception as exc:
            self.message = f"Search failed: {exc}"

    def login_pkce(self) -> None:
        try:
            self._with_terminal(lambda: self.auth.login_for_cli(pkce=True))
            self.message = "PKCE login successful"
        except Exception as exc:
            self.message = f"Login failed: {exc}"

    def _with_terminal(self, callback: Any) -> Any:
        curses.def_prog_mode()
        curses.endwin()
        try:
            return callback()
        finally:
            curses.reset_prog_mode()
            self.screen.clear()
            self.screen.refresh()

    def settings_menu(self) -> None:
        """Open a separate, resize-safe settings window."""
        draft = Settings.from_dict(self.settings.to_dict())
        fields = ["quality_audio", "async_downloads", "downloads_concurrent_max", "skip_existing", "download_base_path", "download_delay"]
        selected = 0
        window = self._settings_window()
        window.keypad(True)
        window.timeout(1000)
        try:
            while True:
                self._draw_settings_window(window, draft, fields, selected)
                key = window.getch()
                if key in (-1, curses.KEY_RESIZE):
                    window.erase()
                    continue
                if key in (27, ord("q"), ord("Q")):
                    self.message = "Settings cancelled"
                    return
                if key in (curses.KEY_UP, ord("k")):
                    selected = (selected - 1) % len(fields)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selected = (selected + 1) % len(fields)
                elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord(" ")):
                    self._change_setting(draft, fields[selected], 1 if key in (curses.KEY_RIGHT, ord(" ")) else -1)
                elif key in (10, 13):
                    if selected == len(fields) - 1:
                        self.settings = draft
                        save_settings(self.settings)
                        self.message = "Settings saved"
                        return
                    self._change_setting(draft, fields[selected], 1)
                elif key in (ord("s"), ord("S")):
                    self.settings = draft
                    save_settings(self.settings)
                    self.message = "Settings saved"
                    return
        finally:
            del window
            self.screen.clear()

    def _settings_window(self) -> Any:
        height, width = self.screen.getmaxyx()
        win_h = min(max(14, height - 4), height)
        win_w = min(max(64, width - 6), width)
        top = max(0, (height - win_h) // 2)
        left = max(0, (width - win_w) // 2)
        return curses.newwin(win_h, win_w, top, left)

    def _draw_settings_window(self, window: Any, settings: Settings, fields: list[str], selected: int) -> None:
        window.erase()
        height, width = window.getmaxyx()
        self._window_box(window, height, width, "SETTINGS")
        values = [
            ("Audio quality", QUALITY_LABELS[settings.quality_audio]),
            ("Async downloads", "ON" if settings.async_downloads else "OFF"),
            ("Track workers", str(settings.downloads_concurrent_max)),
            ("Skip existing", "ON" if settings.skip_existing else "OFF"),
            ("Download folder", settings.download_base_path),
            ("Delay between tracks", "ON" if settings.download_delay else "OFF"),
        ]
        for index, (label, value) in enumerate(values):
            attr = curses.A_REVERSE | self.selected_attr if index == selected else 0
            text = f"  {label:<24} {value}"
            self._window_text(window, 3 + index, 2, self._fit(text, width - 4), attr)
        self._window_text(window, height - 3, 2, "↑/↓ select  ←/→ change  Enter change/save  S save  Esc cancel", curses.A_DIM)
        window.refresh()

    @staticmethod
    def _window_box(window: Any, height: int, width: int, title: str) -> None:
        try:
            window.border()
            window.addnstr(0, 2, f" {title} ", max(1, width - 4), curses.A_BOLD)
        except curses.error:
            pass

    @staticmethod
    def _window_text(window: Any, row: int, col: int, value: str, attr: int = 0) -> None:
        try:
            height, width = window.getmaxyx()
            if 0 <= row < height and 0 <= col < width:
                window.addnstr(row, col, value, max(1, width - col - 1), attr)
        except curses.error:
            pass

    def _change_setting(self, settings: Settings, field: str, direction: int) -> None:
        if field == "quality_audio":
            values = list(Quality)
            settings.quality_audio = values[(values.index(settings.quality_audio) + direction) % len(values)]
        elif field in {"async_downloads", "skip_existing", "download_delay"}:
            setattr(settings, field, not getattr(settings, field))
        elif field == "downloads_concurrent_max":
            settings.downloads_concurrent_max = max(1, min(32, settings.downloads_concurrent_max + direction))
        elif field == "download_base_path":
            value = self._prompt("Download folder (Enter confirms): ")
            if value:
                settings.download_base_path = value


    def show_library(self) -> None:
        self._draw_dashboard()
        self._text(max(4, self._height() - 4), 1, "FILES", self.title_attr)
        files = [path for path in self.session_files if path.is_file()]
        for index, path in enumerate(files[: max(1, self._height() - 7)], 1):
            info = load_downloaded_file_info(path, self.settings.ffmpeg_path)
            self._text(max(4, self._height() - 4) + index, 2, self._fit(f"{path.name} | {info.size_display} | {info.technical_display}", self._width() - 4))
        self._text(self._height() - 1, 1, "Press any key to return", curses.A_DIM)
        self.screen.refresh()
        self.screen.getch()

    @staticmethod
    def _artist(item: dict[str, Any]) -> str:
        artists = item.get("artists") or []
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        return ", ".join(names) or str((item.get("artist") or {}).get("name", ""))

    @contextmanager
    def _quiet_download(self):
        previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                yield
        finally:
            logging.disable(previous_disable)

    @staticmethod
    def _file_info_lines(info: DownloadedFileInfo, width: int) -> list[str]:
        lines = [f"Path: {info.path}", f"Size: {info.size_display}", f"Technical: {info.technical_display}"]
        if info.title or info.artist or info.album:
            lines.extend((f"Track: {info.artist or 'Unknown artist'} - {info.title or 'Unknown title'}", f"Album: {info.album or 'Unknown album'}"))
        return [TuiApp._fit(line, width) for line in lines]

    @staticmethod
    def _hit_test(zones: list[tuple[int, int, int, Any]], x: int | None, y: int | None) -> Any:
        if x is None or y is None:
            return None
        return next((action for row, left, right, action in zones if row == y and left <= x <= right), None)


def run_tui() -> int:
    curses.wrapper(lambda screen: TuiApp(screen).run())
    return 0
