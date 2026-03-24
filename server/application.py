#!/usr/bin/env python3
"""
main.py — VT2 Web UI (self-contained, no other files needed)
Run:
    pip install flask youtube-transcript-api yt-dlp
    python main.py
Open http://localhost:5050
"""
from __future__ import annotations
import random 
import json, os, queue, re, sys, threading, time, uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from flask import Flask, Response, jsonify, request, send_file
except ImportError:
    print("Run: pip install flask"); sys.exit(1)

PORT           = int(os.environ.get("YT_PORT", "8080"))
DEFAULT_OUTDIR = Path("transcripts")
DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)

# for AWS Elastic Beanstalk
# app = Flask(__name__)
application = Flask(__name__)

_VIDEO_RE      = re.compile(r"(?:v=|youtu\.be/|/embed/|/v/|/shorts/)([A-Za-z0-9_-]{11})")
_PLAYLIST_RE   = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_BARE_ID_RE    = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ILLEGAL_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')

# ── Transcript helpers ────────────────────────────────────────────────────────

def _sanitize(name, max_len=100):
    name = _ILLEGAL_CHARS.sub("", name)
    name = re.sub(r"\s+", "_", name.strip()).strip("._")
    return (name or "untitled")[:max_len]

def _unique_path(directory, video_id, force=False):
    fp = directory / (_sanitize(video_id) + ".txt")
    if force or not fp.exists():
        return fp
    return directory / (_sanitize(video_id) + "_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")

def _build_header(video_id, language):
    return (
        "Video ID : " + video_id + "\n"
        "URL      : https://www.youtube.com/watch?v=" + video_id + "\n"
        "Language : " + language + "\n"
        "Fetched  : " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
        + ("\u2500" * 60) + "\n\n"
    )

def _format_entries(entries, timestamps):
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
            lines.append("[{:02d}:{:02d}] {}".format(m, s, text))
        else:
            lines.append(text)
    return "\n".join(lines)

def _api_version():
    try:
        import importlib.metadata
        ver = importlib.metadata.version("youtube-transcript-api")
        m   = re.match(r"(\d+)\.(\d+)", ver)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:
        return (0, 0)

def _extract_video_id(raw):
    raw = raw.strip()
    m   = _VIDEO_RE.search(raw)
    if m: return m.group(1)
    return raw if _BARE_ID_RE.fullmatch(raw) else None

def _expand_playlist(playlist_id, push_fn):
    try:
        import yt_dlp
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info("https://www.youtube.com/playlist?list=" + playlist_id, download=False)
        return [e["id"] for e in (data.get("entries") or []) if e and e.get("id")]
    except ImportError:
        push_fn("yt-dlp not installed", "warn"); return []
    except Exception as exc:
        push_fn("Playlist expand failed: " + str(exc), "warn"); return []

def _resolve_inputs(lines, push_fn):
    seen, ids = set(), []
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"): continue
        pm = _PLAYLIST_RE.search(raw)
        if pm:
            inline = _VIDEO_RE.search(raw)
            if inline and inline.group(1) not in seen:
                v = inline.group(1); seen.add(v); ids.append(v)
            for v in _expand_playlist(pm.group(1), push_fn):
                if v not in seen: seen.add(v); ids.append(v)
            continue
        vid = _extract_video_id(raw)
        if vid:
            if vid not in seen: seen.add(vid); ids.append(vid)
        else:
            push_fn("Skipping: " + raw[:80], "warn")
    return ids

def _fetch_transcript(video_id, lang, timestamps, push_fn):
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
    except ImportError:
        return None, "youtube-transcript-api not installed"
    except Exception as exc:
        return None, str(exc)

    ver = _api_version()
    push_fn("  youtube-transcript-api v{}.{}".format(ver[0], ver[1]), "info")

    candidates = []
    if lang and lang not in ("auto", ""): candidates.append(lang)
    for fb in ("en", "ar", "fr", "es", "de", "ja", "zh", "pt", "ru", "ko"):
        if fb not in candidates: candidates.append(fb)

    if ver >= (1, 0):
        for candidate in candidates:
            try:
                push_fn("     Trying: " + candidate + "...", "info")
                fetched  = api.fetch(video_id, languages=[candidate])
                snippets = getattr(fetched, "snippets", fetched)
                text     = _format_entries(snippets, timestamps)
                if text:
                    push_fn("     \u2714 Found: " + candidate, "ok")
                    return _build_header(video_id, candidate) + text, None
            except Exception:
                pass
        try:
            push_fn("     Discovering languages...", "info")
            available = list(api.list(video_id))
            if not available: return None, "No transcripts available."
            def _rank(t):
                return (0 if t.language_code in candidates else 2) + (1 if t.is_generated else 0)
            chosen = min(available, key=_rank)
            push_fn("     \u2714 Using: {} [{}] ({})".format(
                chosen.language, chosen.language_code, "auto" if chosen.is_generated else "manual"), "ok")
            fetched  = chosen.fetch()
            snippets = getattr(fetched, "snippets", fetched)
            text     = _format_entries(snippets, timestamps)
            if text: return _build_header(video_id, chosen.language_code) + text, None
            return None, "Transcript was empty."
        except Exception as exc:
            return None, "No captions found: " + str(exc)
    else:
        try:
            tlist = api.list_transcripts(video_id)
            transcript = None
            for finder in (
                lambda: tlist.find_manually_created_transcript(candidates),
                lambda: tlist.find_generated_transcript(candidates),
            ):
                try: transcript = finder(); break
                except Exception: pass
            if transcript is None: transcript = next(iter(tlist), None)
            if transcript is None: return None, "No transcripts available."
            push_fn("     \u2714 {} ({})".format(transcript.language,
                "auto" if transcript.is_generated else "manual"), "ok")
            raw = transcript.fetch()
            used_lang = getattr(transcript, "language_code", getattr(transcript, "language", lang))
            text = _format_entries(raw, timestamps)
            return (_build_header(video_id, used_lang) + text, None) if text else (None, "Empty.")
        except Exception as exc:
            return None, "Transcript fetch failed: " + str(exc)

# ── Job registry ──────────────────────────────────────────────────────────────

_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()

def _new_job():
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {"q": queue.Queue(), "status": "running",
                      "ok": 0, "err": 0, "total": 0, "outdir": "", "files": {}}
    return jid

def _push(jid, event):
    with _jobs_lock:
        job = _jobs.get(jid)
    if job: job["q"].put(event)

def _log(jid, text, level="info"):
    _push(jid, {"type": "log", "level": level, "text": text})

def _inc(jid, key):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job: job[key] = job.get(key, 0) + 1

def _set_f(jid, key, val):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job: job[key] = val

def _get_f(jid, key, default=0):
    with _jobs_lock:
        return _jobs.get(jid, {}).get(key, default)

def _store_file(jid, vid, path):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job is not None:
            files = job.setdefault("files", {})
            files[vid] = str(path)

def _finish_job(jid, delay=3600):
    def _drop():
        time.sleep(delay)
        with _jobs_lock: _jobs.pop(jid, None)
    threading.Thread(target=_drop, daemon=True).start()

def _finalize(jid):
    ok, err, outdir = _get_f(jid,"ok"), _get_f(jid,"err"), _get_f(jid,"outdir","")
    _set_f(jid, "status", "done")
    _push(jid, {"type": "done", "ok": ok, "err": err, "outdir": outdir})
    _finish_job(jid)

# ── Transcription loop ────────────────────────────────────────────────────────

def _run_transcriptions(jid, video_ids, lang, timestamps, force, out_dir):
    total = len(video_ids)
    _set_f(jid, "total", total)
    _set_f(jid, "outdir", str(out_dir.resolve()))
    _push(jid, {"type": "start", "total": total, "text": "Starting {} video(s)...".format(total)})
    _log(jid, "\U0001f4dd Transcribing {} \u2192 {}".format(total, out_dir), "head")

    def push_fn(text, level="info"): _log(jid, text, level)

    for i, vid_id in enumerate(video_ids):
        url = "https://www.youtube.com/watch?v=" + vid_id
        _push(jid, {"type": "video_start", "index": i, "total": total, "id": vid_id, "url": url})
        _log(jid, "\n[{}/{}] {}".format(i+1, total, url), "head")
        t0 = time.time()
        try:
            text, error = _fetch_transcript(vid_id, lang, timestamps, push_fn)
            time.sleep(random.uniform(2, 6))  # random delay 2–6s
            elapsed = round(time.time() - t0, 1)
            if text:
                fp = _unique_path(out_dir, vid_id, force=force)
                fp.write_text(text, encoding="utf-8")
                size_kb = round(fp.stat().st_size / 1024, 1)
                _store_file(jid, vid_id, fp)
                dl_url = "/download/{}/{}".format(jid, vid_id)
                _push(jid, {"type": "ok", "id": vid_id, "index": i,
                            "path": str(fp), "size_kb": size_kb,
                            "elapsed": elapsed, "download": dl_url})
                _log(jid, "  \u2714 Saved \u2192 {}  ({} KB, {}s)".format(fp, size_kb, elapsed), "ok")
                _inc(jid, "ok")
            else:
                reason = error or "Unknown error"
                _push(jid, {"type": "err", "id": vid_id, "index": i, "reason": reason})
                _log(jid, "  \u2717 Failed: " + reason, "error")
                _inc(jid, "err")
        except Exception as exc:
            _push(jid, {"type": "err", "id": vid_id, "index": i, "reason": str(exc)})
            _log(jid, "  \u2717 Exception: " + str(exc), "error")
            _inc(jid, "err")
        done = _get_f(jid,"ok") + _get_f(jid,"err")
        _push(jid, {"type": "progress", "done": done, "total": total})

# ── Workers ───────────────────────────────────────────────────────────────────

def _worker_channel(jid, body):
    channel    = (body.get("channel") or "").strip()
    max_videos = int(body.get("max_videos") or 0)
    out_dir    = Path((body.get("output_dir") or "transcripts").strip())
    lang       = (body.get("lang") or "en").strip()
    timestamps = bool(body.get("timestamps", False))
    force      = bool(body.get("force", False))
    try:
        try:
            import yt_dlp
        except ImportError:
            _push(jid, {"type": "fatal", "text": "yt-dlp not installed. Run: pip install yt-dlp"}); return
        s = channel.strip()
        if not s.startswith(("http://", "https://")):
            s = "https://www.youtube.com/@" + s.lstrip("@")
        _log(jid, "\U0001f4fa Resolving: " + s, "head")
        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "no_warnings": True}
        ch_name, ids, seen = channel, [], set()
        for suffix in ("/videos", "/streams", "/shorts", ""):
            url = s.rstrip("/") + suffix
            _log(jid, "  Trying: " + url, "info")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    data = ydl.extract_info(url, download=False)
                if not data: continue
                ch_name = data.get("channel") or data.get("uploader") or data.get("title") or channel
                for entry in (data.get("entries") or []):
                    if not entry: continue
                    if entry.get("_type") == "playlist":
                        for sub in (entry.get("entries") or []):
                            if sub and sub.get("id") and sub["id"] not in seen:
                                seen.add(sub["id"]); ids.append(sub["id"])
                    elif entry.get("id") and entry["id"] not in seen:
                        seen.add(entry["id"]); ids.append(entry["id"])
                if ids:
                    _log(jid, "  \u2714 Found {} videos: {}".format(len(ids), ch_name), "ok"); break
            except Exception as exc:
                _log(jid, "  \u2717 {}: {}".format(suffix or "bare", exc), "warn"); continue
        if not ids:
            _push(jid, {"type": "fatal", "text": "No videos found for: " + repr(channel)}); return
        if max_videos and len(ids) > max_videos:
            ids = ids[:max_videos]
            _log(jid, "  Trimmed to {}".format(max_videos), "info")
        out_dir.mkdir(parents=True, exist_ok=True)
        lines  = ["https://www.youtube.com/watch?v=" + v for v in ids]
        header = "# Channel : {}\n# Videos  : {}\n# Created : {}\n\n".format(
            ch_name, len(lines), time.strftime("%Y-%m-%d %H:%M:%S"))
        (out_dir / "urls.txt").write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
        _log(jid, "  \U0001f4c4 urls.txt written ({} URLs)".format(len(lines)), "ok")
        _run_transcriptions(jid, ids, lang, timestamps, force, out_dir)
    except Exception as exc:
        _push(jid, {"type": "fatal", "text": str(exc)})
    finally:
        _finalize(jid)

def _worker_urls(jid, body):
    raw_urls   = (body.get("urls") or "").strip()
    lang       = (body.get("lang") or "en").strip()
    timestamps = bool(body.get("timestamps", False))
    force      = bool(body.get("force", False))
    out_dir    = Path((body.get("output_dir") or "transcripts").strip())
    try:
        if not raw_urls:
            _push(jid, {"type": "fatal", "text": "No URLs provided."}); return
        def push_fn(text, level="info"): _log(jid, text, level)
        video_ids = _resolve_inputs(raw_urls.splitlines(), push_fn)
        if not video_ids:
            _push(jid, {"type": "fatal", "text": "No valid YouTube video IDs found."}); return
        out_dir.mkdir(parents=True, exist_ok=True)
        _run_transcriptions(jid, video_ids, lang, timestamps, force, out_dir)
    except Exception as exc:
        _push(jid, {"type": "fatal", "text": str(exc)})
    finally:
        _finalize(jid)

# ── Routes ────────────────────────────────────────────────────────────────────

@application.route("/")
def index():
    return HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@application.route("/ping")
def ping():
    ver = _api_version()
    yt_dlp_ok, transcript_ok = False, False
    try: import yt_dlp; yt_dlp_ok = True
    except ImportError: pass
    try: from youtube_transcript_api import YouTubeTranscriptApi; transcript_ok = True
    except ImportError: pass
    active = sum(1 for j in _jobs.values() if j.get("status") == "running")
    return jsonify({
        "status": "ok" if (transcript_ok and yt_dlp_ok) else "degraded",
        "yt_dlp": yt_dlp_ok, "transcript_ok": transcript_ok,
        "transcript_api": "{}.{}".format(ver[0], ver[1]),
        "active_jobs": active, "output_dir": str(DEFAULT_OUTDIR.resolve()),
    })

@application.route("/start", methods=["POST"])
def start_job():
    body = request.get_json(force=True, silent=True)
    if not body: return jsonify({"error": "Invalid JSON"}), 400
    mode = body.get("mode", "urls")
    if mode not in ("channel", "urls"): return jsonify({"error": "bad mode"}), 400
    jid    = _new_job()
    target = _worker_channel if mode == "channel" else _worker_urls
    threading.Thread(target=target, args=(jid, body), daemon=True).start()
    return jsonify({"job_id": jid})

@application.route("/events/<jid>")
def events(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job: return Response("Job not found", status=404, mimetype="text/plain")
    def generate():
        q = job["q"]
        while True:
            try:
                event = q.get(timeout=20)
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"; continue
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            if event.get("type") == "done": break
    return Response(generate(), mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@application.route("/jobs")
def list_jobs():
    with _jobs_lock:
        result = {jid: {"status": j.get("status"), "ok": j.get("ok",0),
                        "err": j.get("err",0), "total": j.get("total",0),
                        "outdir": j.get("outdir","")} for jid, j in _jobs.items()}
    return jsonify(result)

@application.route("/download/<jid>/<vid>")
def download_file(jid, vid):
    with _jobs_lock:
        job   = _jobs.get(jid)
        files = job.get("files") if job else None
    if not job:
        return Response("Job not found", status=404, mimetype="text/plain")
    path = (files or {}).get(vid)
    if not path:
        return Response("File not available for this video", status=404, mimetype="text/plain")
    fp = Path(path).resolve()
    outdir = Path(job.get("outdir", "") or DEFAULT_OUTDIR).resolve()
    try:
        fp.relative_to(outdir)
    except Exception:
        return Response("Forbidden", status=403, mimetype="text/plain")
    if not fp.exists():
        return Response("File missing", status=404, mimetype="text/plain")
    return send_file(str(fp), as_attachment=True, download_name=fp.name, mimetype="text/plain")


# ── Inline HTML ───────────────────────────────────────────────────────────────

# from index.html
HTML = open(Path(__file__).parent / "index.html", encoding="utf-8").read()


# ── Entry point ───────────────────────────────────────────────────────────────

# ── HTML ──────────────────────────────────────────────────────────────────────
# (paste the full _HTML string from my previous message here)
# It starts with: _HTML = "<!DOCTYPE html>..."

if __name__ == "__main__":
    print("  VT2  |  http://localhost:" + str(PORT))
    print("  pip install flask youtube-transcript-api yt-dlp")
    # application.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    # for AWS Elastic Beanstalk
    application.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
