#!/usr/bin/env python3
"""PySide6 GUI for tagging audio files with artist/album/album art/lyrics.

Drag a song or a directory of songs in, auto-fill their metadata from the
web, review/edit, then save the tags into the files.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tag_editor import Track, load_tracks, save_track
import tag_editor

ART_THUMBNAIL_SIZE = 150


class AutoFillWorker(QThread):
    track_updated = Signal(int)
    log_message = Signal(str)
    progress = Signal(int, int)

    def __init__(self, tracks: list[Track], indices: list[int]):
        super().__init__()
        self.tracks = tracks
        self.indices = indices
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        total = len(self.indices)
        for done, index in enumerate(self.indices, start=1):
            if self._cancelled:
                self.log_message.emit("Auto-fill cancelled.")
                break

            track = self.tracks[index]
            try:
                tag_editor.auto_fill(track)
            except Exception as exc:  # noqa: BLE001 - surface any lookup failure
                track.status = "error"
                self.log_message.emit(f"{track.path.name}: {exc}")
            else:
                self.log_message.emit(f"{track.path.name}: {track.status}")

            self.track_updated.emit(index)
            self.progress.emit(done, total)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Tag Editor")
        self.setAcceptDrops(True)

        self.tracks: list[Track] = []
        self.current_index: int = -1
        self.worker: AutoFillWorker | None = None

        self.track_list = QListWidget()
        self.track_list.currentRowChanged.connect(self._on_row_changed)

        self.title_input = QLineEdit()
        self.artist_input = QLineEdit()
        self.album_input = QLineEdit()
        self.track_number_input = QLineEdit()
        self.track_number_input.setPlaceholderText("e.g. 3 or 3/12")
        self.genre_input = QLineEdit()
        self.year_input = QLineEdit()
        self.year_input.setPlaceholderText("e.g. 2024")

        self.apply_artist_button = QPushButton("Apply to All")
        self.apply_artist_button.clicked.connect(lambda: self._apply_field_to_all("artist", self.artist_input.text()))
        self.apply_album_button = QPushButton("Apply to All")
        self.apply_album_button.clicked.connect(lambda: self._apply_field_to_all("album", self.album_input.text()))
        self.apply_track_total_button = QPushButton("Apply Total to All")
        self.apply_track_total_button.clicked.connect(self._apply_track_total_to_all)
        self.apply_genre_button = QPushButton("Apply to All")
        self.apply_genre_button.clicked.connect(lambda: self._apply_field_to_all("genre", self.genre_input.text()))
        self.apply_year_button = QPushButton("Apply to All")
        self.apply_year_button.clicked.connect(lambda: self._apply_field_to_all("year", self.year_input.text()))

        self.art_label = QLabel("No artwork")
        self.art_label.setFixedSize(ART_THUMBNAIL_SIZE, ART_THUMBNAIL_SIZE)
        self.art_label.setStyleSheet("border: 1px solid gray;")
        self.art_label.setScaledContents(True)
        browse_art_button = QPushButton("Browse...")
        browse_art_button.clicked.connect(self._browse_album_art)
        self.save_art_button = QPushButton("Save As...")
        self.save_art_button.clicked.connect(self._save_album_art_as)
        self.apply_art_button = QPushButton("Apply to All")
        self.apply_art_button.clicked.connect(self._apply_album_art_to_all)

        self.lyrics_edit = QPlainTextEdit()
        self.lrc_sidecar_checkbox = QCheckBox("Also write .lrc sidecar file")
        self.rename_checkbox = QCheckBox("Rename file to match title")

        self.auto_fill_selected_button = QPushButton("Auto-Fill (Selected)")
        self.auto_fill_selected_button.clicked.connect(self._auto_fill_selected)
        self.auto_fill_all_button = QPushButton("Auto-Fill (All)")
        self.auto_fill_all_button.clicked.connect(self._auto_fill_all)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_auto_fill)
        self.cancel_button.setEnabled(False)

        self.save_selected_button = QPushButton("Save (Selected)")
        self.save_selected_button.clicked.connect(self._save_selected)
        self.save_all_button = QPushButton("Save (All)")
        self.save_all_button.clicked.connect(self._save_all)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(100)

        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Drop songs or a folder here"))
        left_layout.addWidget(self.track_list)

        artist_row = QHBoxLayout()
        artist_row.addWidget(self.artist_input)
        artist_row.addWidget(self.apply_artist_button)

        album_row = QHBoxLayout()
        album_row.addWidget(self.album_input)
        album_row.addWidget(self.apply_album_button)

        track_number_row = QHBoxLayout()
        track_number_row.addWidget(self.track_number_input)
        track_number_row.addWidget(self.apply_track_total_button)

        genre_row = QHBoxLayout()
        genre_row.addWidget(self.genre_input)
        genre_row.addWidget(self.apply_genre_button)

        year_row = QHBoxLayout()
        year_row.addWidget(self.year_input)
        year_row.addWidget(self.apply_year_button)

        art_row = QHBoxLayout()
        art_row.addWidget(self.art_label)
        art_row.addWidget(browse_art_button)
        art_row.addWidget(self.save_art_button)
        art_row.addWidget(self.apply_art_button)
        art_row.addStretch()

        auto_fill_row = QHBoxLayout()
        auto_fill_row.addWidget(self.auto_fill_selected_button)
        auto_fill_row.addWidget(self.auto_fill_all_button)
        auto_fill_row.addWidget(self.cancel_button)

        save_row = QHBoxLayout()
        save_row.addWidget(self.save_selected_button)
        save_row.addWidget(self.save_all_button)

        middle_layout = QVBoxLayout()
        middle_layout.addWidget(QLabel("Title"))
        middle_layout.addWidget(self.title_input)
        middle_layout.addWidget(QLabel("Artist"))
        middle_layout.addLayout(artist_row)
        middle_layout.addWidget(QLabel("Album"))
        middle_layout.addLayout(album_row)
        middle_layout.addWidget(QLabel("Track #"))
        middle_layout.addLayout(track_number_row)
        middle_layout.addWidget(QLabel("Genre"))
        middle_layout.addLayout(genre_row)
        middle_layout.addWidget(QLabel("Year"))
        middle_layout.addLayout(year_row)
        middle_layout.addWidget(QLabel("Album art"))
        middle_layout.addLayout(art_row)
        middle_layout.addWidget(self.rename_checkbox)
        middle_layout.addLayout(auto_fill_row)
        middle_layout.addLayout(save_row)
        middle_layout.addWidget(self.progress_bar)
        middle_layout.addStretch()

        lyrics_layout = QVBoxLayout()
        lyrics_layout.addWidget(QLabel("Lyrics"))
        lyrics_layout.addWidget(self.lyrics_edit)
        lyrics_layout.addWidget(self.lrc_sidecar_checkbox)

        body_layout = QHBoxLayout()
        body_layout.addLayout(left_layout, stretch=1)
        body_layout.addLayout(middle_layout, stretch=2)
        body_layout.addLayout(lyrics_layout, stretch=2)

        main_layout = QVBoxLayout()
        main_layout.addLayout(body_layout)
        main_layout.addWidget(self.log_view)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)
        self.resize(1040, 620)

        self._set_detail_panel_enabled(False)

    # -- drag and drop -----------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        self._load_paths(paths)

    def _load_paths(self, paths: list[Path]) -> None:
        new_tracks = load_tracks(paths)
        existing_paths = {track.path for track in self.tracks}
        for track in new_tracks:
            if track.path not in existing_paths:
                self.tracks.append(track)
                self.track_list.addItem(self._list_label(track))
                existing_paths.add(track.path)

        if self.tracks and self.track_list.currentRow() < 0:
            self.track_list.setCurrentRow(0)

    @staticmethod
    def _list_label(track: Track) -> str:
        suffix = f"  [{track.status}]" if track.status else ""
        return f"{track.path.name}{suffix}"

    # -- detail panel --------------------------------------------------

    def _set_detail_panel_enabled(self, enabled: bool) -> None:
        for widget in (
            self.title_input,
            self.artist_input,
            self.album_input,
            self.track_number_input,
            self.genre_input,
            self.year_input,
            self.lyrics_edit,
            self.save_selected_button,
            self.auto_fill_selected_button,
            self.save_art_button,
            self.apply_artist_button,
            self.apply_album_button,
            self.apply_track_total_button,
            self.apply_genre_button,
            self.apply_year_button,
            self.apply_art_button,
        ):
            widget.setEnabled(enabled)

    def _commit_current_track_fields(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)):
            return
        track = self.tracks[self.current_index]
        track.title = self.title_input.text()
        track.artist = self.artist_input.text()
        track.album = self.album_input.text()
        track.track_number = self.track_number_input.text()
        track.genre = self.genre_input.text()
        track.year = self.year_input.text()
        track.lyrics = self.lyrics_edit.toPlainText()

    def _load_track_into_panel(self, row: int) -> None:
        if not (0 <= row < len(self.tracks)):
            self.title_input.clear()
            self.artist_input.clear()
            self.album_input.clear()
            self.track_number_input.clear()
            self.genre_input.clear()
            self.year_input.clear()
            self.lyrics_edit.clear()
            self.art_label.setText("No artwork")
            self.art_label.setPixmap(QPixmap())
            self._set_detail_panel_enabled(False)
            return

        track = self.tracks[row]
        self.title_input.setText(track.title)
        self.artist_input.setText(track.artist)
        self.album_input.setText(track.album)
        self.track_number_input.setText(track.track_number)
        self.genre_input.setText(track.genre)
        self.year_input.setText(track.year)
        self.lyrics_edit.setPlainText(track.lyrics)
        self._update_art_preview(track)
        self._set_detail_panel_enabled(True)

    def _update_art_preview(self, track: Track) -> None:
        if track.album_art:
            pixmap = QPixmap()
            pixmap.loadFromData(track.album_art)
            self.art_label.setPixmap(pixmap)
        else:
            self.art_label.setPixmap(QPixmap())
            self.art_label.setText("No artwork")

    def _on_row_changed(self, new_row: int) -> None:
        self._commit_current_track_fields()
        self.current_index = new_row
        self._load_track_into_panel(new_row)

    def _browse_album_art(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)):
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select album art", "", "Images (*.jpg *.jpeg *.png)"
        )
        if not file_path:
            return
        track = self.tracks[self.current_index]
        track.album_art = Path(file_path).read_bytes()
        self._update_art_preview(track)

    def _save_album_art_as(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)):
            return
        track = self.tracks[self.current_index]
        if not track.album_art:
            QMessageBox.information(self, "No artwork", "This track has no album art to save.")
            return

        default_name = f"{track.artist} - {track.title}".strip(" -") or track.path.stem
        default_path = str(track.path.with_name(f"{default_name}.jpg"))
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save album art", default_path, "JPEG Image (*.jpg)"
        )
        if not file_path:
            return
        Path(file_path).write_bytes(track.album_art)
        self.log_view.appendPlainText(f"Saved album art to {file_path}")

    # -- bulk apply --------------------------------------------------------

    def _apply_field_to_all(self, field_name: str, value: str) -> None:
        if not self.tracks:
            return
        for track in self.tracks:
            setattr(track, field_name, value)
        self.log_view.appendPlainText(
            f"Applied {field_name} = {value!r} to all {len(self.tracks)} track(s)."
        )

    def _apply_track_total_to_all(self) -> None:
        text = self.track_number_input.text().strip()
        if "/" not in text:
            QMessageBox.information(
                self, "No total", 'Enter a track number with a total, e.g. "3/12", first.'
            )
            return
        total = text.split("/", 1)[1].strip()
        for track in self.tracks:
            prefix = track.track_number.split("/", 1)[0].strip() if track.track_number else ""
            track.track_number = f"{prefix}/{total}"
        self.log_view.appendPlainText(
            f"Applied total track count ({total}) to all {len(self.tracks)} track(s)."
        )
        if 0 <= self.current_index < len(self.tracks):
            self.track_number_input.setText(self.tracks[self.current_index].track_number)

    def _apply_album_art_to_all(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)):
            return
        track = self.tracks[self.current_index]
        if not track.album_art:
            QMessageBox.information(self, "No artwork", "This track has no album art to apply.")
            return
        for other in self.tracks:
            other.album_art = track.album_art
        self.log_view.appendPlainText(f"Applied album art to all {len(self.tracks)} track(s).")

    # -- auto-fill -----------------------------------------------------

    def _start_auto_fill(self, indices: list[int]) -> None:
        if not indices or self.worker is not None:
            return
        self._commit_current_track_fields()

        self.progress_bar.setValue(0)
        self.auto_fill_selected_button.setEnabled(False)
        self.auto_fill_all_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        self.worker = AutoFillWorker(self.tracks, indices)
        self.worker.track_updated.connect(self._on_track_updated)
        self.worker.log_message.connect(self.log_view.appendPlainText)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_auto_fill_finished)
        self.worker.start()

    def _auto_fill_selected(self) -> None:
        if 0 <= self.current_index < len(self.tracks):
            self._start_auto_fill([self.current_index])

    def _auto_fill_all(self) -> None:
        self._start_auto_fill(list(range(len(self.tracks))))

    def _cancel_auto_fill(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)

    def _on_track_updated(self, index: int) -> None:
        track = self.tracks[index]
        self.track_list.item(index).setText(self._list_label(track))
        if index == self.current_index:
            self._load_track_into_panel(index)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress_bar.setValue(int(done / total * 100) if total else 0)

    def _on_auto_fill_finished(self) -> None:
        self.worker = None
        self.auto_fill_selected_button.setEnabled(True)
        self.auto_fill_all_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    # -- save ------------------------------------------------------------

    def _save_track(self, index: int) -> bool:
        track = self.tracks[index]
        try:
            save_track(
                track,
                write_lrc_sidecar=self.lrc_sidecar_checkbox.isChecked(),
                rename_to_title=self.rename_checkbox.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001 - surface any tagging failure
            track.status = "save failed"
            self.log_view.appendPlainText(f"{track.path.name}: save failed - {exc}")
            self.track_list.item(index).setText(self._list_label(track))
            return False
        else:
            track.status = "saved"
            self.log_view.appendPlainText(f"{track.path.name}: saved")
            self.track_list.item(index).setText(self._list_label(track))
            return True

    def _save_selected(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)):
            return
        self._commit_current_track_fields()
        if not self._save_track(self.current_index):
            QMessageBox.warning(self, "Save failed", "See the log for details.")

    def _save_all(self) -> None:
        if not self.tracks:
            return
        self._commit_current_track_fields()
        failures = [i for i in range(len(self.tracks)) if not self._save_track(i)]
        if failures:
            QMessageBox.warning(
                self, "Save failed", f"{len(failures)} file(s) failed to save. See the log."
            )


def main() -> None:
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
