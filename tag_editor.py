#!/usr/bin/env python3
"""Core logic for tagging audio files: artist/album/album art/lyrics.

Searches the free iTunes Search API for metadata and album art, and the
syncedlyrics library for (synced) lyrics, then embeds everything into the
audio file's tags with mutagen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import requests
import syncedlyrics
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK, USLT
from mutagen.mp3 import MP3

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav"}

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

YT_DLP_ID_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]+\]\s*$")


@dataclass
class Track:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    track_number: str = ""
    genre: str = ""
    year: str = ""
    album_art: bytes | None = None
    lyrics: str = ""
    status: str = ""


def _parse_filename(path: Path) -> tuple[str, str]:
    """Guess (artist, title) from a filename, best-effort."""
    stem = YT_DLP_ID_SUFFIX_RE.sub("", path.stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem


def load_tracks(paths: list[Path]) -> list[Track]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(
                sorted(p for p in path.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS)
            )
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(path)

    tracks = []
    for file_path in files:
        artist, title = _parse_filename(file_path)
        tracks.append(Track(path=file_path, title=title, artist=artist))
    return tracks


def search_metadata(track: Track) -> dict:
    """Look up artist/album/artwork on the iTunes Search API."""
    query = f"{track.artist} {track.title}".strip()
    if not query:
        return {}

    response = requests.get(
        ITUNES_SEARCH_URL,
        params={"term": query, "media": "music", "entity": "song", "limit": 1},
        timeout=10,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return {}

    hit = results[0]
    artwork_url = hit.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
    album_art = None
    if artwork_url:
        art_response = requests.get(artwork_url, timeout=10)
        if art_response.ok:
            album_art = art_response.content

    track_number = ""
    if hit.get("trackNumber"):
        track_number = str(hit["trackNumber"])
        if hit.get("trackCount"):
            track_number += f"/{hit['trackCount']}"

    return {
        "artist": hit.get("artistName", track.artist),
        "album": hit.get("collectionName", track.album),
        "title": hit.get("trackName", track.title),
        "track_number": track_number,
        "genre": hit.get("primaryGenreName", track.genre),
        "year": hit.get("releaseDate", "")[:4] or track.year,
        "album_art": album_art,
    }


def search_lyrics(track: Track) -> str | None:
    """Look up synced (LRC) lyrics via syncedlyrics."""
    query = f"{track.artist} {track.title}".strip()
    if not query:
        return None
    return syncedlyrics.search(query)


def auto_fill(track: Track) -> Track:
    """Fetch metadata and lyrics for a track, returning the updated track."""
    metadata = search_metadata(track)
    track.artist = metadata.get("artist", track.artist)
    track.album = metadata.get("album", track.album)
    track.title = metadata.get("title", track.title)
    if metadata.get("track_number"):
        track.track_number = metadata["track_number"]
    track.genre = metadata.get("genre", track.genre)
    track.year = metadata.get("year", track.year)
    if metadata.get("album_art"):
        track.album_art = metadata["album_art"]

    lyrics = search_lyrics(track)
    if lyrics:
        track.lyrics = lyrics

    track.status = "found" if metadata or lyrics else "not found"
    return track


def save_track(track: Track, write_lrc_sidecar: bool = False) -> None:
    """Embed the track's fields into the audio file's tags."""
    if track.path.suffix.lower() != ".mp3":
        raise ValueError(f"Unsupported file type for tagging: {track.path.suffix}")

    audio = MP3(track.path)
    if audio.tags is None:
        audio.add_tags()
    tags: ID3 = audio.tags

    tags.setall("TIT2", [TIT2(encoding=3, text=[track.title])])
    tags.setall("TPE1", [TPE1(encoding=3, text=[track.artist])])
    tags.setall("TALB", [TALB(encoding=3, text=[track.album])])
    if track.track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=[track.track_number])])
    if track.genre:
        tags.setall("TCON", [TCON(encoding=3, text=[track.genre])])
    if track.year:
        tags.setall("TDRC", [TDRC(encoding=3, text=[track.year])])

    if track.album_art:
        tags.setall(
            "APIC",
            [APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=track.album_art)],
        )

    if track.lyrics:
        tags.setall(
            "USLT",
            [USLT(encoding=3, lang="eng", desc="", text=track.lyrics)],
        )

    audio.save()

    if write_lrc_sidecar and track.lyrics:
        track.path.with_suffix(".lrc").write_text(track.lyrics, encoding="utf-8")
