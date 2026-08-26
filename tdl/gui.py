"""Qt6 desktop interface for the Python TIDAL downloader."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .api import TidalApi
from .auth import TidalAuth
from .downloader import HiResDownloader
from .models import Quality, Settings
from .storage import load_settings, save_settings


class TaskWorker(QObject):
    """Run one blocking API/download operation away from the GUI thread."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, task: Any) -> None:
        super().__init__()
        self.task = task

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.task(self.progress.emit))
        except Exception as exc:
            self.failed.emit(str(exc))


class TaskBridge(QObject):
    """Deliver worker callbacks on the GUI thread.

    Connecting a Qt signal to a free lambda does not reliably provide a Qt
    receiver context in PySide. This bridge is a QObject living in the main
    thread, so widget mutations cannot happen from a download thread.
    """

    def __init__(self, done: Any, failed: Any, progress: Any | None, cleanup: Any) -> None:
        super().__init__()
        self.done_callback = done
        self.failed_callback = failed
        self.progress_callback = progress
        self.cleanup_callback = cleanup

    @Slot(object)
    def handle_finished(self, result: object) -> None:
        self.done_callback(result)

    @Slot(str)
    def handle_failed(self, message: str) -> None:
        self.failed_callback(message)

    @Slot(int, int, str)
    def handle_progress(self, current: int, total: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(current, total, message)

    @Slot()
    def handle_thread_finished(self) -> None:
        self.cleanup_callback()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.api = TidalApi()
        self.auth = TidalAuth(self.api)
        self._threads: list[QThread] = []
        self._workers: list[TaskWorker] = []
        self._bridges: list[TaskBridge] = []
        self._reported_download_paths: set[str] = set()
        self.setWindowTitle("tdl | TIDAL Hi-Res Downloader")
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)
        self._build_ui()
        self._refresh_account()
        # Session validation is network I/O and is triggered lazily by the
        # first API operation; local token state is enough for initial UI state.

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(20, 24, 20, 20)
        logo = QLabel("tdl")
        logo.setObjectName("logo")
        side_layout.addWidget(logo)
        subtitle = QLabel("TIDAL audio library")
        subtitle.setObjectName("subtitle")
        side_layout.addWidget(subtitle)
        side_layout.addSpacing(30)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for label in ("Download", "Search", "Account", "Settings"):
            self.nav.addItem(QListWidgetItem(label))
        side_layout.addWidget(self.nav)
        side_layout.addStretch()
        legal = QLabel("Personal use only. Respect copyright and TIDAL terms.")
        legal.setWordWrap(True)
        legal.setObjectName("muted")
        side_layout.addWidget(legal)

        self.pages = QStackedWidget()
        self.download_page = self._build_download_page()
        self.search_page = self._build_search_page()
        self.account_page = self._build_account_page()
        self.settings_page = self._build_settings_page()
        for page in (self.download_page, self.search_page, self.account_page, self.settings_page):
            self.pages.addWidget(page)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

        refresh = QAction("Refresh session", self)
        refresh.triggered.connect(self._refresh_account)
        self.menuBar().addAction(refresh)

    def _build_download_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 28, 34, 28)
        title = QLabel("Download")
        title.setObjectName("page-title")
        layout.addWidget(title)
        intro = QLabel("Paste a TIDAL track, album, or playlist URL. Hi-Res Lossless downloads use PKCE credentials.")
        intro.setObjectName("muted")
        layout.addWidget(intro)

        row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://tidal.com/browse/track/...")
        self.url_input.returnPressed.connect(self.start_download)
        row.addWidget(self.url_input, 1)
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self.start_download)
        row.addWidget(self.download_button)
        layout.addLayout(row)

        quality_box = QGroupBox("Audio quality")
        quality_layout = QGridLayout(quality_box)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("Low 96 kbps", Quality.LOW96K)
        self.quality_combo.addItem("High 320 kbps", Quality.LOW320K)
        self.quality_combo.addItem("Lossless", Quality.LOSSLESS)
        self.quality_combo.addItem("Hi-Res Lossless", Quality.HIRES)
        self.quality_combo.setCurrentIndex(self.quality_combo.findData(self.settings.quality_audio.value))
        quality_layout.addWidget(QLabel("Stream"), 0, 0)
        quality_layout.addWidget(self.quality_combo, 0, 1)
        self.pkce_hint = QLabel("PKCE is required for Hi-Res Lossless")
        self.pkce_hint.setObjectName("accent")
        quality_layout.addWidget(self.pkce_hint, 0, 2)
        layout.addWidget(quality_box)

        queue_box = QGroupBox("Download queue")
        queue_layout = QVBoxLayout(queue_box)
        self.queue = QTableWidget(0, 3)
        self.queue.setHorizontalHeaderLabels(("Item", "Progress", "Status"))
        self.queue.horizontalHeader().setStretchLastSection(True)
        self.queue.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        queue_layout.addWidget(self.queue)
        layout.addWidget(queue_box, 1)

        history_box = QGroupBox("Saved files")
        history_layout = QVBoxLayout(history_box)
        self.download_log = QTextEdit()
        self.download_log.setReadOnly(True)
        self.download_log.setPlaceholderText("Saved file paths will appear here")
        self.download_log.setMinimumHeight(90)
        self.download_log.setMaximumHeight(150)
        self.download_log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        history_layout.addWidget(self.download_log)
        layout.addWidget(history_box)
        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        return page

    def _build_search_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 28, 34, 28)
        title = QLabel("Search TIDAL")
        title.setObjectName("page-title")
        layout.addWidget(title)
        row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Artist, album, or track")
        self.search_input.returnPressed.connect(self.search)
        row.addWidget(self.search_input, 1)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self.search)
        row.addWidget(search_button)
        layout.addLayout(row)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(("Type", "Title", "Artist", "Action"))
        self.results.setWordWrap(False)
        self.results.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Match qBittorrent-style result tables: the table follows the window
        # width, while users can drag the boundaries between columns.
        header = self.results.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionsMovable(False)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(1)
        self._search_columns_initialized = False
        layout.addWidget(self.results, 1)
        # Apply initial widths after the parent layout has calculated the table
        # geometry without resetting widths after subsequent searches.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._fix_search_columns)
        return page

    def _build_account_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 28, 34, 28)
        title = QLabel("Account")
        title.setObjectName("page-title")
        layout.addWidget(title)
        self.account_status = QLabel()
        self.account_status.setObjectName("account-status")
        layout.addWidget(self.account_status)
        card = QGroupBox("Authentication")
        card_layout = QVBoxLayout(card)
        self.device_login_button = QPushButton("Login with device authorization")
        self.device_login_button.clicked.connect(self.device_login)
        card_layout.addWidget(self.device_login_button)
        self.pkce_login_button = QPushButton("Login with PKCE for Hi-Res")
        self.pkce_login_button.setObjectName("primary")
        self.pkce_login_button.clicked.connect(self.pkce_login)
        card_layout.addWidget(self.pkce_login_button)
        self.logout_button = QPushButton("Log out")
        self.logout_button.clicked.connect(self.logout)
        card_layout.addWidget(self.logout_button)
        layout.addWidget(card)
        self.pkce_url = QTextEdit()
        self.pkce_url.setReadOnly(True)
        self.pkce_url.setPlaceholderText("PKCE authorization URL appears here")
        self.pkce_url.setMaximumHeight(80)
        layout.addWidget(self.pkce_url)
        redirect_row = QHBoxLayout()
        self.redirect_input = QLineEdit()
        self.redirect_input.setPlaceholderText("Paste the full redirect URL after browser login")
        redirect_row.addWidget(self.redirect_input, 1)
        self.pkce_submit = QPushButton("Complete PKCE")
        self.pkce_submit.clicked.connect(self.pkce_submit_clicked)
        redirect_row.addWidget(self.pkce_submit)
        layout.addLayout(redirect_row)
        layout.addStretch()
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(34, 28, 34, 28)
        title = QLabel("Settings")
        title.setObjectName("page-title")
        outer.addWidget(title)
        subtitle = QLabel("All download, quality, artwork, metadata, and file options are stored in ~/.tdl/settings.json.")
        subtitle.setObjectName("muted")
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 12, 12, 12)
        layout.setSpacing(14)

        audio_box = QGroupBox("Quality")
        audio_form = QFormLayout(audio_box)
        self.settings_quality = QComboBox()
        for label, quality in (("Low 96 kbps", Quality.LOW96K), ("High 320 kbps", Quality.LOW320K),
                               ("Lossless", Quality.LOSSLESS), ("Hi-Res Lossless", Quality.HIRES)):
            self.settings_quality.addItem(label, quality.value)
        self.settings_quality.setCurrentIndex(self.settings_quality.findData(self.settings.quality_audio.value))
        self.settings_quality.currentIndexChanged.connect(self._settings_quality_changed)
        audio_form.addRow("Audio quality", self.settings_quality)
        self.settings_video_quality = QComboBox()
        for label, quality in (("360p", "p360"), ("480p", "p480"), ("720p", "p720"), ("1080p", "p1080")):
            self.settings_video_quality.addItem(label, quality)
        self.settings_video_quality.setCurrentIndex(self.settings_video_quality.findData(self.settings.quality_video.value))
        audio_form.addRow("Video quality", self.settings_video_quality)
        self.settings_extract_flac = QCheckBox("Extract FLAC from compatible Hi-Res containers")
        self.settings_extract_flac.setChecked(self.settings.extract_flac)
        audio_form.addRow("Hi-Res output", self.settings_extract_flac)
        layout.addWidget(audio_box)

        files_box = QGroupBox("Files and folders")
        files_form = QFormLayout(files_box)
        self.base_path = QLineEdit(self.settings.download_base_path)
        browse = QPushButton("Browse")
        browse.clicked.connect(self.choose_directory)
        path_row = QHBoxLayout()
        path_row.addWidget(self.base_path, 1)
        path_row.addWidget(browse)
        files_form.addRow("Download folder", path_row)
        self.skip_existing = QCheckBox("Skip files that already exist")
        self.skip_existing.setChecked(self.settings.skip_existing)
        files_form.addRow("Existing files", self.skip_existing)
        self.track_num_pad_zero = QCheckBox("Pad track numbers with zeroes")
        self.track_num_pad_zero.setChecked(self.settings.track_num_pad_zero)
        files_form.addRow("Track names", self.track_num_pad_zero)
        self.playlist_folder = QCheckBox("Use a separate Playlists folder")
        self.playlist_folder.setChecked(self.settings.playlist_folder)
        files_form.addRow("Playlists", self.playlist_folder)
        self.playlist_format = QComboBox()
        self.playlist_format.addItems(["m3u8", "m3u"])
        self.playlist_format.setCurrentText(self.settings.playlist_format)
        files_form.addRow("Playlist format", self.playlist_format)
        self.video_download = QCheckBox("Allow video downloads")
        self.video_download.setChecked(self.settings.video_download)
        files_form.addRow("Video", self.video_download)
        self.video_convert_mp4 = QCheckBox("Convert downloaded video to MP4")
        self.video_convert_mp4.setChecked(self.settings.video_convert_mp4)
        files_form.addRow("Video output", self.video_convert_mp4)
        self.ffmpeg = QLineEdit(self.settings.ffmpeg_path)
        self.ffmpeg.setPlaceholderText("ffmpeg from PATH")
        files_form.addRow("FFmpeg path", self.ffmpeg)
        layout.addWidget(files_box)

        art_box = QGroupBox("Artwork, lyrics, and metadata")
        art_form = QFormLayout(art_box)
        self.cover_dimension = QComboBox()
        self.cover_dimension.addItems(["80x80", "160x160", "320x320", "640x640", "1280x1280"])
        self.cover_dimension.setCurrentText(self.settings.cover_dimension)
        art_form.addRow("Cover size", self.cover_dimension)
        self.metadata_cover_embed = QCheckBox("Embed cover in the audio file")
        self.metadata_cover_embed.setChecked(self.settings.metadata_cover_embed)
        art_form.addRow("Cover in song", self.metadata_cover_embed)
        self.cover_album_file = QCheckBox("Save cover.jpg next to the album")
        self.cover_album_file.setChecked(self.settings.cover_album_file)
        art_form.addRow("Cover file", self.cover_album_file)
        self.lyrics_embed = QCheckBox("Embed lyrics in the audio file")
        self.lyrics_embed.setChecked(self.settings.lyrics_embed)
        art_form.addRow("Lyrics in song", self.lyrics_embed)
        self.lyrics_file = QCheckBox("Save lyrics as an .lrc file")
        self.lyrics_file.setChecked(self.settings.lyrics_file)
        art_form.addRow("Lyrics file", self.lyrics_file)
        self.metadata_replay_gain = QCheckBox("Write ReplayGain metadata")
        self.metadata_replay_gain.setChecked(self.settings.metadata_replay_gain)
        art_form.addRow("ReplayGain", self.metadata_replay_gain)
        layout.addWidget(art_box)

        network_box = QGroupBox("Download limits")
        network_form = QFormLayout(network_box)
        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 64)
        self.concurrent.setValue(self.settings.downloads_simultaneous_per_track_max)
        network_form.addRow("Segments per song", self.concurrent)
        self.downloads_concurrent = QSpinBox()
        self.downloads_concurrent.setRange(1, 32)
        self.downloads_concurrent.setValue(self.settings.downloads_concurrent_max)
        network_form.addRow("Songs at once", self.downloads_concurrent)
        self.download_delay = QCheckBox("Pause between songs")
        self.download_delay.setChecked(self.settings.download_delay)
        network_form.addRow("Download delay", self.download_delay)
        self.delay_min = QDoubleSpinBox()
        self.delay_min.setRange(0, 3600)
        self.delay_min.setDecimals(1)
        self.delay_min.setValue(self.settings.download_delay_sec_min)
        network_form.addRow("Minimum delay (sec)", self.delay_min)
        self.delay_max = QDoubleSpinBox()
        self.delay_max.setRange(0, 3600)
        self.delay_max.setDecimals(1)
        self.delay_max.setValue(self.settings.download_delay_sec_max)
        network_form.addRow("Maximum delay (sec)", self.delay_max)
        layout.addWidget(network_box)

        misc_box = QGroupBox("Other")
        misc_form = QFormLayout(misc_box)
        self.symlink_to_track = QCheckBox("Create a track symlink when supported")
        self.symlink_to_track.setChecked(self.settings.symlink_to_track)
        misc_form.addRow("Links", self.symlink_to_track)
        self.log_level = QComboBox()
        self.log_level.addItems(["error", "warn", "info", "debug", "trace"])
        self.log_level.setCurrentText(self.settings.log_level)
        misc_form.addRow("Log level", self.log_level)
        layout.addWidget(misc_box)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        save_button = QPushButton("Save settings")
        save_button.setObjectName("primary")
        save_button.clicked.connect(self.save_settings)
        outer.addWidget(save_button)
        return page

    def _run(self, task: Any, done: Any, progress: Any | None = None, failed: Any | None = None) -> None:
        thread = QThread(self)
        worker = TaskWorker(task)
        bridge = TaskBridge(done, failed or self._show_error, progress, lambda: self._forget_worker(thread, worker, bridge))
        bridge.setParent(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(bridge.handle_finished)
        worker.failed.connect(bridge.handle_failed)
        worker.progress.connect(bridge.handle_progress)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(bridge.handle_thread_finished)
        self._threads.append(thread)
        self._workers.append(worker)
        self._bridges.append(bridge)
        thread.start()

    def _forget_worker(self, thread: QThread, worker: TaskWorker, bridge: TaskBridge) -> None:
        if thread in self._threads:
            self._threads.remove(thread)
        if worker in self._workers:
            self._workers.remove(worker)
        if bridge in self._bridges:
            self._bridges.remove(bridge)
        bridge.deleteLater()

    def closeEvent(self, event: Any) -> None:
        # Do not let QObject parents destroy active workers while their thread
        # is still executing; wait for each thread to finish first.
        for thread in list(self._threads):
            thread.quit()
            # The worker may be inside a blocking HTTP request. Destroying a
            # running QThread after a fixed timeout can crash Qt on exit, so
            # wait until the worker has actually returned.
            thread.wait()
        self._threads.clear()
        self._workers.clear()
        self._bridges.clear()
        super().closeEvent(event)

    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 10000)
        QMessageBox.critical(self, "tdl", message)

    def _set_logged_in(self, logged_in: bool) -> None:
        if logged_in:
            mode = "PKCE / Hi-Res enabled" if self.auth.token.is_pkce else "Device authorization"
            self.account_status.setText(f"Authenticated · user {self.auth.token.user_id or 'unknown'} · {mode}")
        else:
            self.account_status.setText("Not authenticated. Use PKCE to unlock Hi-Res Lossless downloads.")
        self.pkce_login_button.setEnabled(True)

    def _refresh_account(self) -> None:
        self._set_logged_in(bool(self.auth.token.valid or self.auth.token.restorable))

    def _quality_changed(self) -> None:
        quality = Quality(self.quality_combo.currentData())
        self.pkce_hint.setVisible(quality is Quality.HIRES)
        if quality is Quality.HIRES and not self.auth.token.is_pkce:
            self.statusBar().showMessage("Hi-Res requires PKCE authentication", 6000)

    def _settings_quality_changed(self) -> None:
        quality = Quality(self.settings_quality.currentData())
        self.quality_combo.setCurrentIndex(self.quality_combo.findData(quality.value))
        self.pkce_hint.setVisible(quality is Quality.HIRES)
        if quality is Quality.HIRES and not self.auth.token.is_pkce:
            self.statusBar().showMessage("Hi-Res requires PKCE authentication", 6000)

    def device_login(self) -> None:
        self.device_login_button.setEnabled(False)
        self._run(lambda emit: self.auth.login_device(lambda url, code: emit(0, 0, f"Open {url} and enter {code}")),
                  lambda _result: (self.device_login_button.setEnabled(True), self._set_logged_in(True)),
                  lambda _current, _total, message: self.statusBar().showMessage(message, 10000),
                  lambda message: (self.device_login_button.setEnabled(True), self._show_error(message)))

    def pkce_login(self) -> None:
        try:
            url, verifier, unique_key = self.auth.build_pkce_url()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._pkce_verifier = verifier
        self._pkce_unique_key = unique_key
        self.pkce_url.setPlainText(url)
        QDesktopServices.openUrl(QUrl(url))
        self.statusBar().showMessage("Complete login in the browser, then paste the redirect URL below")

    def pkce_submit_clicked(self) -> None:
        redirect = self.redirect_input.text().strip()
        if not redirect or not hasattr(self, "_pkce_verifier"):
            self._show_error("Start PKCE login first and paste the redirect URL")
            return
        self.pkce_submit.setEnabled(False)
        self._run(lambda _emit: self.auth.exchange_pkce(redirect, self._pkce_verifier, self._pkce_unique_key),
                  lambda _result: (self.pkce_submit.setEnabled(True), self._set_logged_in(True), self.statusBar().showMessage("PKCE login successful")),
                  lambda _current, _total, message: self.statusBar().showMessage(message, 10000),
                  lambda message: (self.pkce_submit.setEnabled(True), self._show_error(message)))

    def logout(self) -> None:
        self.auth.logout()
        self._set_logged_in(False)

    def start_download(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            return
        quality = Quality(self.quality_combo.currentData())
        if quality is Quality.HIRES and not self.auth.token.is_pkce:
            QMessageBox.warning(self, "Hi-Res requires PKCE", "Authenticate with PKCE on the Account page before downloading Hi-Res Lossless.")
            self.nav.setCurrentRow(2)
            return
        self.settings.quality_audio = quality
        row = self.queue.rowCount()
        self.queue.insertRow(row)
        self.queue.setItem(row, 0, QTableWidgetItem(url))
        progress = QProgressBar()
        progress.setRange(0, 100)
        self.queue.setCellWidget(row, 1, progress)
        self.queue.setItem(row, 2, QTableWidgetItem("Starting"))
        self.download_button.setEnabled(False)
        def update(current: int, total: int, message: str) -> None:
            value = int(current * 100 / total) if total else 0
            self._update_queue(row, value, message)
            if message.startswith(("Saved: ", "Skipped existing: ")):
                self._append_download_path(message.removeprefix("Skipped existing: ").split(" (already existed)", 1)[0].removeprefix("Saved: "))
        self._run(lambda emit: self._download_with_restore(url, emit),
                  lambda result: self._download_done(row, result), update,
                  lambda message: self._download_failed(row, message))

    def _download_with_restore(self, url: str, emit: Any) -> list[Path]:
        if not self.auth.restore():
            raise RuntimeError("Session expired. Log in before downloading.")
        return HiResDownloader(self.api, self.settings, emit).download_url(url)

    def _update_queue(self, row: int, value: int, message: str) -> None:
        if row >= self.queue.rowCount():
            return
        widget = self.queue.cellWidget(row, 1)
        if isinstance(widget, QProgressBar):
            widget.setValue(value)
        item = self.queue.item(row, 2)
        if item:
            item.setText(message)

    def _append_download_path(self, path: str) -> None:
        path = path.strip()
        if not path or path in self._reported_download_paths:
            return
        self._reported_download_paths.add(path)
        print(f"Saved: {path}", flush=True)
        self.download_log.append(path)
        self.download_log.ensureCursorVisible()

    def _download_failed(self, row: int, message: str) -> None:
        self.download_button.setEnabled(True)
        self._update_queue(row, 0, f"Failed: {message}")
        self.download_log.append(f"Failed: {message}")
        self.download_log.ensureCursorVisible()
        self.statusBar().showMessage(message, 10000)
        QMessageBox.critical(self, "Download failed", message)

    def _download_done(self, row: int, result: list[Path]) -> None:
        self.download_button.setEnabled(True)
        self._update_queue(row, 100, f"Saved {len(result)} item(s)")
        self.url_input.clear()
        for path in result:
            self._append_download_path(str(path))
        saved_paths = "\n".join(str(path) for path in result)
        self.statusBar().showMessage(f"Saved: {saved_paths}" if saved_paths else "Download complete", 15000)

    def search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        self.results.setRowCount(0)
        self._run(lambda _emit: self.api.search(query), self._render_results)

    def _fix_search_columns(self) -> None:
        if self._search_columns_initialized:
            return
        header = self.results.horizontalHeader()
        widths = (64, 150, 96, 88)
        for column, width in enumerate(widths):
            header.resizeSection(column, width)
        self._search_columns_initialized = True

    @staticmethod
    def _result_artist(item: dict) -> str:
        artists = item.get("artists") or []
        names = [artist.get("name", "") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        if names:
            return ", ".join(names)
        artist = item.get("artist") or item.get("author") or {}
        if isinstance(artist, dict):
            return artist.get("name", "") or artist.get("title", "")
        return str(artist) if artist else ""

    @staticmethod
    def _set_result_text(table: QTableWidget, row: int, column: int, value: Any, limit: int) -> None:
        full_text = str(value or "")
        display_text = full_text if len(full_text) <= limit else f"{full_text[:limit - 3]}..."
        item = QTableWidgetItem(display_text)
        if display_text != full_text:
            item.setToolTip(full_text)
        table.setItem(row, column, item)

    def _render_results(self, payload: dict) -> None:
        tracks = payload.get("tracks", {}).get("items", [])
        albums = payload.get("albums", {}).get("items", [])
        rows = [("Track", item.get("title", item.get("name", "")), self._result_artist(item), item.get("id")) for item in tracks]
        rows += [("Album", item.get("title", item.get("name", "")), self._result_artist(item), item.get("id")) for item in albums]
        for media_type, title, artist, media_id in rows:
            row = self.results.rowCount()
            self.results.insertRow(row)
            self._set_result_text(self.results, row, 0, media_type, 12)
            self._set_result_text(self.results, row, 1, title, 24)
            self._set_result_text(self.results, row, 2, artist, 18)
            button = QPushButton("Download")
            button.clicked.connect(lambda _checked=False, mt=media_type, mid=media_id: self._download_result(mt, mid))
            self.results.setCellWidget(row, 3, button)
        self._fix_search_columns()

    def _download_result(self, media_type: str, media_id: Any) -> None:
        self.url_input.setText(f"https://tidal.com/browse/{media_type.lower()}/{media_id}")
        self.nav.setCurrentRow(0)
        self.start_download()

    def choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose download folder", self.base_path.text())
        if directory:
            self.base_path.setText(directory)

    def save_settings(self) -> None:
        self.settings.download_base_path = self.base_path.text().strip() or "~/download"
        self.settings.skip_existing = self.skip_existing.isChecked()
        self.settings.track_num_pad_zero = self.track_num_pad_zero.isChecked()
        self.settings.playlist_folder = self.playlist_folder.isChecked()
        self.settings.playlist_format = self.playlist_format.currentText()
        self.settings.video_download = self.video_download.isChecked()
        self.settings.video_convert_mp4 = self.video_convert_mp4.isChecked()
        self.settings.extract_flac = self.settings_extract_flac.isChecked()
        self.settings.downloads_simultaneous_per_track_max = self.concurrent.value()
        self.settings.downloads_concurrent_max = self.downloads_concurrent.value()
        self.settings.download_delay = self.download_delay.isChecked()
        self.settings.download_delay_sec_min = self.delay_min.value()
        self.settings.download_delay_sec_max = self.delay_max.value()
        self.settings.ffmpeg_path = self.ffmpeg.text().strip()
        self.settings.quality_audio = Quality(self.settings_quality.currentData())
        self.settings.quality_video = type(self.settings.quality_video)(self.settings_video_quality.currentData())
        self.settings.cover_dimension = self.cover_dimension.currentText()
        self.settings.metadata_cover_embed = self.metadata_cover_embed.isChecked()
        self.settings.cover_album_file = self.cover_album_file.isChecked()
        self.settings.lyrics_embed = self.lyrics_embed.isChecked()
        self.settings.lyrics_file = self.lyrics_file.isChecked()
        self.settings.metadata_replay_gain = self.metadata_replay_gain.isChecked()
        self.settings.symlink_to_track = self.symlink_to_track.isChecked()
        self.settings.log_level = self.log_level.currentText()
        self.quality_combo.setCurrentIndex(self.quality_combo.findData(self.settings.quality_audio.value))
        save_settings(self.settings)
        self.statusBar().showMessage("Settings saved", 5000)


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("tdl")
    window = MainWindow()
    window.show()
    return app.exec()
