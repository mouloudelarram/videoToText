#!/usr/bin/env python3
"""
yt_transcribe.py — YouTube Transcript CLI
==========================================
Fetch transcripts from YouTube videos, playlists, or entire channels.

Dependencies:
    pip install youtube-transcript-api   # required — transcript fetching
    pip install yt-dlp                   # required — channel & playlist support

Usage:
    python yt_transcribe.py <url_or_id> [options]
    python yt_transcribe.py --file urls.txt [options]
    python yt_transcribe.py --channel @MrBeast

Examples:
    python yt_transcribe.py dQw4w9WgXcQ
    python yt_transcribe.py https://youtu.be/dQw4w9WgXcQ --lang fr --timestamps
    python yt_transcribe.py dQw4w9WgXcQ --list-langs
    python yt_transcribe.py dQw4w9WgXcQ --stdout
    python yt_transcribe.py --channel @NASA --max-videos 50
    python yt_transcribe.py --channel @NASA --urls-file nasa.txt
    python yt_transcribe.py --file urls.txt --output ./transcripts --lang en
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_OUTPUT_DIR = Path("transcripts")
DEFAULT_URLS_FILE  = Path("urls.txt")

_VIDEO_RE      = re.compile(r"(?:v=|youtu\.be/|/embed/|/v/|/shorts/)([A-Za-z0-9_-]{11})")
_PLAYLIST_RE   = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_BARE_ID_RE    = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ILLEGAL_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


# ── Output helpers ────────────────────────────────────────────────────────────

def print_err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)

def print_info(msg: str) -> None:
    print(f"  {msg}")


# ── File utilities ────────────────────────────────────────────────────────────

def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = _ILLEGAL_CHARS.sub("", name)
    name = re.sub(r"\s+", "_", name.strip()).strip("._")
    return (name or "untitled")[:max_len]

def unique_output_path(directory: Path, video_id: str, force: bool = False) -> Path:
    """Return a safe, non-colliding path for a transcript file."""
    filepath = directory / f"{sanitize_filename(video_id)}.txt"
    if force or not filepath.exists():
        return filepath
    return directory / f"{sanitize_filename(video_id)}_{time.strftime('%Y%m%d_%H%M%S')}.txt"


# ── Transcript formatting ─────────────────────────────────────────────────────

def build_header(video_id: str, language: str) -> str:
    return (
        f"Video ID : {video_id}\n"
        f"URL      : https://www.youtube.com/watch?v={video_id}\n"
        f"Language : {language}\n"
        f"Fetched  : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "─" * 60 + "\n\n"
    )

def format_entries(entries, timestamps: bool) -> str:
    """Convert transcript entries (dicts or objects) to plain text."""
    lines, prev = [], ""
    for e in entries:
        text  = (e.get("text") if isinstance(e, dict) else getattr(e, "text", "")) or ""
        start = (e.get("start") if isinstance(e, dict) else getattr(e, "start", 0)) or 0
        text  = text.strip()
        if not text or text == prev:
            continue
        prev = text
        if timestamps:
            m, s = divmod(int(start), 60)
            lines.append(f"[{m:02d}:{s:02d}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


# ── URL / ID resolution ───────────────────────────────────────────────────────

def extract_video_id(raw: str) -> Optional[str]:
    raw = raw.strip()
    m = _VIDEO_RE.search(raw)
    if m:
        return m.group(1)
    return raw if _BARE_ID_RE.fullmatch(raw) else None

def expand_playlist(playlist_id: str) -> List[str]:
    """Return video IDs from a playlist (requires yt-dlp)."""
    try:
        import yt_dlp
    except ImportError:
        print_err("yt-dlp not installed — cannot expand playlists. Run: pip install yt-dlp")
        return []
    try:
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}", download=False
            )
        return [e["id"] for e in (data.get("entries") or []) if e and e.get("id")]
    except Exception as exc:
        print_err(f"Playlist expand failed: {exc}")
        return []

def resolve_inputs(lines: List[str]) -> List[str]:
    """Parse URLs/IDs from text lines into a deduplicated list of video IDs."""
    seen: set      = set()
    ids: List[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        pm = _PLAYLIST_RE.search(raw)
        if pm:
            inline = _VIDEO_RE.search(raw)
            if inline and inline.group(1) not in seen:
                v = inline.group(1); seen.add(v); ids.append(v)
            for v in expand_playlist(pm.group(1)):
                if v not in seen:
                    seen.add(v); ids.append(v)
            continue
        vid = extract_video_id(raw)
        if vid:
            if vid not in seen:
                seen.add(vid); ids.append(vid)
        else:
            print_err(f"Skipping unrecognised input: {raw[:80]}")
    return ids


# ── youtube-transcript-api helpers ───────────────────────────────────────────

def _transcript_api_version() -> Tuple[int, int]:
    try:
        import importlib.metadata
        m = re.match(r"(\d+)\.(\d+)", importlib.metadata.version("youtube-transcript-api"))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:
        return (0, 0)

def _get_transcript_api():
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        return YouTubeTranscriptApi()
    except ImportError:
        print_err("youtube-transcript-api not installed. Run: pip install youtube-transcript-api")
        sys.exit(1)


# ── Channel → urls.txt ────────────────────────────────────────────────────────

def channel_to_urls(
    channel    : str,
    out_file   : Path = DEFAULT_URLS_FILE,
    max_videos : int  = 0,
    verbose    : bool = True,
) -> int:
    """
    Resolve a channel to its video URLs and write them to out_file.
    Uses yt-dlp — no API key required.  Install: pip install yt-dlp

    channel accepts:
        @handle          →  @MrBeast
        full channel URL →  https://www.youtube.com/@NASA
        bare name        →  MrBeast  (resolved as @MrBeast)

    Tries /videos, /streams, /shorts tabs in order — stops as soon as
    videos are found, so channels that only post shorts are also covered.
    Returns the number of URLs written.
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is required for channel mode.  Install: pip install yt-dlp")

    s = channel.strip()
    if not s:
        raise RuntimeError("Channel name or URL is empty.")

    if not s.startswith(("http://", "https://")):
        s = f"https://www.youtube.com/@{s.lstrip('@')}"

    if verbose:
        print_info(f"🔍 Looking up channel: {s}")

    opts                = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
    ch_name             = channel
    ids : List[str]     = []
    seen: set           = set()

    for suffix in ("/videos", "/streams", "/shorts", ""):
        url = s.rstrip("/") + suffix
        if verbose:
            print(f"     Trying: {url}", end="\r", flush=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(url, download=False)
            if not data:
                continue
            ch_name = (
                data.get("channel") or data.get("uploader") or
                data.get("title")   or channel
            )
            for entry in (data.get("entries") or []):
                if not entry:
                    continue
                if entry.get("_type") == "playlist":
                    for sub in (entry.get("entries") or []):
                        if sub and sub.get("id") and sub["id"] not in seen:
                            seen.add(sub["id"]); ids.append(sub["id"])
                elif entry.get("id") and entry["id"] not in seen:
                    seen.add(entry["id"]); ids.append(entry["id"])
            if ids:
                if verbose:
                    print()
                    print_info(f"   ✔ Found {len(ids)} videos on: {ch_name}")
                break
        except Exception as exc:
            if verbose:
                print(f"     ✗ tab '{suffix or 'bare'}': {exc}")
            continue

    if not ids:
        raise RuntimeError(
            f"No videos found for channel: {channel!r}\n"
            "    Double-check the handle / URL and try again."
        )

    if max_videos and len(ids) > max_videos:
        ids = ids[:max_videos]
        if verbose:
            print_info(f"   Trimmed to {max_videos} videos (--max-videos)")

    urls   = [f"https://www.youtube.com/watch?v={v}" for v in ids]
    header = (
        f"# Channel : {ch_name}\n"
        f"# Videos  : {len(urls)}\n"
        f"# Created : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"# Next    : python yt_transcribe.py --file {out_file.name}\n\n"
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(header + "\n".join(urls) + "\n", encoding="utf-8")

    if verbose:
        print()
        print_info(f"✔ Saved → {out_file.resolve()}  ({len(urls)} URLs)")
        print_info(f"  Run: python yt_transcribe.py --file {out_file.name}")

    return len(urls)


# ── List available transcript languages ──────────────────────────────────────

def list_languages(video_id: str) -> None:
    api = _get_transcript_api()
    ver = _transcript_api_version()
    print(f"\n  Transcripts for: {video_id}")
    print(f"  youtube-transcript-api v{'.'.join(map(str, ver))}")
    print("  " + "─" * 50)
    try:
        listing = api.list(video_id) if ver >= (1, 0) else api.list_transcripts(video_id)
        found   = False
        for t in listing:
            found = True
            print(f"  [{t.language_code:8s}]  {t.language}  ({'auto' if t.is_generated else 'manual'})")
        if not found:
            print_info("No transcripts found.")
    except Exception as exc:
        print_err(f"Could not list transcripts: {exc}")


# ── Fetch a single transcript ─────────────────────────────────────────────────

def get_transcript(
    video_id  : str,
    lang      : str  = "en",
    timestamps: bool = False,
    verbose   : bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch the transcript for video_id.
    Returns (text, None) on success or (None, error_message) on failure.

    Language selection:
      1. Try the requested language directly.
      2. Try common fallback languages.
      3. Discover all available transcripts, pick the best
         (manual > auto-generated, preferred lang > other).
    """
    api = _get_transcript_api()
    ver = _transcript_api_version()

    if verbose:
        print_info(f"📝 Fetching  [youtube-transcript-api v{'.'.join(map(str, ver))}]")

    candidates: List[str] = []
    if lang and lang not in ("auto", ""):
        candidates.append(lang)
    for fb in ("en", "ar", "fr", "es", "de", "ja", "zh", "pt", "ru", "ko"):
        if fb not in candidates:
            candidates.append(fb)

    # ── v1.x (.fetch / .list) ─────────────────────────────────────
    if ver >= (1, 0):
        for candidate in candidates:
            try:
                if verbose:
                    print(f"     Trying: {candidate}...", end="\r", flush=True)
                fetched  = api.fetch(video_id, languages=[candidate])
                snippets = getattr(fetched, "snippets", fetched)
                text     = format_entries(snippets, timestamps)
                if text:
                    if verbose:
                        print(f"     ✔ Found captions: {candidate}          ")
                    return build_header(video_id, candidate) + text, None
            except Exception:
                pass

        try:
            if verbose:
                print_info("   Discovering available languages...")
            available = list(api.list(video_id))
            if not available:
                return None, "No transcripts available for this video."

            def _rank(t) -> int:
                return (0 if t.language_code in candidates else 2) + (1 if t.is_generated else 0)

            chosen   = min(available, key=_rank)
            if verbose:
                kind = "auto" if chosen.is_generated else "manual"
                print_info(f"   ✔ Using: {chosen.language} [{chosen.language_code}] ({kind})")

            snippets = getattr(chosen.fetch(), "snippets", chosen.fetch())
            text     = format_entries(snippets, timestamps)
            if text:
                return build_header(video_id, chosen.language_code) + text, None
            return None, "Transcript was empty."

        except Exception as exc:
            return None, f"No captions found: {exc}"

    # ── v0.x (.list_transcripts) ──────────────────────────────────
    else:
        try:
            tlist      = api.list_transcripts(video_id)
            transcript = None
            for finder in (
                lambda: tlist.find_manually_created_transcript(candidates),
                lambda: tlist.find_generated_transcript(candidates),
            ):
                try:
                    transcript = finder(); break
                except Exception:
                    pass
            if transcript is None:
                transcript = next(iter(tlist), None)
            if transcript is None:
                return None, "No transcripts available for this video."

            if verbose:
                print_info(f"   ✔ {transcript.language} ({'auto' if transcript.is_generated else 'manual'})")

            raw       = transcript.fetch()
            used_lang = getattr(transcript, "language_code", getattr(transcript, "language", lang))
            text      = format_entries(raw, timestamps)
            return (build_header(video_id, used_lang) + text, None) if text else (None, "Transcript was empty.")

        except Exception as exc:
            return None, f"Transcript fetch failed: {exc}"


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt_transcribe",
        description="Download YouTube transcripts from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s dQw4w9WgXcQ
  %(prog)s https://youtu.be/dQw4w9WgXcQ --lang fr --timestamps
  %(prog)s dQw4w9WgXcQ --list-langs
  %(prog)s dQw4w9WgXcQ --stdout
  %(prog)s --file urls.txt --output ./transcripts --lang en

  # Channel mode (requires yt-dlp, no API key)
  %(prog)s --channel @MrBeast
  %(prog)s --channel @NASA --max-videos 50 --urls-file nasa.txt

  # Full workflow
  %(prog)s --channel @veritasium && %(prog)s --file urls.txt
        """,
    )
    parser.add_argument("inputs", nargs="*", metavar="URL_OR_ID",
                        help="YouTube video URLs or bare 11-character video IDs.")
    parser.add_argument("--file", "-f", metavar="FILE",
                        help="Read URLs/IDs from a text file (one per line, # = comment).")
    parser.add_argument("--lang", "-l", default="en", metavar="LANG",
                        help="Preferred transcript language code (default: en).")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="Prefix every line with a [MM:SS] timestamp.")
    parser.add_argument("--output", "-o", default=None, metavar="DIR",
                        help=f"Directory to save transcripts (default: {DEFAULT_OUTPUT_DIR}).")
    parser.add_argument("--stdout", "-s", action="store_true",
                        help="Print transcript to stdout instead of saving to a file.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing transcript files.")
    parser.add_argument("--list-langs", action="store_true",
                        help="List all available transcript languages for the first input.")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress all progress output.")

    ch = parser.add_argument_group(
        "channel mode",
        "Scrape a channel's video list and save it as a urls.txt file.\n"
        "Requires yt-dlp — no API key needed.  (pip install yt-dlp)",
    )
    ch.add_argument("--channel", "-c", metavar="CHANNEL",
                    help="Channel @handle, full URL, or bare name.")
    ch.add_argument("--urls-file", metavar="FILE", default=str(DEFAULT_URLS_FILE),
                    help=f"Where to write channel URLs (default: {DEFAULT_URLS_FILE}).")
    ch.add_argument("--max-videos", metavar="N", type=int, default=0,
                    help="Limit the number of videos fetched (default: all).")
    return parser


def run_channel_mode(args: argparse.Namespace) -> None:
    print(f"\n📺  Channel → {args.urls_file}")
    print(f"  Channel : {args.channel}\n")
    try:
        count = channel_to_urls(
            channel    = args.channel,
            out_file   = Path(args.urls_file),
            max_videos = args.max_videos,
            verbose    = not args.quiet,
        )
        print(f"\n{'─' * 50}")
        print(f"  Done — {count} URLs written to {args.urls_file}\n")
    except RuntimeError as exc:
        print_err(str(exc)); sys.exit(1)


def run_transcript_mode(args: argparse.Namespace) -> None:
    raw_lines: List[str] = list(args.inputs)
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            print_err(f"File not found: {path}"); sys.exit(1)
        raw_lines.extend(path.read_text(encoding="utf-8").splitlines())

    if not raw_lines:
        build_parser().print_help(); sys.exit(0)

    video_ids = resolve_inputs(raw_lines)
    if not video_ids:
        print_err("No valid YouTube video IDs found."); sys.exit(1)

    if args.list_langs:
        list_languages(video_ids[0]); sys.exit(0)

    out_dir = Path(args.output) if args.output else DEFAULT_OUTPUT_DIR
    if not args.stdout:
        out_dir.mkdir(parents=True, exist_ok=True)

    verbose   = not args.quiet
    ok_count  = 0
    err_count = 0
    total     = len(video_ids)

    print(f"\n🎬  YT Transcriber — {total} video(s)\n")

    for i, vid_id in enumerate(video_ids, 1):
        if verbose:
            print(f"[{i}/{total}] https://www.youtube.com/watch?v={vid_id}")

        t0 = time.time()
        text, error = get_transcript(vid_id, lang=args.lang,
                                     timestamps=args.timestamps, verbose=verbose)
        elapsed = time.time() - t0

        if text:
            if args.stdout:
                print(text)
            else:
                fp = unique_output_path(out_dir, vid_id, force=args.force)
                fp.write_text(text, encoding="utf-8")
                if verbose:
                    print_info(f"✔ Saved → {fp}  ({fp.stat().st_size/1024:.1f} KB, {elapsed:.1f}s)")
            ok_count += 1
        else:
            print_err(f"Failed: {error}")
            err_count += 1

        if verbose and i < total:
            print()

    print(f"\n{'─' * 50}")
    print(f"  Done — ✔ {ok_count} succeeded  ✗ {err_count} failed")
    if not args.stdout and ok_count:
        print(f"  Saved to: {out_dir.resolve()}")
    print()
    sys.exit(0 if err_count == 0 else 1)


def main() -> None:
    args = build_parser().parse_args()
    if args.channel:
        run_channel_mode(args)
    else:
        run_transcript_mode(args)


if __name__ == "__main__":
    main()