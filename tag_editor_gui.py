#!/usr/bin/env python3
"""PySide6 GUI for tagging audio files with artist/album/album art/lyrics.

Drag a song or a directory of songs in, auto-fill their metadata from the
web, review/edit, then save the tags into the files.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
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
    QSplitter,
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


class LyricsSearchWorker(QThread):
    results_ready = Signal(list)
    failed = Signal(str)

    def __init__(self, artist: str, title: str, album_hint: str):
        super().__init__()
        self.artist = artist
        self.title = title
        self.album_hint = album_hint

    def run(self) -> None:
        try:
            track = Track(path=Path("."), artist=self.artist, title=self.title)
            candidates = tag_editor.search_lyrics_candidates(track, album_hint=self.album_hint)
        except Exception as exc:  # noqa: BLE001 - surface any lookup failure
            self.failed.emit(str(exc))
        else:
            self.results_ready.emit(candidates)


class LyricsPickerDialog(QDialog):
    def __init__(self, candidates: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Lyrics")
        self.resize(560, 480)
        self.candidates = candidates
        self.selected_candidate: dict | None = None

        self.result_list = QListWidget()
        for candidate in candidates:
            album = candidate["album"] or "(no album)"
            self.result_list.addItem(f"[{candidate['source']}] {album} — {candidate['type']}")
        self.result_list.currentRowChanged.connect(self._on_selection_changed)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(f"{len(candidates)} result(s) found — pick one:"))
        layout.addWidget(self.result_list)
        layout.addWidget(QLabel("Preview"))
        layout.addWidget(self.preview)
        layout.addWidget(self.button_box)
        self.setLayout(layout)

        if candidates:
            self.result_list.setCurrentRow(0)

    def _on_selection_changed(self, row: int) -> None:
        if 0 <= row < len(self.candidates):
            self.preview.setPlainText(self.candidates[row]["lyrics"])
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        else:
            self.preview.clear()
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _on_accept(self) -> None:
        row = self.result_list.currentRow()
        if 0 <= row < len(self.candidates):
            self.selected_candidate = self.candidates[row]
            self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Tag Editor")
        self.setAcceptDrops(True)

        self.tracks: list[Track] = []
        self.current_index: int = -1
        self.worker: AutoFillWorker | None = None
        self.lyrics_search_worker: LyricsSearchWorker | None = None
        self._lyrics_search_target: Track | None = None

        self.track_list = QListWidget()
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.currentRowChanged.connect(self._on_row_changed)

        self.remove_selected_button = QPushButton("Remove Selected")
        self.remove_selected_button.clicked.connect(self._remove_selected_tracks)
        self.clear_all_button = QPushButton("Clear All")
        self.clear_all_button.clicked.connect(self._clear_all_tracks)

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

        self.search_lyrics_button = QPushButton("Search Lyrics...")
        self.search_lyrics_button.clicked.connect(self._search_lyrics_for_selected)

        self.lyrics_edit = QPlainTextEdit()
        self.lyrics_source_label = QLabel("")
        self.lyrics_source_label.setStyleSheet("color: gray; font-style: italic;")
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

        list_buttons_row = QHBoxLayout()
        list_buttons_row.addWidget(self.remove_selected_button)
        list_buttons_row.addWidget(self.clear_all_button)

        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Drop songs or a folder here"))
        left_layout.addWidget(self.track_list)
        left_layout.addLayout(list_buttons_row)

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
        lyrics_layout.addWidget(self.search_lyrics_button)
        lyrics_layout.addWidget(self.lyrics_source_label)
        lyrics_layout.addWidget(self.lyrics_edit)
        lyrics_layout.addWidget(self.lrc_sidecar_checkbox)

        left_widget = QWidget()
        left_widget.setLayout(left_layout)
        middle_widget = QWidget()
        middle_widget.setLayout(middle_layout)
        lyrics_widget = QWidget()
        lyrics_widget.setLayout(lyrics_layout)

        body_splitter = QSplitter(Qt.Orientation.Horizontal)
        body_splitter.addWidget(left_widget)
        body_splitter.addWidget(middle_widget)
        body_splitter.addWidget(lyrics_widget)
        body_splitter.setStretchFactor(0, 1)
        body_splitter.setStretchFactor(1, 2)
        body_splitter.setStretchFactor(2, 2)
        body_splitter.setSizes([260, 480, 480])

        main_layout = QVBoxLayout()
        main_layout.addWidget(body_splitter)
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

    def _remove_selected_tracks(self) -> None:
        rows = sorted({index.row() for index in self.track_list.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self.track_list.takeItem(row)
            del self.tracks[row]
        self._refresh_selection_after_removal()

    def _clear_all_tracks(self) -> None:
        if not self.tracks:
            return
        reply = QMessageBox.question(
            self,
            "Clear all",
            f"Remove all {len(self.tracks)} loaded track(s) from the list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.track_list.clear()
        self.tracks.clear()
        self._refresh_selection_after_removal()

    def _refresh_selection_after_removal(self) -> None:
        new_row = min(self.current_index, len(self.tracks) - 1) if self.tracks else -1
        self.current_index = new_row
        self.track_list.blockSignals(True)
        self.track_list.setCurrentRow(new_row)
        self.track_list.blockSignals(False)
        self._load_track_into_panel(new_row)

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
            self.search_lyrics_button,
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
            self.lyrics_source_label.clear()
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
        self.lyrics_source_label.setText(f"Source: {track.lyrics_source}" if track.lyrics_source else "")
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

    # -- lyrics search -------------------------------------------------

    def _search_lyrics_for_selected(self) -> None:
        if not (0 <= self.current_index < len(self.tracks)) or self.lyrics_search_worker is not None:
            return
        self._commit_current_track_fields()
        track = self.tracks[self.current_index]
        self._lyrics_search_target = track

        self.search_lyrics_button.setEnabled(False)
        self.log_view.appendPlainText(f"Searching lyrics for {track.artist} - {track.title}...")

        self.lyrics_search_worker = LyricsSearchWorker(track.artist, track.title, track.album)
        self.lyrics_search_worker.results_ready.connect(self._on_lyrics_search_results)
        self.lyrics_search_worker.failed.connect(self._on_lyrics_search_failed)
        self.lyrics_search_worker.finished.connect(self._on_lyrics_search_finished)
        self.lyrics_search_worker.start()

    def _on_lyrics_search_results(self, candidates: list[dict]) -> None:
        target = self._lyrics_search_target
        if not candidates:
            QMessageBox.information(self, "No lyrics found", "No lyrics results were found for this track.")
            return

        dialog = LyricsPickerDialog(candidates, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_candidate and target is not None:
            chosen = dialog.selected_candidate
            target.lyrics = chosen["lyrics"]
            target.lyrics_source = chosen["source"]
            self.log_view.appendPlainText(f"Lyrics set from {chosen['source']} for {target.path.name}.")
            if 0 <= self.current_index < len(self.tracks) and self.tracks[self.current_index] is target:
                self._load_track_into_panel(self.current_index)

    def _on_lyrics_search_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Lyrics search failed", message)

    def _on_lyrics_search_finished(self) -> None:
        self.lyrics_search_worker = None
        self._lyrics_search_target = None
        self.search_lyrics_button.setEnabled(True)

    # -- auto-fill -----------------------------------------------------

    def _start_auto_fill(self, indices: list[int]) -> None:
        if not indices or self.worker is not None:
            return
        self._commit_current_track_fields()

        self.progress_bar.setValue(0)
        self.auto_fill_selected_button.setEnabled(False)
        self.auto_fill_all_button.setEnabled(False)
        self.remove_selected_button.setEnabled(False)
        self.clear_all_button.setEnabled(False)
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
        self.remove_selected_button.setEnabled(True)
        self.clear_all_button.setEnabled(True)
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
