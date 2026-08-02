#!/usr/bin/env python3
"""PySide6 GUI for downloading YouTube videos with yt-dlp.

Use only for videos you own, videos with permission, or content that is
otherwise legal for you to download.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import yt_dlp

from download_youtube import build_options


class DownloadCancelled(Exception):
    pass


class DownloadWorker(QThread):
    progress = Signal(float, str)
    log_message = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, url: str, output_dir: Path, audio_only: bool, playlist: bool):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.audio_only = audio_only
        self.playlist = playlist
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _progress_hook(self, status: dict) -> None:
        if self._cancelled:
            raise DownloadCancelled

        if status["status"] == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            downloaded = status.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0.0
            filename = Path(status.get("filename", "")).name
            self.progress.emit(percent, filename)
        elif status["status"] == "finished":
            self.log_message.emit(f"Downloaded: {Path(status.get('filename', '')).name}")

    def run(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            options = build_options(self.output_dir, self.audio_only, self.playlist)
            options["progress_hooks"] = [self._progress_hook]

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([self.url])
        except DownloadCancelled:
            self.failed.emit("Cancelled.")
        except Exception as exc:  # noqa: BLE001 - surface any yt-dlp failure to the UI
            self.failed.emit(str(exc))
        else:
            self.finished_ok.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Downloader")
        self.worker: DownloadWorker | None = None

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("YouTube video or playlist URL")

        self.output_dir_input = QLineEdit("downloads")
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_output_dir)

        self.audio_only_checkbox = QCheckBox("Audio only (MP3)")
        self.playlist_checkbox = QCheckBox("Download whole playlist")

        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self._start_download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_download)
        self.cancel_button.setEnabled(False)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        output_dir_row = QHBoxLayout()
        output_dir_row.addWidget(self.output_dir_input)
        output_dir_row.addWidget(browse_button)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self.download_button)
        buttons_row.addWidget(self.cancel_button)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("URL"))
        layout.addWidget(self.url_input)
        layout.addWidget(QLabel("Output directory"))
        layout.addLayout(output_dir_row)
        layout.addWidget(self.audio_only_checkbox)
        layout.addWidget(self.playlist_checkbox)
        layout.addLayout(buttons_row)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_view)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.resize(520, 480)

    def _browse_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select output directory")
        if directory:
            self.output_dir_input.setText(directory)

    def _start_download(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a YouTube URL.")
            return

        output_dir = Path(self.output_dir_input.text().strip() or "downloads")

        self.progress_bar.setValue(0)
        self.log_view.appendPlainText(f"Starting download: {url}")
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        self.worker = DownloadWorker(
            url,
            output_dir,
            self.audio_only_checkbox.isChecked(),
            self.playlist_checkbox.isChecked(),
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.log_message.connect(self.log_view.appendPlainText)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _cancel_download(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)

    def _on_progress(self, percent: float, filename: str) -> None:
        self.progress_bar.setValue(int(percent))
        if filename:
            self.setWindowTitle(f"YouTube Downloader - {filename}")

    def _reset_controls(self) -> None:
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.setWindowTitle("YouTube Downloader")

    def _on_finished_ok(self) -> None:
        self.progress_bar.setValue(100)
        self.log_view.appendPlainText("Done.")
        self._reset_controls()

    def _on_failed(self, message: str) -> None:
        self.log_view.appendPlainText(f"Error: {message}")
        self._reset_controls()


def main() -> None:
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
