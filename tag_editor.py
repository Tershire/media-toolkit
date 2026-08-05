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
from rapidfuzz import fuzz
from syncedlyrics import Genius, Megalobiz, Musixmatch, NetEase
from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK, USLT
from mutagen.mp3 import MP3

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav"}

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LYRICS_MATCH_MIN_SCORE = 60

YT_DLP_ID_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]+\]\s*$")
YT_DLP_ID_CAPTURE_RE = re.compile(r"\[([^\[\]]+)\]\s*$")
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    lyrics_source: str = ""
    status: str = ""


def _parse_filename(path: Path) -> tuple[str, str]:
    """Guess (artist, title) from a filename, best-effort."""
    stem = YT_DLP_ID_SUFFIX_RE.sub("", path.stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem


def extract_youtube_id(path: Path) -> str | None:
    """Pull the trailing "[<id>]" yt-dlp suffix out of a filename, if present."""
    match = YT_DLP_ID_CAPTURE_RE.search(path.stem)
    return match.group(1) if match else None


def sanitize_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS_RE.sub("_", name).strip().strip(".")
    return cleaned or "untitled"


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


def _fetch_lrclib_results(track: Track) -> list[dict]:
    """Search LRCLIB directly and return raw results that plausibly match.

    LRCLIB is the only lyrics provider that returns album names alongside
    results, so it's the only one we can rank/list by album match ourselves.
    """
    query = f"{track.artist} {track.title}".strip()
    if not query:
        return []

    response = requests.get(LRCLIB_SEARCH_URL, params={"q": query}, timeout=10)
    if not response.ok:
        return []
    results = response.json()
    if not results:
        return []

    def title_artist_score(entry: dict) -> float:
        return fuzz.token_set_ratio(
            f'{entry.get("artistName", "")} {entry.get("trackName", "")}'.lower(), query.lower()
        )

    return [r for r in results if title_artist_score(r) >= LYRICS_MATCH_MIN_SCORE]


def _search_lrclib(track: Track, album_hint: str) -> dict | None:
    query = f"{track.artist} {track.title}".strip()
    candidates = _fetch_lrclib_results(track)
    if not candidates:
        return None

    def title_artist_score(entry: dict) -> float:
        return fuzz.token_set_ratio(
            f'{entry.get("artistName", "")} {entry.get("trackName", "")}'.lower(), query.lower()
        )

    def by_album_match(cands: list[dict]) -> list[dict]:
        if album_hint:
            return sorted(
                cands,
                key=lambda r: fuzz.token_set_ratio((r.get("albumName") or "").lower(), album_hint.lower()),
                reverse=True,
            )
        return sorted(cands, key=title_artist_score, reverse=True)

    # Prefer synced lyrics across all candidates; only fall back to plain
    # lyrics if none of them have a synced version.
    synced_candidates = [r for r in candidates if r.get("syncedLyrics")]
    if synced_candidates:
        best = by_album_match(synced_candidates)[0]
        return {"lyrics": best["syncedLyrics"], "source": "LRCLIB"}

    plain_candidates = [r for r in candidates if r.get("plainLyrics")]
    if plain_candidates:
        best = by_album_match(plain_candidates)[0]
        return {"lyrics": best["plainLyrics"], "source": "LRCLIB"}

    return None


def _search_other_lyrics_providers(track: Track) -> dict | None:
    """Fall back to syncedlyrics' other providers (no album-level ranking available)."""
    query = f"{track.artist} {track.title}".strip()
    if not query:
        return None

    # Keep looking across providers for synced lyrics; remember the first
    # plain-text hit as a fallback in case none of them have synced lyrics.
    plain_fallback: dict | None = None
    for provider in (Musixmatch(), NetEase(), Megalobiz(), Genius()):
        try:
            lrc = provider.get_lrc(query)
        except Exception:
            continue
        if not lrc:
            continue
        if lrc.synced:
            return {"lyrics": lrc.synced, "source": str(provider)}
        if lrc.unsynced and plain_fallback is None:
            plain_fallback = {"lyrics": lrc.unsynced, "source": str(provider)}
    return plain_fallback


def search_lyrics(track: Track, album_hint: str | None = None) -> dict | None:
    """Look up (synced) lyrics, preferring the result closest to `album_hint`."""
    album = track.album if album_hint is None else album_hint
    return _search_lrclib(track, album) or _search_other_lyrics_providers(track)


def search_lyrics_candidates(
    track: Track, album_hint: str | None = None, limit: int = 10
) -> list[dict]:
    """Return multiple lyrics candidates (LRCLIB results + other providers) for review.

    Unlike `search_lyrics`, this doesn't pick a single "best" result - it's meant
    for a UI where the user reviews and picks one themselves.
    """
    album = track.album if album_hint is None else album_hint
    query = f"{track.artist} {track.title}".strip()

    lrclib_results = _fetch_lrclib_results(track)
    if album:
        lrclib_results = sorted(
            lrclib_results,
            key=lambda r: fuzz.token_set_ratio((r.get("albumName") or "").lower(), album.lower()),
            reverse=True,
        )

    candidates: list[dict] = []
    for r in lrclib_results:
        lyrics = r.get("syncedLyrics") or r.get("plainLyrics")
        if not lyrics:
            continue
        candidates.append(
            {
                "source": "LRCLIB",
                "album": r.get("albumName") or "",
                "artist": r.get("artistName") or track.artist,
                "title": r.get("trackName") or track.title,
                "lyrics": lyrics,
                "type": "synced" if r.get("syncedLyrics") else "plain",
            }
        )
        if len(candidates) >= limit:
            break

    if query:
        for provider in (Musixmatch(), NetEase(), Megalobiz(), Genius()):
            try:
                lrc = provider.get_lrc(query)
            except Exception:
                continue
            if not lrc:
                continue
            lyrics = lrc.synced or lrc.unsynced
            if not lyrics:
                continue
            candidates.append(
                {
                    "source": str(provider),
                    "album": "",
                    "artist": track.artist,
                    "title": track.title,
                    "lyrics": lyrics,
                    "type": "synced" if lrc.synced else "plain",
                }
            )

    return candidates


def auto_fill(track: Track) -> Track:
    """Fetch metadata and lyrics for a track, returning the updated track."""
    user_album = track.album  # preserve the user-specified album for lyrics ranking

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

    lyrics_result = search_lyrics(track, album_hint=user_album or track.album)
    if lyrics_result:
        track.lyrics = lyrics_result["lyrics"]
        track.lyrics_source = lyrics_result["source"]

    track.status = "found" if metadata or lyrics_result else "not found"
    return track


def save_track(
    track: Track,
    write_lrc_sidecar: bool = False,
    rename_to_title: bool = False,
) -> None:
    """Embed the track's fields into the audio file's tags."""
    if track.path.suffix.lower() != ".mp3":
        raise ValueError(f"Unsupported file type for tagging: {track.path.suffix}")

    video_id = extract_youtube_id(track.path)

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
    if video_id:
        comment = f"https://www.youtube.com/watch?v={video_id}"
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=[comment])])

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

    if rename_to_title and track.title:
        new_path = track.path.with_name(sanitize_filename(track.title) + track.path.suffix)
        if new_path != track.path:
            track.path.rename(new_path)
            track.path = new_path

    if write_lrc_sidecar and track.lyrics:
        track.path.with_suffix(".lrc").write_text(track.lyrics, encoding="utf-8")
