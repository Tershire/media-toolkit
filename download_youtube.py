#!/usr/bin/env python3
"""Download YouTube videos with yt-dlp.

Use only for videos you own, videos with permission, or content that is
otherwise legal for you to download.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yt_dlp


def build_options(
    output_dir: Path, audio_only: bool, playlist: bool, video_only: bool = False
) -> dict:
    output_template = str(output_dir / "%(title).200B [%(id)s].%(ext)s")

    if audio_only:
        return {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "noplaylist": not playlist,
        }

    if video_only:
        return {
            "format": "bestvideo[ext=mp4]/bestvideo",
            "outtmpl": output_template,
            "noplaylist": not playlist,
        }

    return {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "noplaylist": not playlist,
    }


def download(
    url: str, output_dir: Path, audio_only: bool, playlist: bool, video_only: bool = False
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    options = build_options(output_dir, audio_only, playlist, video_only)

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a YouTube video or playlist using yt-dlp."
    )
    parser.add_argument("url", help="YouTube video or playlist URL")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="downloads",
        help="Directory to save downloaded files. Default: downloads",
    )
    av_group = parser.add_mutually_exclusive_group()
    av_group.add_argument(
        "--audio-only",
        action="store_true",
        help="Download audio only and convert it to MP3. Requires ffmpeg.",
    )
    av_group.add_argument(
        "--video-only",
        action="store_true",
        help="Download video only, without audio.",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Download the whole playlist if the URL belongs to one. "
        "Default: download only the single video.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    download(
        args.url, Path(args.output_dir), args.audio_only, args.playlist, args.video_only
    )


if __name__ == "__main__":
    main()
