from __future__ import annotations

import json
from pathlib import Path

from .errors import ExternalCommandError
from .media import run_command
from .url import validate_youtube_url


MIN_SECTION_BYTES = 100_000


def _ytdlp_base_args() -> list[str]:
    """Shared resilience flags for every yt-dlp invocation.

    YouTube throttles bursty clients (HTTP 429) and escalates to bot
    checks ("Sign in to confirm you're not a bot"). These flags back
    off exponentially, space out metadata requests, and attach login
    cookies when the operator provides them via YTDLP_COOKIES_FILE
    (Netscape cookies.txt, e.g. exported from a logged-in browser).
    """
    import os

    args = [
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--retry-sleep", "exp=2:30",
        "--sleep-requests", "1",
        # YouTube's n-sig JS challenge: solved locally via deno using the
        # EJS remote component (required since mid-2026; without it even
        # logged-in sessions get "The page needs to be reloaded").
        "--js-runtimes", "deno",
        "--remote-components", "ejs:github",
    ]
    cookies = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if cookies:
        args += ["--cookies", cookies]
    return args


def _check_media_size(path: Path, context: str) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExternalCommandError(f"{context}: downloaded file is unreadable: {exc}") from exc
    if size < MIN_SECTION_BYTES:
        raise ExternalCommandError(
            f"{context}: downloaded file is too small ({size} bytes), likely a truncated section"
        )


def download_video(url: str, output_dir: Path, yt_dlp: str = "yt-dlp") -> Path:
    validate_youtube_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "source.%(ext)s"
    result = run_command([
        yt_dlp, *_ytdlp_base_args(), "--restrict-filenames",
        "--concurrent-fragments", "4",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(output), url,
    ])
    files = sorted(output_dir.glob("source.*"))
    media = next((path for path in files if path.suffix.lower() in {".mp4", ".mkv", ".webm"}), None)
    if media is None:
        raise ExternalCommandError(f"yt-dlp completed without a media file: {result.stdout[-500:]}")
    _check_media_size(media, "download_video")
    return media


def video_metadata(url: str, yt_dlp: str = "yt-dlp") -> dict:
    validate_youtube_url(url)
    result = run_command([
        yt_dlp, *_ytdlp_base_args(), "--skip-download", "--dump-single-json", url,
    ])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError("yt-dlp returned invalid metadata JSON") from exc
    return payload


def video_duration(url: str, yt_dlp: str = "yt-dlp") -> float | None:
    duration = video_metadata(url, yt_dlp=yt_dlp).get("duration")
    return float(duration) if duration is not None else None


def _direct_stream_url(url: str, format_selector: str, yt_dlp: str = "yt-dlp") -> str:
    """Resolve a single direct (googlevideo) stream URL via `yt-dlp -g`."""
    result = run_command([
        yt_dlp, *_ytdlp_base_args(), "-g", "-f", format_selector, url])
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("http"):
            return candidate
    raise ExternalCommandError(
        f"yt-dlp returned no stream URL for format {format_selector!r}"
    )


def _sanitize_error(message: str) -> str:
    """Strip signed stream URLs (sig tokens) from user-facing errors."""
    import re
    message = re.sub(r"https?://\S+", "<stream-url>", message)
    return message[-500:]


def download_video_section(
    url: str,
    output_dir: Path,
    start: float,
    end: float,
    name: str,
    yt_dlp: str = "yt-dlp",
) -> Path:
    """Download only [start, end] with guaranteed video+audio streams.

    NOTE: yt-dlp's `--download-sections` cutting silently drops the video
    stream on fragmented DASH/HLS sources (ffmpeg copies 0 video frames but
    still exits 0). Instead we resolve direct stream URLs with `yt-dlp -g`
    and cut locally with ffmpeg using accurate seek + video re-encode.
    """
    from .media import verify_clip

    validate_youtube_url(url)
    if not end > start:
        raise ExternalCommandError(f"Invalid section range: start={start} end={end}")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{name}.mp4"
    if output.exists():
        output.unlink()
    last_error: str = "unknown error"
    # Try full quality first, then step down. URLs are re-resolved every
    # attempt because googlevideo links expire quickly (403s).
    for video_selector in ("bv*[height<=1080]", "bv*[height<=720]", "bv*[height<=480]"):
        try:
            video_url = _direct_stream_url(url, video_selector, yt_dlp)
            audio_url = _direct_stream_url(url, "ba", yt_dlp)
            run_command([
                "ffmpeg", "-y", "-hide_banner", "-v", "warning",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video_url,
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", audio_url,
                "-map", "0:v:0", "-map", "1:a:0",
                # Near-transparent intermediate (CRF 16): this file is cut
                # again + re-encoded at final render, so a lossy CRF 20 here
                # would stack generation loss twice onto the output.
                "-c:v", "libx264", "-preset", "fast", "-crf", "16",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
                "-shortest", str(output),
            ])
            _check_media_size(output, f"download_video_section {name}")
            verify_clip(output, expected_duration=end - start)
            return output
        except ExternalCommandError as exc:
            last_error = _sanitize_error(str(exc))
            if output.exists():
                output.unlink()
            continue
    raise ExternalCommandError(
        f"download_video_section {name} gagal di semua kualitas, "
        f"kesalahan terakhir: {last_error}"
    )
    return media


def download_audio(url: str, output_dir: Path, yt_dlp: str = "yt-dlp") -> Path:
    validate_youtube_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "audio.%(ext)s"
    result = run_command([
        yt_dlp, *_ytdlp_base_args(), "--restrict-filenames", "-f", "ba",
        "-o", str(output), url,
    ])
    audio = next(iter(sorted(output_dir.glob("audio.*"))), None)
    if audio is None:
        raise ExternalCommandError(f"yt-dlp completed without an audio file: {result.stdout[-500:]}")
    return audio


def download_subtitles(
    url: str,
    output_dir: Path,
    language: str = "id",
    yt_dlp: str = "yt-dlp",
) -> Path | None:
    validate_youtube_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "subtitles.%(ext)s"
    try:
        run_command([
            yt_dlp, *_ytdlp_base_args(), "--skip-download",
            "--write-auto-subs", "--write-subs", "--sub-langs", language,
            "--sub-format", "vtt/srt", "--convert-subs", "vtt",
            "-o", str(output), url,
        ])
    except ExternalCommandError as exc:
        if "subtitle" not in str(exc).lower() and "subtitles" not in str(exc).lower():
            raise
    return next(iter(sorted(output_dir.glob("subtitles.*"))), None)
