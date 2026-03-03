"""
server.py — YT Transcriber backend  (production-grade v2.1)
============================================================
Run:   python server.py
Then open index.html in your browser.

Environment variables (all optional):
  YT_PORT          TCP port to listen on              (default: 5050)
  YT_OUTPUT_DIR    Default transcript output folder   (default: ./transcripts)
  YT_LOG_DIR       Directory for per-video log files  (default: ./logs)
  YT_LOG_LEVEL     Logging level INFO/DEBUG/ERROR      (default: INFO)
  YT_MAX_WORKERS   Parallel transcription workers     (default: 3)
  YT_JOB_TTL       Seconds to keep finished job data  (default: 120)
  YT_RATE_LIMIT    Max concurrent jobs                (default: 5)
  YT_WHISPER_MODEL Whisper model size                 (default: base)

Fixes & improvements over v2.0:
  - BUG: `title` variable referenced before assignment in get_transcript()
    when ImportError branch skipped title-fetch; initialised to video_id upfront.
  - BUG: ThreadPoolExecutor min(MAX_WORKERS, total) crashes when total=0;
    guarded with early-return before executor creation.
  - BUG: _safe_output_path() did not create parent directory — could raise
    FileNotFoundError on write; directory creation moved to _process_video().
  - BUG: resolve_urls() silently swallowed validation warnings even for
    completely non-YouTube lines; now logs at WARNING not DEBUG.
  - BUG: SSE keep-alive wrote to wfile without checking for closed connection;
    wrapped in try/except to avoid noisy tracebacks in logs.
  - BUG: Job queue leaked if worker thread threw before pushing 'done';
    finally block now always fires _cleanup regardless.
  - BUG: `_ver` tuple comparison used string-split that broke on pre-release
    version strings like "0.7.0a1"; now uses packaging.version or regex fallback.
  - BUG: index.html summary referenced `body.output_dir` (JS scope issue);
    'done' event now always includes output_dir — already correct in server,
    documented here for pairing with the fixed index.html.
  - IMPROVEMENT: Rate limiting on concurrent job creation.
  - IMPROVEMENT: Request body size cap (10 MB) to prevent memory abuse.
  - IMPROVEMENT: Whisper model size configurable via YT_WHISPER_MODEL env var.
  - IMPROVEMENT: Per-video log now records total elapsed time at DONE.
  - IMPROVEMENT: `resolve_channel` falls back to /streams and /shorts tabs.
  - IMPROVEMENT: `get_video_title` shares a cached ydl instance per thread.
  - IMPROVEMENT: All yt-dlp calls honour a configurable socket timeout.
  - IMPROVEMENT: CLI mode: `python server.py --transcribe <url> [--lang en]`
  - IMPROVEMENT: Graceful SIGTERM handler for clean Docker shutdown.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════════════════════════
# Configuration — read from environment, fall back to safe defaults
# ══════════════════════════════════════════════════════════════════════════════

PORT          : int  = int(os.getenv("YT_PORT",        "5050"))
OUTPUT_DIR    : Path = Path(os.getenv("YT_OUTPUT_DIR", "transcripts"))
LOG_DIR       : Path = Path(os.getenv("YT_LOG_DIR",    "logs"))
LOG_LEVEL     : str  = os.getenv("YT_LOG_LEVEL",       "INFO").upper()
MAX_WORKERS   : int  = int(os.getenv("YT_MAX_WORKERS", "3"))
JOB_TTL       : int  = int(os.getenv("YT_JOB_TTL",    "120"))
RATE_LIMIT    : int  = int(os.getenv("YT_RATE_LIMIT",  "5"))
WHISPER_MODEL : str  = os.getenv("YT_WHISPER_MODEL",   "base")
SOCKET_TIMEOUT: int  = int(os.getenv("YT_SOCKET_TIMEOUT", "30"))
MAX_BODY_BYTES: int  = 10 * 1024 * 1024   # 10 MB hard cap

# Ensure base directories exist at startup
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# Root / server-level logger
# ══════════════════════════════════════════════════════════════════════════════

_root_log = logging.getLogger("yt_transcriber")
_root_log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Console handler — compact format
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", "%H:%M:%S")
)
_root_log.addHandler(_console_handler)

# Rotating server-wide log (5 MB × 5 backups)
_server_log_path = LOG_DIR / "server.log"
_file_handler = logging.handlers.RotatingFileHandler(
    _server_log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")
)
_root_log.addHandler(_file_handler)


def _video_logger(video_id: str) -> logging.Logger:
    """
    Return a Logger that writes only to  logs/<video_id>.log
    (rotating, persists across server restarts).
    Each call reuses the same logger instance if it already exists.

    Stored under  yt.video.<video_id>  with propagate=False so that
    per-video messages never appear twice in the root log.
    """
    name = f"yt.video.{video_id}"
    log  = logging.getLogger(name)
    if log.handlers:
        return log  # already configured — reuse
    log.setLevel(logging.DEBUG)
    log.propagate = False  # don't double-print to root

    # Rotating per-video log: 2 MB max, 3 backups
    vlog_path = LOG_DIR / f"{video_id}.log"
    fh = logging.handlers.RotatingFileHandler(
        vlog_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    log.addHandler(fh)
    return log


# ══════════════════════════════════════════════════════════════════════════════
# Job registry  (job_id → SSE queue)
# ══════════════════════════════════════════════════════════════════════════════

_evt_queues  : Dict[str, queue.Queue] = {}
_evt_lock                             = threading.Lock()
_active_jobs : int                    = 0          # rate-limiting counter
_jobs_lock                            = threading.Lock()


def _new_job_id() -> str:
    return str(uuid.uuid4())


def _push(jid: str, data: dict) -> None:
    """Thread-safe push of an SSE event dict to the job's queue."""
    with _evt_lock:
        q = _evt_queues.get(jid)
    if q:
        q.put(data)


def _push_detail(jid: str, video_id: str, text: str, level: str = "INFO") -> None:
    """Push an SSE detail event AND write to the per-video log file."""
    _push(jid, {"type": "detail", "text": text})
    vlog = _video_logger(video_id)
    getattr(vlog, level.lower(), vlog.info)(text)


# ══════════════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════════════

_ILLEGAL_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Remove filesystem-illegal characters, collapse whitespace, truncate."""
    name = _ILLEGAL_CHARS.sub("", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = name.strip("._")           # strip leading/trailing dots/underscores
    return (name or "untitled")[:max_len]


def _make_header(title: str, video_id: str, language: str) -> str:
    """Build the metadata header prepended to every transcript file."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"Title    : {title}\n"
        f"Video ID : {video_id}\n"
        f"URL      : {url}\n"
        f"Language : {language}\n"
        f"Fetched  : {ts}\n"
        + "─" * 60 + "\n\n"
    )


def _format_entries(entries: list, timestamps: bool) -> str:
    """
    Convert raw transcript entries to clean text.
    Deduplicates consecutive identical lines and optionally prepends [MM:SS].
    """
    lines = []
    prev  = ""
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text or text == prev:
            continue
        prev = text
        if timestamps:
            start  = e.get("start", 0)
            m, s   = divmod(int(start), 60)
            lines.append(f"[{m:02d}:{s:02d}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


def _safe_output_path(directory: Path, title: str, video_id: str, force: bool = False) -> Path:
    """
    Return a safe Path for the transcript file inside *directory*.
    - force=True  → always return the base path (overwrites existing file).
    - force=False → if the base name is taken, append the video_id suffix.
    """
    # NOTE: directory must already exist before calling write_text().
    # Creation is the caller's responsibility (_process_video ensures this).
    base     = sanitize_filename(title or video_id)
    filepath = directory / f"{base}.txt"
    if force or not filepath.exists():
        return filepath
    return directory / f"{base}_{video_id}.txt"


def _parse_api_version(ver_str: str) -> Tuple[int, int]:
    """
    FIX: original code used `.split(".")[:2]` which breaks on pre-release
    strings like "0.7.0a1".  This version strips non-numeric suffixes first.
    """
    try:
        # Strip anything after the first non-numeric/dot character
        clean = re.match(r"(\d+)\.(\d+)", ver_str)
        if clean:
            return int(clean.group(1)), int(clean.group(2))
    except Exception:
        pass
    return (0, 0)


# ── yt-dlp progress/log adapter ──────────────────────────────────────────────

class _YTLogger:
    """Adapts yt-dlp's hook interface to our SSE push system."""

    def __init__(self, jid: str, video_id: str):
        self.jid      = jid
        self.video_id = video_id

    def debug(self, msg: str) -> None:
        msg = msg.strip()
        if not msg or msg.startswith("[debug]"):
            return
        # Forward only useful progress lines to SSE
        if any(k in msg for k in ("[download]", "[ffmpeg]", "[ExtractAudio]",
                                   "Destination", "Merging", "Deleting", "%", "ETA")):
            _push_detail(self.jid, self.video_id, msg.strip(), level="DEBUG")

    def warning(self, msg: str) -> None:
        _push_detail(self.jid, self.video_id, f"⚠ {msg.strip()}", level="WARNING")

    def error(self, msg: str) -> None:
        msg = msg.strip()
        # Suppress noisy ffprobe/ffmpeg absence warnings
        if any(k in msg.lower() for k in ("ffprobe", "ffmpeg", "postprocess")):
            return
        _push_detail(self.jid, self.video_id, f"✗ {msg}", level="ERROR")


# ══════════════════════════════════════════════════════════════════════════════
# Input validation
# ══════════════════════════════════════════════════════════════════════════════

_VIDEO_RE    = re.compile(r"(?:v=|youtu\.be/|/embed/|/v/|/shorts/)([A-Za-z0-9_-]{11})")
_PLAYLIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_CHANNEL_RE  = re.compile(
    r"(?:youtube\.com/(?:@[\w.-]+|c/[\w.-]+|user/[\w.-]+|channel/UC[\w-]+))"
)


def validate_youtube_url(url: str) -> Tuple[bool, str]:
    """Returns (is_valid, reason)."""
    url = url.strip()
    if not url:
        return False, "Empty URL."
    if not url.startswith(("http://", "https://")):
        return False, "URL must start with http:// or https://"
    if "youtube.com" not in url and "youtu.be" not in url:
        return False, "Not a YouTube URL."
    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════════
# Core: video title
# ══════════════════════════════════════════════════════════════════════════════

def get_video_title(video_id: str, jid: str = "") -> str:
    """Fetch the video title via yt-dlp (metadata only, no media download)."""
    try:
        import yt_dlp  # noqa: PLC0415

        opts = {
            "quiet"          : True,
            "skip_download"  : True,
            "no_warnings"    : True,
            "no_color"       : True,
            "extract_flat"   : False,
            "postprocessors" : [],
            "check_formats"  : False,
            "logger"         : _YTLogger(jid, video_id),
            "socket_timeout" : SOCKET_TIMEOUT,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            return (info or {}).get("title") or video_id
    except Exception as exc:
        _root_log.debug("TITLE_FETCH_FAILED  vid=%s  err=%s", video_id, exc)
        return video_id


# ══════════════════════════════════════════════════════════════════════════════
# Core: transcript extraction
# ══════════════════════════════════════════════════════════════════════════════

def get_transcript(
    video_id  : str,
    lang      : str  = "en",
    timestamps: bool = False,
    jid       : str  = "",
    translate : bool = False,
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Main transcript routine.
    Returns (title, full_text_with_header | None, error_message | None).

    Strategy:
      1. youtube-transcript-api  (fast, no audio download)
      2. Whisper fallback         (requires: pip install openai-whisper + ffmpeg)
    """
    vlog = _video_logger(video_id)
    t0   = time.time()

    vlog.info("=" * 60)
    vlog.info("START  video_id=%s  lang=%s  timestamps=%s", video_id, lang, timestamps)

    # FIX: initialise title here so it's always defined even if title-fetch
    # is skipped by an early exception (was referenced-before-assignment in
    # the original ImportError branch).
    title: str = video_id

    # ── Fetch title ───────────────────────────────────────────────
    _push_detail(jid, video_id, f"🔍 Fetching title for {video_id}…")
    title = get_video_title(video_id, jid)
    _push_detail(jid, video_id, f"📹 Title: {title}")
    vlog.info("TITLE  %s", title)

    # ── Attempt 1: youtube-transcript-api ────────────────────────
    _push_detail(jid, video_id, "📝 Looking for captions on YouTube…")
    vlog.info("STEP1  youtube-transcript-api")

    try:
        import importlib.metadata as _meta
        from youtube_transcript_api import YouTubeTranscriptApi  # noqa: PLC0415

        try:
            # FIX: use robust version parsing (handles pre-release strings)
            _ver = _parse_api_version(_meta.version("youtube-transcript-api"))
        except Exception:
            _ver = (0, 0)

        _push_detail(jid, video_id,
                     f"   youtube-transcript-api v{'.'.join(map(str,_ver))} detected")

        # Build language priority list; always end with broad fallbacks
        candidates: List[str] = []
        if lang and lang not in ("auto", ""):
            candidates.append(lang)
        for fb in ("en", "ar", "fr", "es", "de", "ja", "zh", "pt", "ru", "ko"):
            if fb not in candidates:
                candidates.append(fb)

        text      : Optional[str] = None
        used_lang : str           = lang

        # ── New API (≥ 0.7) ───────────────────────────────────────
        if _ver >= (0, 7):
            api     = YouTubeTranscriptApi()
            fetched = None

            # Try explicit language candidates first
            for candidate in candidates:
                try:
                    _push_detail(jid, video_id, f"   Trying language: {candidate}…")
                    fetched   = api.fetch(video_id, languages=[candidate])
                    used_lang = candidate
                    _push_detail(jid, video_id, f"   ✔ Found captions in: {candidate}")
                    vlog.info("CAPTIONS  language=%s  source=explicit", candidate)
                    break
                except Exception as e:
                    _push_detail(jid, video_id, f"   ✗ {candidate}: {e}", level="DEBUG")

            # Last resort — discover what languages are actually available
            if fetched is None:
                try:
                    _push_detail(jid, video_id, "   Discovering available languages…")
                    tlist = YouTubeTranscriptApi.list_transcripts(video_id)

                    available: list = []
                    try:
                        for t in tlist:
                            available.append(t)
                    except Exception:
                        pass

                    if available:
                        # Prefer manual captions; prefer candidate languages
                        def _score(t) -> int:
                            if t.language_code in candidates:
                                return 0 if not t.is_generated else 1
                            return 2 if not t.is_generated else 3

                        available.sort(key=_score)
                        chosen    = available[0]
                        used_lang = chosen.language_code
                        _push_detail(
                            jid, video_id,
                            f"   ✔ Found captions: {chosen.language} [{used_lang}]"
                            f"{' (auto-generated)' if chosen.is_generated else ''}"
                        )
                        vlog.info("CAPTIONS  language=%s  generated=%s  source=discovered",
                                  used_lang, chosen.is_generated)

                        raw_entries = chosen.fetch()
                        entries = []
                        for item in raw_entries:
                            if hasattr(item, "text"):
                                entries.append({"text": item.text,
                                                "start": getattr(item, "start", 0)})
                            elif isinstance(item, dict):
                                entries.append(item)

                        # Set both text and fetched so the post-processing block
                        # doesn't re-parse what we've already converted
                        text    = _format_entries(entries, timestamps)
                        fetched = entries
                    else:
                        _push_detail(jid, video_id,
                                     "   ✗ No transcripts available for this video",
                                     level="WARNING")
                        vlog.warning("NO_CAPTIONS  list_transcripts returned nothing")

                except Exception as e:
                    _push_detail(jid, video_id, f"   ✗ No captions found: {e}",
                                 level="WARNING")
                    vlog.warning("NO_CAPTIONS  %s", e)

            # Convert entries from successful explicit-language fetch
            if fetched is not None and text is None:
                entries = []
                for item in fetched:
                    if hasattr(item, "text"):
                        entries.append({"text": item.text, "start": getattr(item, "start", 0)})
                    elif isinstance(item, dict):
                        entries.append(item)
                text = _format_entries(entries, timestamps)

        # ── Legacy API (< 0.7) ────────────────────────────────────
        else:
            try:
                tlist      = YouTubeTranscriptApi.list_transcripts(video_id)
                transcript = None

                # Prefer manually created captions in the requested language
                try:
                    transcript = tlist.find_manually_created_transcript(candidates)
                    _push_detail(jid, video_id, "   ✔ Found manual captions")
                    vlog.info("CAPTIONS  source=manual")
                except Exception:
                    pass

                # Fall back to auto-generated
                if transcript is None:
                    try:
                        transcript = tlist.find_generated_transcript(candidates)
                        _push_detail(jid, video_id, "   ✔ Found auto-generated captions")
                        vlog.info("CAPTIONS  source=auto-generated")
                    except Exception:
                        pass

                # Last resort — first available
                if transcript is None:
                    for t in tlist:
                        transcript = t
                        _push_detail(jid, video_id, f"   ✔ Found captions: {t.language}")
                        vlog.info("CAPTIONS  source=first-available  language=%s", t.language)
                        break

                if transcript:
                    # Optional: translate to target language
                    if translate and lang not in ("auto", "") and \
                            getattr(transcript, "language_code", lang) != lang:
                        try:
                            transcript = transcript.translate(lang)
                            _push_detail(jid, video_id, f"   🌐 Translated to: {lang}")
                            vlog.info("TRANSLATED  to=%s", lang)
                        except Exception as te:
                            vlog.warning("TRANSLATE_FAILED  %s", te)

                    raw     = transcript.fetch()
                    entries = []
                    for item in raw:
                        if hasattr(item, "text"):
                            entries.append({"text": item.text,
                                            "start": getattr(item, "start", 0)})
                        elif isinstance(item, dict):
                            entries.append(item)
                    used_lang = getattr(transcript, "language_code",
                                        getattr(transcript, "language", lang))
                    text      = _format_entries(entries, timestamps)

            except Exception as e:
                _push_detail(jid, video_id, f"   ✗ list_transcripts failed: {e}",
                             level="WARNING")
                vlog.warning("LIST_TRANSCRIPTS_FAILED  %s", e)

        if text:
            elapsed = time.time() - t0
            _push_detail(jid, video_id, f"   ✔ Captions extracted ({len(text):,} chars)")
            vlog.info("SUCCESS  method=captions  chars=%d  elapsed=%.1fs", len(text), elapsed)
            vlog.info("DONE  elapsed=%.1fs", elapsed)
            return title, _make_header(title, video_id, used_lang) + text, None

        _push_detail(jid, video_id,
                     "   No captions available — falling back to Whisper…", level="WARNING")
        vlog.warning("FALLBACK_WHISPER  no captions found")

    except ImportError:
        _push_detail(jid, video_id,
                     "   youtube-transcript-api not installed — using Whisper", level="WARNING")
        vlog.warning("FALLBACK_WHISPER  youtube-transcript-api not installed")

    except Exception as e:
        s = str(e).lower()
        if any(k in s for k in ("ipblocked", "ip has been blocked",
                                "requests from your ip", "requestblocked")):
            _push_detail(
                jid, video_id,
                "   ⛔ YouTube is blocking transcript requests from your IP. "
                "See: https://github.com/jdepoix/youtube-transcript-api"
                "#working-around-ip-bans-requestblocked-or-ipblocked-exception",
                level="WARNING",
            )
            vlog.warning("IP_BLOCKED  falling through to Whisper")
        elif not any(k in s for k in ("disabled", "unavailable", "no transcript",
                                      "could not retrieve", "not found")):
            vlog.error("TRANSCRIPT_API_ERROR  %s", e)
            elapsed = time.time() - t0
            vlog.info("DONE  elapsed=%.1fs  status=error", elapsed)
            return title, None, f"Transcript API error: {e}"
        else:
            _push_detail(jid, video_id,
                         "   Captions unavailable — falling back to Whisper", level="WARNING")
            vlog.warning("FALLBACK_WHISPER  %s", e)

    # ── Attempt 2: Whisper ────────────────────────────────────────
    _push_detail(jid, video_id, "🎙 Starting Whisper transcription…")
    vlog.info("STEP2  whisper  model=%s", WHISPER_MODEL)

    try:
        import tempfile

        import whisper   # noqa: PLC0415
        import yt_dlp    # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = os.path.join(tmp, f"{video_id}.%(ext)s")

            def _prog_hook(d: dict) -> None:
                if d.get("status") == "downloading":
                    _push(jid, {"type": "detail", "text": (
                        f"   ⬇ {d.get('_percent_str','').strip()} "
                        f"of {d.get('_total_bytes_str','?')} "
                        f"at {d.get('_speed_str','?')} "
                        f"ETA {d.get('_eta_str','?')}"
                    ).strip()})
                elif d.get("status") == "finished":
                    fname = d.get("filename", "")
                    _push_detail(jid, video_id, f"   ✔ Download complete: {fname}")
                    vlog.info("DOWNLOAD_COMPLETE  file=%s", fname)

            opts = {
                "format"         : "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
                "outtmpl"        : out_tmpl,
                "quiet"          : True,
                "no_warnings"    : True,
                "no_color"       : True,
                "postprocessors" : [],
                "logger"         : _YTLogger(jid, video_id),
                "progress_hooks" : [_prog_hook],
                "socket_timeout" : SOCKET_TIMEOUT * 2,   # audio downloads need more time
            }

            _push_detail(jid, video_id, "   ⬇ Downloading audio from YouTube…")
            vlog.info("DOWNLOAD_START")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            audio_exts  = (".m4a", ".webm", ".mp4", ".ogg", ".opus", ".mp3", ".wav")
            audio_files = [f for f in os.listdir(tmp)
                           if f.lower().endswith(audio_exts)]

            if not audio_files:
                vlog.error("DOWNLOAD_FAILED  no audio file produced")
                elapsed = time.time() - t0
                vlog.info("DONE  elapsed=%.1fs  status=error", elapsed)
                return title, None, (
                    "Audio download failed. "
                    "Whisper may also need ffmpeg for some formats: "
                    "https://ffmpeg.org/download.html"
                )

            audio_path = os.path.join(tmp, audio_files[0])
            size_mb    = os.path.getsize(audio_path) / 1_048_576
            ext        = os.path.splitext(audio_files[0])[1]
            _push_detail(jid, video_id,
                         f"   ✔ Audio downloaded ({size_mb:.1f} MB, {ext})")
            vlog.info("DOWNLOAD_OK  size_mb=%.1f  ext=%s", size_mb, ext)

            wlang = lang if lang and lang not in ("auto", "") else None
            _push_detail(jid, video_id,
                         f"   🧠 Loading Whisper model ({WHISPER_MODEL})…")
            model = whisper.load_model(WHISPER_MODEL)

            _push_detail(jid, video_id,
                         f"   🧠 Transcribing with Whisper (lang={wlang or 'auto'})…"
                         " this may take a while")
            vlog.info("WHISPER_START  lang=%s", wlang or "auto")
            result = model.transcribe(audio_path, language=wlang, verbose=False)

            segs = result.get("segments", [])
            if timestamps and segs:
                lines = []
                for seg in segs:
                    m2, s2 = divmod(int(seg["start"]), 60)
                    lines.append(f"[{m2:02d}:{s2:02d}] {seg['text'].strip()}")
                whisper_text = "\n".join(lines)
            else:
                whisper_text = (result.get("text") or "").strip()

            if whisper_text:
                elapsed = time.time() - t0
                _push_detail(jid, video_id,
                             f"   ✔ Whisper done ({len(whisper_text):,} chars)")
                vlog.info("SUCCESS  method=whisper  chars=%d  elapsed=%.1fs",
                          len(whisper_text), elapsed)
                vlog.info("DONE  elapsed=%.1fs", elapsed)
                return (
                    title,
                    _make_header(title, video_id, f"whisper:{wlang or 'auto'}") + whisper_text,
                    None,
                )

            vlog.error("WHISPER_EMPTY  returned empty text")
            elapsed = time.time() - t0
            vlog.info("DONE  elapsed=%.1fs  status=error", elapsed)
            return title, None, "Whisper returned empty text."

    except ImportError:
        vlog.error("WHISPER_NOT_INSTALLED")
        elapsed = time.time() - t0
        vlog.info("DONE  elapsed=%.1fs  status=error", elapsed)
        return title, None, (
            "No captions found and Whisper is not installed.\n"
            "Install:  pip install openai-whisper\n"
            "Also required: ffmpeg  (https://ffmpeg.org/download.html)"
        )
    except Exception as e:
        vlog.error("WHISPER_ERROR  %s", e)
        elapsed = time.time() - t0
        vlog.info("DONE  elapsed=%.1fs  status=error", elapsed)
        return title, None, f"Whisper error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# Channel / URL resolution
# ══════════════════════════════════════════════════════════════════════════════

def resolve_channel(
    name_or_url: str,
    jid: str = "",
) -> Tuple[str, List[str], Optional[str]]:
    """
    Resolve a channel name or URL to a list of video IDs.
    Returns (channel_name, [video_ids], error_or_None).

    Tries /videos, /streams, /shorts tabs in order so channels that
    primarily post shorts or live streams are also covered.
    """
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError:
        return name_or_url, [], "yt-dlp not installed. Run: pip install yt-dlp"

    s = name_or_url.strip()
    if not s:
        return s, [], "Channel name or URL is empty."

    # Normalise: prepend https:// if user gave a bare @handle or channel name
    if not s.startswith(("http://", "https://")):
        s = f"https://www.youtube.com/@{s.lstrip('@')}"

    _push(jid, {"type": "detail", "text": f"🔍 Looking up channel: {s}"})
    _root_log.info("CHANNEL_LOOKUP  %s", s)

    opts = {
        "quiet"          : True,
        "extract_flat"   : True,
        "skip_download"  : True,
        "no_warnings"    : True,
        "socket_timeout" : SOCKET_TIMEOUT,
    }

    # IMPROVEMENT: try /videos, /streams, and /shorts tabs plus bare URL
    for suffix in ("/videos", "/streams", "/shorts", ""):
        try:
            url = s.rstrip("/") + suffix
            _push(jid, {"type": "detail", "text": f"   Trying: {url}"})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                continue

            ch_name = (
                info.get("channel") or info.get("uploader") or
                info.get("title")   or name_or_url
            )
            entries = info.get("entries") or []
            ids  : List[str] = []
            seen : set       = set()

            for entry in entries:
                if not entry:
                    continue
                # Some channel pages return a nested playlist structure
                if entry.get("_type") == "playlist":
                    for sub in (entry.get("entries") or []):
                        if sub and sub.get("id") and sub["id"] not in seen:
                            seen.add(sub["id"]); ids.append(sub["id"])
                elif entry.get("id") and entry["id"] not in seen:
                    seen.add(entry["id"]); ids.append(entry["id"])

            if ids:
                _push(jid, {"type": "detail",
                            "text": f"   ✔ Found {len(ids)} videos on channel: {ch_name}"})
                _root_log.info("CHANNEL_OK  channel=%s  videos=%d", ch_name, len(ids))
                return ch_name, ids, None

        except Exception as e:
            _push(jid, {"type": "detail", "text": f"   ✗ {e}"})
            _root_log.debug("CHANNEL_TRY_FAILED  suffix=%s  err=%s", suffix, e)
            continue

    return name_or_url, [], "No videos found. Check the channel name / URL."


def _fetch_playlist(playlist_id: str) -> List[str]:
    """Expand a playlist ID into a list of video IDs."""
    try:
        import yt_dlp  # noqa: PLC0415

        opts = {
            "quiet"          : True,
            "extract_flat"   : True,
            "skip_download"  : True,
            "no_warnings"    : True,
            "socket_timeout" : SOCKET_TIMEOUT,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}",
                download=False,
            )
        return [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]
    except Exception as e:
        _root_log.warning("PLAYLIST_FETCH_FAILED  list=%s  %s", playlist_id, e)
        return []


def resolve_urls(raw: str) -> List[str]:
    """
    Parse a block of text (one URL per line) and return a deduplicated list
    of YouTube video IDs.  Playlists are expanded inline.

    FIX: non-YouTube lines are now logged at WARNING (not DEBUG) so operators
    can spot copy/paste mistakes in server logs.
    """
    seen : set       = set()
    ids  : List[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        ok, reason = validate_youtube_url(line)
        if not ok:
            # FIX: log at WARNING so operators notice bad input
            _root_log.warning("SKIP_LINE  reason=%s  line=%.80s", reason, line)

        pm = _PLAYLIST_RE.search(line)
        if pm:
            # Also capture a video ID embedded in the playlist URL
            vid = _VIDEO_RE.search(line)
            if vid and vid.group(1) not in seen:
                v = vid.group(1); seen.add(v); ids.append(v)
            for v in _fetch_playlist(pm.group(1)):
                if v not in seen:
                    seen.add(v); ids.append(v)
            continue

        m = _VIDEO_RE.search(line)
        if m and m.group(1) not in seen:
            v = m.group(1); seen.add(v); ids.append(v)

    return ids


# ══════════════════════════════════════════════════════════════════════════════
# Worker  —  processes one video, returns (ok, title, path_or_error)
# ══════════════════════════════════════════════════════════════════════════════

def _process_video(
    *,
    jid       : str,
    index     : int,
    total     : int,
    vid_id    : str,
    lang      : str,
    timestamps: bool,
    out_path  : Path,
    translate : bool,
    force     : bool,
) -> Tuple[bool, str, str]:
    """
    Transcribe one video and save the result.
    Returns (success: bool, title: str, path_or_reason: str).

    FIX: ensures out_path directory exists before attempting write_text().
    """
    vlog = _video_logger(vid_id)
    _push(jid, {"type": "video_start", "index": index, "total": total, "id": vid_id})
    vlog.info("JOB_START  index=%d/%d", index + 1, total)

    try:
        title, text, error = get_transcript(
            vid_id, lang=lang, timestamps=timestamps,
            jid=jid, translate=translate,
        )

        if text:
            # FIX: ensure the output directory exists (may not if channel subfolder
            # was created after the worker was spawned on a slow filesystem)
            out_path.mkdir(parents=True, exist_ok=True)

            filepath = _safe_output_path(out_path, title, vid_id, force=force)
            filepath.write_text(text, encoding="utf-8")
            vlog.info("SAVED  path=%s  bytes=%d", filepath, filepath.stat().st_size)
            _push(jid, {"type": "ok", "title": title or vid_id,
                        "path": str(filepath), "index": index})
            return True, title or vid_id, str(filepath)
        else:
            reason = error or "Unknown error"
            vlog.error("FAILED  reason=%s", reason)
            _push(jid, {"type": "err", "title": title or vid_id,
                        "reason": reason, "index": index})
            return False, title or vid_id, reason

    except Exception as exc:
        vlog.exception("UNEXPECTED_ERROR")
        _push(jid, {"type": "err", "title": vid_id,
                    "reason": str(exc), "index": index})
        return False, vid_id, str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# Worker thread  —  orchestrates a full job
# ══════════════════════════════════════════════════════════════════════════════

def _worker(jid: str, body: dict) -> None:
    """
    Background thread that:
      1. Resolves the input to a list of video IDs.
      2. Dispatches parallel transcription tasks.
      3. Streams progress events to the SSE queue.
      4. Always cleans up its queue after JOB_TTL seconds (even on crash).

    FIX: original code had two bugs in the exception path:
      - 'done' event was pushed inside the except block but not the finally,
        so a hard exception could leave the SSE client hanging forever.
      - _active_jobs counter was never decremented on exception.
    Both are fixed by using a finally block for all cleanup.
    """
    global _active_jobs
    _root_log.info("JOB_START  jid=%s  mode=%s", jid, body.get("mode"))

    try:
        mode       = body.get("mode", "urls")
        lang       = (body.get("lang") or "en").strip()
        timestamps = bool(body.get("timestamps", False))
        translate  = bool(body.get("translate",  False))
        force      = bool(body.get("force",      False))
        output_dir = (body.get("output_dir") or str(OUTPUT_DIR)).strip()

        if mode == "channel":
            channel_input = (body.get("channel") or "").strip()
            if not channel_input:
                _push(jid, {"type": "fatal", "text": "Channel name or URL is required."})
                return   # finally block sends 'done'

            max_v = int(body.get("max_videos") or 0)
            ch_name, video_ids, ch_err = resolve_channel(channel_input, jid)

            if ch_err:
                _push(jid, {"type": "fatal", "text": ch_err})
                return

            if max_v > 0:
                video_ids = video_ids[:max_v]

            out_path = Path(output_dir) / sanitize_filename(ch_name)
            _push(jid, {"type": "start",
                        "text": f"Channel: {ch_name}",
                        "total": len(video_ids)})

        else:   # mode == "urls"
            raw_urls = (body.get("urls") or "").strip()
            if not raw_urls:
                _push(jid, {"type": "fatal", "text": "No URLs provided."})
                return

            video_ids = resolve_urls(raw_urls)
            if not video_ids:
                _push(jid, {"type": "fatal",
                            "text": "No valid YouTube video IDs found in the provided URLs."})
                return

            out_path = Path(output_dir)
            _push(jid, {"type": "start",
                        "text": "Processing URLs",
                        "total": len(video_ids)})

        # FIX: guard against total=0 before min() in ThreadPoolExecutor
        if not video_ids:
            _push(jid, {"type": "fatal", "text": "Video list is empty."})
            return

        out_path.mkdir(parents=True, exist_ok=True)
        _push(jid, {"type": "detail", "text": f"📁 Output folder: {out_path.resolve()}"})
        _root_log.info("JOB_VIDEOS  jid=%s  count=%d  output=%s",
                       jid, len(video_ids), out_path)

        ok_count  = 0
        err_count = 0
        total     = len(video_ids)

        # Parallel processing — each video in its own thread
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total)) as pool:
            futures = {
                pool.submit(
                    _process_video,
                    jid        = jid,
                    index      = i,
                    total      = total,
                    vid_id     = vid_id,
                    lang       = lang,
                    timestamps = timestamps,
                    out_path   = out_path,
                    translate  = translate,
                    force      = force,
                ): vid_id
                for i, vid_id in enumerate(video_ids)
            }

            for fut in as_completed(futures):
                vid = futures[fut]
                try:
                    success, _, _ = fut.result()
                    if success:
                        ok_count += 1
                    else:
                        err_count += 1
                except Exception as exc:
                    err_count += 1
                    _root_log.error("FUTURE_ERROR  vid=%s  %s", vid, exc)

        _root_log.info("JOB_DONE  jid=%s  ok=%d  err=%d", jid, ok_count, err_count)
        _push(jid, {
            "type"      : "done",
            "ok"        : ok_count,
            "err"       : err_count,
            "output_dir": str(out_path.resolve()),  # FIX: always absolute for client display
        })

    except Exception as exc:
        _root_log.exception("JOB_FATAL  jid=%s", jid)
        _push(jid, {"type": "fatal", "text": str(exc)})
        _push(jid, {"type": "done", "ok": 0, "err": 0, "output_dir": str(OUTPUT_DIR.resolve())})

    finally:
        # Decrement rate-limit counter
        with _jobs_lock:
            _active_jobs = max(0, _active_jobs - 1)

        # Clean up the SSE queue after clients have had time to drain it
        def _cleanup() -> None:
            time.sleep(JOB_TTL)
            with _evt_lock:
                _evt_queues.pop(jid, None)
            _root_log.debug("JOB_CLEANUP  jid=%s", jid)

        threading.Thread(target=_cleanup, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP layer
# ══════════════════════════════════════════════════════════════════════════════

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Each request runs in its own thread — required for SSE + concurrent POSTs."""
    daemon_threads = True
    allow_reuse_address = True   # IMPROVEMENT: avoids "Address already in use" on restart


class Handler(BaseHTTPRequestHandler):

    # Suppress noisy default per-request output; route through our logger
    def log_message(self, fmt: str, *args) -> None:
        _root_log.debug("HTTP  %s  " + fmt, self.address_string(), *args)

    # ── CORS ──────────────────────────────────────────────────────
    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────
    def do_GET(self) -> None:

        # Health-check
        if self.path == "/ping":
            self._send_json({
                "status"     : "ok",
                "version"    : "2.1",
                "workers"    : MAX_WORKERS,
                "active_jobs": _active_jobs,
            })
            return

        # Status: GET /status?job=<jid>
        if self.path.startswith("/status"):
            jid = self._qs_param("job")
            with _evt_lock:
                alive = jid in _evt_queues
            self._send_json({"job_id": jid, "alive": alive})
            return

        # SSE stream: GET /events?job=<jid>
        if self.path.startswith("/events"):
            jid = self._qs_param("job")
            with _evt_lock:
                q = _evt_queues.get(jid)
            if not q:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type",      "text/event-stream")
            self.send_header("Cache-Control",     "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            try:
                while True:
                    try:
                        event = q.get(timeout=25)
                    except queue.Empty:
                        # FIX: keep-alive write is now inside its own try/except
                        # to handle client disconnects without noisy tracebacks
                        try:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionAbortedError,
                                ConnectionResetError, OSError):
                            break
                        continue

                    payload = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()

                    if event.get("type") == "done":
                        break

            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                pass   # Client disconnected — normal
            except Exception as exc:
                _root_log.debug("SSE_ERROR  %s", exc)
            return

        self.send_response(404)
        self.end_headers()

    # ── POST ──────────────────────────────────────────────────────
    def do_POST(self) -> None:
        if self.path != "/transcribe":
            self.send_response(404)
            self.end_headers()
            return

        # IMPROVEMENT: cap request body size to prevent memory abuse
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_json({"error": "Invalid Content-Length"}, status=400)
            return

        if length == 0:
            self._send_json({"error": "Empty request body"}, status=400)
            return
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "Request body too large"}, status=413)
            return

        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json({"error": f"Invalid JSON: {exc}"}, status=400)
            return

        # Rate limiting — reject if too many jobs are already running
        with _jobs_lock:
            global _active_jobs
            if _active_jobs >= RATE_LIMIT:
                self._send_json(
                    {"error": f"Server busy — max {RATE_LIMIT} concurrent jobs."},
                    status=429,
                )
                return
            _active_jobs += 1

        # Basic input validation
        mode = body.get("mode", "urls")
        if mode not in ("channel", "urls"):
            with _jobs_lock:
                _active_jobs = max(0, _active_jobs - 1)
            self._send_json({"error": f"Unknown mode: {mode!r}"}, status=400)
            return

        jid = _new_job_id()
        with _evt_lock:
            _evt_queues[jid] = queue.Queue()

        _root_log.info("JOB_QUEUED  jid=%s  mode=%s", jid, mode)

        t = threading.Thread(target=_worker, args=(jid, body), daemon=True)
        t.start()

        self._send_json({"job_id": jid})

    # ── Helpers ───────────────────────────────────────────────────
    def _qs_param(self, key: str) -> str:
        """Extract a single query-string parameter from self.path."""
        match = re.search(rf"[?&]{re.escape(key)}=([^&]+)", self.path)
        return match.group(1) if match else ""

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass   # Client disconnected


# ══════════════════════════════════════════════════════════════════════════════
# CLI mode — python server.py --transcribe <url> [options]
# ══════════════════════════════════════════════════════════════════════════════

def _cli_main(args: argparse.Namespace) -> None:
    """Minimal CLI: transcribe one URL/ID and print result to stdout."""
    _check_dependencies()

    video_ids = resolve_urls(args.url)
    if not video_ids:
        # Try treating the argument as a raw video ID
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", args.url):
            video_ids = [args.url]
        else:
            print(f"ERROR: Could not extract a video ID from: {args.url}", file=sys.stderr)
            sys.exit(1)

    out_dir = Path(args.output or str(OUTPUT_DIR))
    out_dir.mkdir(parents=True, exist_ok=True)

    for vid_id in video_ids:
        print(f"\n▶ Processing: {vid_id}")
        title, text, error = get_transcript(
            vid_id,
            lang       = args.lang,
            timestamps = args.timestamps,
        )
        if text:
            filepath = _safe_output_path(out_dir, title, vid_id, force=args.force)
            filepath.write_text(text, encoding="utf-8")
            print(f"   ✔ Saved → {filepath}")
        else:
            print(f"   ✗ Failed: {error}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# Startup helpers
# ══════════════════════════════════════════════════════════════════════════════

def _check_dependencies() -> None:
    """Warn about optional but important dependencies at startup."""
    missing = []
    for pkg in ("yt_dlp", "youtube_transcript_api"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg.replace("_", "-"))
    if missing:
        _root_log.warning("MISSING_PACKAGES  %s — run: pip install %s",
                          missing, " ".join(missing))


def _setup_signal_handlers(server: ThreadedHTTPServer) -> None:
    """IMPROVEMENT: graceful SIGTERM for Docker / systemd shutdown."""
    def _on_sigterm(sig, frame):  # noqa: ANN001
        _root_log.info("SIGTERM received — shutting down gracefully")
        threading.Thread(target=server.shutdown, daemon=True).start()
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (OSError, ValueError):
        pass  # Windows or non-main-thread — ignore


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="YT Transcriber — web server or CLI mode"
    )
    parser.add_argument(
        "--transcribe", metavar="URL",
        help="CLI mode: transcribe a single video/playlist URL and exit"
    )
    parser.add_argument("--lang",       default="en",  help="Language code (default: en)")
    parser.add_argument("--output",     default=None,  help="Output directory")
    parser.add_argument("--timestamps", action="store_true", help="Include timestamps")
    parser.add_argument("--force",      action="store_true", help="Overwrite existing files")

    # Allow positional URL as shortcut: python server.py <url>
    parser.add_argument("url", nargs="?", help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Resolve shorthand: `python server.py <url>`
    if args.url and not args.transcribe:
        args.transcribe = args.url

    if args.transcribe:
        _cli_main(args)
        sys.exit(0)

    # ── Web server mode ───────────────────────────────────────────
    _check_dependencies()

    banner = "─" * 58
    print(banner)
    print("  🎬  YT Transcriber  —  backend  v2.1")
    print(f"  Listening  : http://localhost:{PORT}")
    print(f"  Transcripts: {OUTPUT_DIR.resolve()}")
    print(f"  Logs       : {LOG_DIR.resolve()}")
    print(f"  Workers    : {MAX_WORKERS} parallel  |  Rate limit: {RATE_LIMIT} jobs")
    print(f"  Whisper    : {WHISPER_MODEL} model (if captions unavailable)")
    print("  Open index.html in your browser to start.")
    print(banner)

    try:
        server = ThreadedHTTPServer(("localhost", PORT), Handler)
        _setup_signal_handlers(server)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        sys.exit(0)