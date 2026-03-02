"""
server.py — YT Transcriber backend
Run with:  python server.py
Then open: index.html in your browser
"""

import json
import os
import re
import sys
import time
import queue
import threading
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import List, Tuple, Optional

PORT = 5050

# ══════════════════════════════════════════════════════════════════════════════
# Global event queue  (worker thread → HTTP handler via SSE)
# ══════════════════════════════════════════════════════════════════════════════
_job_queue: queue.Queue = queue.Queue()   # pending jobs  (body dicts)
_evt_queues: dict       = {}             # job_id → Queue of SSE strings
_evt_lock               = threading.Lock()


def _new_job_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _push(jid: str, data: dict):
    """Push an event dict to the SSE queue for a given job."""
    with _evt_lock:
        q = _evt_queues.get(jid)
    if q:
        q.put(data)


# ══════════════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:100]


def _make_header(title: str, video_id: str, language: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return (
        f"Title    : {title}\n"
        f"Video ID : {video_id}\n"
        f"URL      : {url}\n"
        f"Language : {language}\n"
        + "─" * 60 + "\n\n"
    )


def _format_entries(entries: list, timestamps: bool) -> str:
    lines = []
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        if timestamps:
            start = e.get("start", 0)
            m, s = divmod(int(start), 60)
            lines.append(f"[{m:02d}:{s:02d}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


# ── yt-dlp logger that forwards messages to SSE ───────────────────────────────

class YTLogger:
    def __init__(self, jid: str, prefix: str = ""):
        self.jid    = jid
        self.prefix = prefix

    def debug(self, msg):
        msg = msg.strip()
        if not msg or msg.startswith("[debug]"):
            return
        # Show download/conversion progress lines
        if any(k in msg for k in ("[download]", "[ffmpeg]", "[ExtractAudio]",
                                   "Destination", "Merging", "Deleting",
                                   "%", "ETA", "at")):
            _push(self.jid, {"type": "detail", "text": (self.prefix + msg).strip()})

    def warning(self, msg):
        _push(self.jid, {"type": "detail", "text": f"⚠ {msg.strip()}"})

    def error(self, msg):
        msg = msg.strip()
        # Suppress ffmpeg/ffprobe not-found noise — not useful to user
        if any(k in msg.lower() for k in ("ffprobe", "ffmpeg", "postprocess")):
            return
        _push(self.jid, {"type": "detail", "text": f"✗ {msg}"})


# ══════════════════════════════════════════════════════════════════════════════
# Core: get video title
# ══════════════════════════════════════════════════════════════════════════════

def get_video_title(video_id: str, jid: str = "") -> str:
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "no_color": True,
            "extract_flat": False,
            # Prevent ANY postprocessor / ffmpeg check at title-fetch time
            "postprocessors": [],
            "check_formats": False,
            "logger": YTLogger(jid, ""),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            return info.get("title") or video_id
    except Exception:
        return video_id


# ══════════════════════════════════════════════════════════════════════════════
# Core: get transcript
# ══════════════════════════════════════════════════════════════════════════════

def get_transcript(
    video_id: str,
    lang: str = "ar",
    timestamps: bool = False,
    jid: str = "",
) -> Tuple[str, Optional[str], Optional[str]]:
    """Returns (title, text, error). text is None on failure."""

    _push(jid, {"type": "detail", "text": f"🔍 Fetching title for {video_id}…"})
    title = get_video_title(video_id, jid)
    _push(jid, {"type": "detail", "text": f"📹 Title: {title}"})

    # ── 1. youtube-transcript-api ─────────────────────────────────────────────
    _push(jid, {"type": "detail", "text": "📝 Looking for captions on YouTube…"})
    try:
        import importlib.metadata as _meta
        from youtube_transcript_api import YouTubeTranscriptApi

        try:
            _ver = tuple(int(x) for x in _meta.version("youtube-transcript-api").split(".")[:2])
        except Exception:
            _ver = (0, 0)

        _push(jid, {"type": "detail", "text": f"   youtube-transcript-api v{'.'.join(map(str,_ver))} detected"})

        candidates = []
        if lang and lang != "auto":
            candidates.append(lang)
        for fb in ("ar", "en", "fr"):
            if fb not in candidates:
                candidates.append(fb)

        text      = None
        used_lang = lang

        if _ver >= (0, 7):
            api     = YouTubeTranscriptApi()
            fetched = None
            for candidate in candidates:
                try:
                    _push(jid, {"type": "detail", "text": f"   Trying language: {candidate}…"})
                    fetched   = api.fetch(video_id, languages=[candidate])
                    used_lang = candidate
                    _push(jid, {"type": "detail", "text": f"   ✔ Found captions in: {candidate}"})
                    break
                except Exception as e:
                    _push(jid, {"type": "detail", "text": f"   ✗ {candidate}: {e}"})

            if fetched is None:
                try:
                    _push(jid, {"type": "detail", "text": "   Trying any available language…"})
                    fetched   = api.fetch(video_id)
                    used_lang = "auto"
                    _push(jid, {"type": "detail", "text": "   ✔ Found captions (auto language)"})
                except Exception as e:
                    _push(jid, {"type": "detail", "text": f"   ✗ No captions found: {e}"})

            if fetched is not None:
                entries = []
                for item in fetched:
                    if hasattr(item, "text"):
                        entries.append({"text": item.text, "start": getattr(item, "start", 0)})
                    elif isinstance(item, dict):
                        entries.append(item)
                text = _format_entries(entries, timestamps)

        else:
            # Old API
            try:
                tlist      = YouTubeTranscriptApi.list_transcripts(video_id)
                transcript = None
                try:
                    transcript = tlist.find_manually_created_transcript(candidates)
                    _push(jid, {"type": "detail", "text": f"   ✔ Found manual captions"})
                except Exception:
                    pass
                if transcript is None:
                    try:
                        transcript = tlist.find_generated_transcript(candidates)
                        _push(jid, {"type": "detail", "text": f"   ✔ Found auto-generated captions"})
                    except Exception:
                        pass
                if transcript is None:
                    for t in tlist:
                        transcript = t
                        _push(jid, {"type": "detail", "text": f"   ✔ Found captions: {t.language}"})
                        break
                if transcript:
                    raw     = transcript.fetch()
                    entries = []
                    for item in raw:
                        if hasattr(item, "text"):
                            entries.append({"text": item.text, "start": getattr(item, "start", 0)})
                        elif isinstance(item, dict):
                            entries.append(item)
                    used_lang = getattr(transcript, "language", lang)
                    text      = _format_entries(entries, timestamps)
            except Exception as e:
                _push(jid, {"type": "detail", "text": f"   ✗ list_transcripts failed: {e}"})

        if text:
            _push(jid, {"type": "detail", "text": f"   ✔ Captions extracted ({len(text)} chars)"})
            return title, _make_header(title, video_id, used_lang) + text, None

        _push(jid, {"type": "detail", "text": "   No captions available — falling back to Whisper…"})

    except ImportError:
        _push(jid, {"type": "detail", "text": "   youtube-transcript-api not installed — using Whisper"})
    except Exception as e:
        s = str(e).lower()
        if not any(k in s for k in ("disabled", "unavailable", "no transcript", "could not retrieve", "not found")):
            return title, None, f"Transcript API: {e}"
        _push(jid, {"type": "detail", "text": f"   Captions unavailable — falling back to Whisper"})

    # ── 2. Whisper fallback ───────────────────────────────────────────────────
    _push(jid, {"type": "detail", "text": "🎙 Starting Whisper transcription…"})
    try:
        import whisper
        import yt_dlp

        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = os.path.join(tmp, f"{video_id}.%(ext)s")
            # Prefer m4a — Whisper reads it natively, no ffmpeg needed
            # Fall back to any audio if m4a unavailable
            def _prog_hook(d):
                if d.get("status") == "downloading":
                    _push(jid, {"type": "detail", "text": (
                        f"   ⬇ {d.get('_percent_str','').strip()} "
                        f"of {d.get('_total_bytes_str','?')} "
                        f"at {d.get('_speed_str','?')} "
                        f"ETA {d.get('_eta_str','?')}"
                    ).strip()})
                elif d.get("status") == "finished":
                    _push(jid, {"type": "detail", "text": f"   ✔ Download complete: {d.get('filename','')}"})

            opts = {
                "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
                "outtmpl": out_tmpl,
                "quiet": True,
                "no_warnings": True,
                "no_color": True,
                # NO postprocessors → NO ffmpeg needed
                "postprocessors": [],
                "logger": YTLogger(jid, ""),
                "progress_hooks": [_prog_hook],
            }

            _push(jid, {"type": "detail", "text": f"   ⬇ Downloading audio from YouTube…"})
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            audio_exts = (".m4a", ".webm", ".mp4", ".ogg", ".opus", ".mp3", ".wav")
            audio_files = [f for f in os.listdir(tmp) if f.lower().endswith(audio_exts)]
            if not audio_files:
                return title, None, (
                    "Audio download failed. "
                    "If you need Whisper fallback, install ffmpeg: "
                    "https://ffmpeg.org/download.html"
                )

            mp3_path = os.path.join(tmp, audio_files[0])
            size_mb  = os.path.getsize(mp3_path) / 1024 / 1024
            ext      = os.path.splitext(audio_files[0])[1]
            _push(jid, {"type": "detail", "text": f"   ✔ Audio downloaded ({size_mb:.1f} MB, {ext})"})
            _push(jid, {"type": "detail", "text": "   🧠 Loading Whisper model (base)…"})

            wlang  = lang if lang and lang != "auto" else None
            model  = whisper.load_model("base")
            _push(jid, {"type": "detail", "text": f"   🧠 Transcribing with Whisper (lang={wlang or 'auto'})… this may take a while"})
            result = model.transcribe(mp3_path, language=wlang, verbose=False)

            segs = result.get("segments", [])
            if timestamps and segs:
                lines = []
                for seg in segs:
                    m, s = divmod(int(seg["start"]), 60)
                    lines.append(f"[{m:02d}:{s:02d}] {seg['text'].strip()}")
                text = "\n".join(lines)
            else:
                text = (result.get("text") or "").strip()

            if text:
                _push(jid, {"type": "detail", "text": f"   ✔ Whisper done ({len(text)} chars)"})
                return title, _make_header(title, video_id, f"whisper:{lang}") + text, None
            return title, None, "Whisper returned empty text."

    except ImportError:
        return title, None, (
            "No captions & Whisper not installed. "
            "Run: pip install openai-whisper  (also needs ffmpeg)"
        )
    except Exception as e:
        return title, None, f"Whisper error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# Channel / URL resolution
# ══════════════════════════════════════════════════════════════════════════════

_VIDEO_RE    = re.compile(r"(?:v=|youtu\.be/|/embed/|/v/|/shorts/)([A-Za-z0-9_-]{11})")
_PLAYLIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def resolve_channel(name_or_url: str, jid: str = "") -> Tuple[str, List[str], Optional[str]]:
    try:
        import yt_dlp
    except ImportError:
        return name_or_url, [], "yt-dlp not installed. Run: pip install yt-dlp"

    s = name_or_url.strip()
    if not s.startswith(("http://", "https://")):
        s = f"https://www.youtube.com/@{s.lstrip('@')}"

    _push(jid, {"type": "detail", "text": f"🔍 Looking up channel: {s}"})
    opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}

    for suffix in ("/videos", ""):
        try:
            url = s.rstrip("/") + suffix
            _push(jid, {"type": "detail", "text": f"   Trying: {url}"})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                continue

            ch_name = (
                info.get("channel") or info.get("uploader") or
                info.get("title") or name_or_url
            )
            entries = info.get("entries") or []
            ids: List[str] = []
            seen: set = set()

            for entry in entries:
                if not entry:
                    continue
                if entry.get("_type") == "playlist":
                    for sub in (entry.get("entries") or []):
                        if sub and sub.get("id") and sub["id"] not in seen:
                            seen.add(sub["id"]); ids.append(sub["id"])
                elif entry.get("id") and entry["id"] not in seen:
                    seen.add(entry["id"]); ids.append(entry["id"])

            if ids:
                _push(jid, {"type": "detail", "text": f"   ✔ Found {len(ids)} videos on channel: {ch_name}"})
                return ch_name, ids, None
        except Exception as e:
            _push(jid, {"type": "detail", "text": f"   ✗ {e}"})
            continue

    return name_or_url, [], "No videos found. Check the channel name / URL."


def resolve_urls(raw: str) -> List[str]:
    seen: set = set()
    ids: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        pm = _PLAYLIST_RE.search(line)
        if pm:
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


def _fetch_playlist(playlist_id: str) -> List[str]:
    try:
        import yt_dlp
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}", download=False
            )
        return [e["id"] for e in (info.get("entries") or []) if e and e.get("id")]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Worker thread — runs the actual transcription job
# ══════════════════════════════════════════════════════════════════════════════

def _worker(jid: str, body: dict):
    try:
        mode       = body.get("mode", "urls")
        lang       = body.get("lang", "ar")
        timestamps = bool(body.get("timestamps", False))
        output_dir = body.get("output_dir", r"C:\Users\moulo\Documents\Hobbies\videoToText")

        if mode == "channel":
            channel_input = body.get("channel", "")
            max_v = int(body.get("max_videos") or 0)
            ch_name, video_ids, ch_err = resolve_channel(channel_input, jid)
            if ch_err:
                _push(jid, {"type": "fatal", "text": ch_err})
                _push(jid, {"type": "done"})
                return
            if max_v > 0:
                video_ids = video_ids[:max_v]
            out_path = Path(output_dir) / sanitize_filename(ch_name)
            _push(jid, {"type": "start", "text": f"Channel: {ch_name}", "total": len(video_ids)})
        else:
            video_ids = resolve_urls(body.get("urls", ""))
            if not video_ids:
                _push(jid, {"type": "fatal", "text": "No valid YouTube video IDs found."})
                _push(jid, {"type": "done"})
                return
            out_path = Path(output_dir)
            _push(jid, {"type": "start", "text": "Processing URLs", "total": len(video_ids)})

        out_path.mkdir(parents=True, exist_ok=True)
        _push(jid, {"type": "detail", "text": f"📁 Output folder: {out_path}"})

        ok_count  = 0
        err_count = 0

        for i, vid_id in enumerate(video_ids):
            _push(jid, {"type": "video_start", "index": i, "total": len(video_ids), "id": vid_id})

            title, text, error = get_transcript(vid_id, lang, timestamps, jid)

            if text:
                filename = sanitize_filename(title or vid_id) + ".txt"
                filepath = out_path / filename
                filepath.write_text(text, encoding="utf-8")
                ok_count += 1
                _push(jid, {"type": "ok", "title": title or vid_id, "path": str(filepath), "index": i})
            else:
                err_count += 1
                _push(jid, {"type": "err", "title": title or vid_id, "reason": error or "Unknown", "index": i})

        _push(jid, {
            "type": "done",
            "ok": ok_count,
            "err": err_count,
            "output_dir": str(out_path),
        })

    except Exception as e:
        _push(jid, {"type": "fatal", "text": str(e)})
        _push(jid, {"type": "done"})
    finally:
        # Clean up queue after a delay
        def cleanup():
            time.sleep(30)
            with _evt_lock:
                _evt_queues.pop(jid, None)
        threading.Thread(target=cleanup, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP handler
# ══════════════════════════════════════════════════════════════════════════════

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread — required for SSE + other requests to coexist."""
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        # Health check
        if self.path == "/ping":
            self._send_json({"status": "ok"})
            return

        # SSE event stream:  GET /events?job=<jid>
        if self.path.startswith("/events"):
            jid = self.path.split("job=")[-1] if "job=" in self.path else ""
            with _evt_lock:
                q = _evt_queues.get(jid)
            if not q:
                self.send_response(404)
                self.end_headers()
                return

            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            try:
                while True:
                    try:
                        event = q.get(timeout=25)
                    except queue.Empty:
                        # Send keep-alive comment
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue

                    data = json.dumps(event)
                    msg  = f"data: {data}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()

                    if event.get("type") == "done":
                        break

            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            except Exception as e:
                print(f"  SSE error: {e}")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/transcribe":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))

        # Create a job + queue
        jid = _new_job_id()
        with _evt_lock:
            _evt_queues[jid] = queue.Queue()

        # Start worker thread
        t = threading.Thread(target=_worker, args=(jid, body), daemon=True)
        t.start()

        # Return the job ID immediately
        self._send_json({"job_id": jid})

    def _send_json(self, obj: dict):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("─" * 55)
    print("  🎬  YT Transcriber — backend server")
    print(f"  Listening on http://localhost:{PORT}")
    print("  Open index.html in your browser to start.")
    print("─" * 55)

    try:
        server = ThreadedHTTPServer(("localhost", PORT), Handler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        sys.exit(0)