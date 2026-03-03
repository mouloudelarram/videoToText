#!/usr/bin/env python3
"""
YouTube Audio Transcriber (Terminal App)

This script:
1) Asks for a YouTube URL
2) Downloads audio with yt-dlp
3) Converts audio to WAV (16 kHz mono) via ffmpeg
4) Transcribes with local OpenAI Whisper
5) Saves transcription to file.txt (same directory as this script)

Install dependencies:
1) Python 3.9+ recommended
2) Install Python packages:
   pip install -r requirements.txt
3) Install ffmpeg and make sure `ffmpeg` is in PATH.
   Windows (example):
   - Download ffmpeg from https://ffmpeg.org/download.html
   - Add the ffmpeg `bin` folder to PATH

Run:
   python index.py

Notes:
- Uses local Whisper model (no OpenAI API key required).
- Default Whisper model is "base" for speed/accuracy balance.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def ensure_ffmpeg_available() -> None:
    """Ensure ffmpeg is available in PATH, trying local ./ffmpeg/bin fallback."""
    local_ffmpeg_bin = Path(__file__).resolve().parent / "ffmpeg" / "bin"
    if local_ffmpeg_bin.exists():
        # Prepend local ffmpeg for this process only.
        path_sep = ";" if sys.platform.startswith("win") else ":"
        current_path = os.environ.get("PATH", "")
        if str(local_ffmpeg_bin) not in current_path:
            os.environ["PATH"] = f"{local_ffmpeg_bin}{path_sep}{os.environ.get('PATH', '')}"

    # Safe subprocess usage: pass args as a list, no shell=True.
    result = subprocess.run(
        ["ffmpeg", "-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EnvironmentError(
            "ffmpeg is not installed or not available in PATH. "
            "Install ffmpeg, then try again."
        )


def validate_youtube_url(url: str) -> bool:
    """Basic YouTube URL validation."""
    if not url or not isinstance(url, str):
        return False

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False

    host = (parsed.netloc or "").lower()
    valid_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
    if host not in valid_hosts:
        return False

    # Accept typical watch/embed/short links.
    return bool(re.search(r"(watch\?v=|youtu\.be/|/shorts/|/embed/)", url))


def download_audio_as_wav(url: str, output_dir: Path) -> Path:
    """Download YouTube audio and convert to WAV using yt-dlp + ffmpeg."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise ImportError("Missing dependency: yt-dlp. Run: pip install yt-dlp") from exc

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "quiet": False,  # Keep download progress visible.
        "no_warnings": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
            }
        ],
        "postprocessor_args": [
            "-ar",
            "16000",
            "-ac",
            "1",
        ],
    }

    print("Downloading and converting audio...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info.get("id")
        if not video_id:
            raise RuntimeError("Could not determine video ID after download.")

    wav_path = output_dir / f"{video_id}.wav"
    if not wav_path.exists():
        wav_candidates = sorted(output_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not wav_candidates:
            raise RuntimeError("Audio conversion failed: WAV file not found.")
        wav_path = wav_candidates[0]

    return wav_path


def transcribe_audio(audio_path: Path, model_name: str = "base") -> str:
    """Transcribe audio file using local Whisper model."""
    try:
        import whisper
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: openai-whisper. Run: pip install openai-whisper"
        ) from exc

    print(f"Loading Whisper model '{model_name}'...")
    model = whisper.load_model(model_name)

    print("Transcribing audio (this may take a while)...")
    result = model.transcribe(str(audio_path), fp16=False, verbose=True)
    text = (result or {}).get("text", "").strip()

    if not text:
        raise RuntimeError("Transcription completed but no text was produced.")

    return text


def save_transcription(text: str, output_file: Path) -> None:
    output_file.write_text(text, encoding="utf-8")


def main() -> int:
    print("=== YouTube to Text (Whisper Local) ===")
    url = input("Enter a YouTube video URL: ").strip()

    if not validate_youtube_url(url):
        print("Error: Invalid YouTube URL.")
        return 1

    try:
        ensure_ffmpeg_available()

        with tempfile.TemporaryDirectory(prefix="yt_audio_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            audio_file = download_audio_as_wav(url, tmp_path)
            transcription = transcribe_audio(audio_file, model_name="base")

        output_path = Path(__file__).resolve().parent / "file.txt"
        save_transcription(transcription, output_path)

        print(f"Success: Transcription saved to {output_path}")
        return 0

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
