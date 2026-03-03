#!/usr/bin/env python3
"""
main.py — VT2 Web UI (self-contained, no other files needed)
Run:
    pip install flask youtube-transcript-api yt-dlp
    python main.py
Open http://localhost:5050
"""
from __future__ import annotations
import json, os, queue, re, sys, threading, time, uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from flask import Flask, Response, jsonify, request
except ImportError:
    print("Run: pip install flask"); sys.exit(1)

PORT           = int(os.environ.get("YT_PORT", "5050"))
DEFAULT_OUTDIR = Path("transcripts")
DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)

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
                      "ok": 0, "err": 0, "total": 0, "outdir": ""}
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

def _finish_job(jid, delay=120):
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
            elapsed = round(time.time() - t0, 1)
            if text:
                fp = _unique_path(out_dir, vid_id, force=force)
                fp.write_text(text, encoding="utf-8")
                size_kb = round(fp.stat().st_size / 1024, 1)
                _push(jid, {"type": "ok", "id": vid_id, "index": i,
                            "path": str(fp), "size_kb": size_kb, "elapsed": elapsed})
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

@app.route("/")
def index():
    return HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/ping")
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

@app.route("/start", methods=["POST"])
def start_job():
    body = request.get_json(force=True, silent=True)
    if not body: return jsonify({"error": "Invalid JSON"}), 400
    mode = body.get("mode", "urls")
    if mode not in ("channel", "urls"): return jsonify({"error": "bad mode"}), 400
    jid    = _new_job()
    target = _worker_channel if mode == "channel" else _worker_urls
    threading.Thread(target=target, args=(jid, body), daemon=True).start()
    return jsonify({"job_id": jid})

@app.route("/events/<jid>")
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

@app.route("/jobs")
def list_jobs():
    with _jobs_lock:
        result = {jid: {"status": j.get("status"), "ok": j.get("ok",0),
                        "err": j.get("err",0), "total": j.get("total",0),
                        "outdir": j.get("outdir","")} for jid, j in _jobs.items()}
    return jsonify(result)


# ── Inline HTML ───────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>VT2</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>
<style>
:root {
  --bg:      #0a0a0f;
  --surf:    #111118;
  --surf2:   #1a1a24;
  --border:  #252535;
  --red:     #ff4455;
  --red-dim: rgba(255,68,85,.15);
  --green:   #00e5a0;
  --blue:    #4d9fff;
  --yellow:  #ffcc44;
  --purple:  #bb86fc;
  --text:    #e0e0f0;
  --muted:   #55556a;
  --mono:    'IBM Plex Mono', monospace;
  --sans:    'IBM Plex Sans', sans-serif;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding-bottom:80px}

/* header */
header{
  background:var(--surf);border-bottom:1px solid var(--border);
  padding:14px 32px;display:flex;align-items:center;gap:12px;
  position:sticky;top:0;z-index:100;backdrop-filter:blur(12px);
}
.logo{font-family:var(--mono);font-weight:700;font-size:16px;color:var(--red);letter-spacing:-.5px}
.logo span{color:var(--text)}
.badge{font-family:var(--mono);font-size:9px;background:var(--red-dim);color:var(--red);border:1px solid var(--red);border-radius:3px;padding:2px 6px;letter-spacing:1px}
.srv-bar{margin-left:auto;display:flex;align-items:center;gap:18px}
.srv-item{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:all .4s}
.dot.ok{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.dot.warn{background:var(--yellow);box-shadow:0 0 8px var(--yellow)}

/* layout */
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 0}

/* tabs */
.tabs{display:flex;gap:3px;background:var(--surf);border:1px solid var(--border);border-radius:10px;padding:4px;margin-bottom:22px}
.tab{flex:1;padding:9px 6px;border:none;background:transparent;color:var(--muted);font-family:var(--mono);font-size:12px;border-radius:7px;cursor:pointer;transition:all .2s;white-space:nowrap}
.tab:hover:not(.active){color:var(--text);background:var(--surf2)}
.tab.active{background:var(--red);color:#fff}
.panel{display:none}
.panel.active{display:block}

/* cards */
.card{background:var(--surf);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
.card-title{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:14px}

/* inputs */
label{display:block;font-size:11px;color:var(--muted);margin-bottom:5px;font-family:var(--mono)}
input[type=text],input[type=number],textarea,select{
  width:100%;background:var(--surf2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);font-family:var(--mono);font-size:12px;
  padding:9px 12px;outline:none;resize:vertical;transition:border-color .2s,box-shadow .2s;
}
input:focus,textarea:focus,select:focus{border-color:var(--red);box-shadow:0 0 0 3px var(--red-dim)}
textarea{min-height:100px;line-height:1.6}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.check-row{display:flex;align-items:center;gap:9px;margin-top:8px}
input[type=checkbox]{width:14px;height:14px;accent-color:var(--red);cursor:pointer;flex-shrink:0}
.note{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.6;font-family:var(--mono)}
.note b{color:var(--yellow)}

/* buttons */
.btn{
  display:inline-flex;align-items:center;gap:7px;
  background:var(--red);color:#fff;border:none;border-radius:8px;
  font-family:var(--mono);font-weight:700;font-size:12px;
  padding:11px 22px;cursor:pointer;margin-top:14px;transition:all .2s;
}
.btn:hover:not(:disabled){background:#ff2233;transform:translateY(-1px);box-shadow:0 6px 20px rgba(255,68,85,.4)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important}
.btn-ghost{
  background:none;border:1px solid var(--border);color:var(--muted);
  font-family:var(--mono);font-size:10px;border-radius:5px;
  padding:3px 8px;cursor:pointer;transition:all .2s;
}
.btn-ghost:hover{color:var(--text);border-color:var(--muted)}

/* progress */
#prog-wrap{display:none;margin-top:22px}
.prog-track{background:var(--surf2);border-radius:99px;height:4px;overflow:hidden;margin-bottom:7px}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--red),var(--yellow));border-radius:99px;width:0%;transition:width .4s ease}
.prog-lbl{font-family:var(--mono);font-size:11px;color:var(--muted)}

/* output grid */
#output-grid{display:none;grid-template-columns:1fr 1fr;gap:14px;margin-top:22px}
@media(max-width:680px){#output-grid{grid-template-columns:1fr}}
.pbox{background:var(--surf);border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.pbox-head{
  font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:1.5px;
  text-transform:uppercase;padding:9px 13px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px;flex-shrink:0;
}
.pbox-body{padding:8px;flex:1;overflow-y:auto;max-height:440px}
.pbox-body::-webkit-scrollbar{width:3px}
.pbox-body::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px}

/* result items */
.res{border-radius:7px;border:1px solid var(--border);padding:9px 11px;margin-bottom:7px;animation:fadeUp .2s ease}
.res.ok {border-left:3px solid var(--green)}
.res.err{border-left:3px solid var(--red)}
.res.act{border-left:3px solid var(--blue);background:rgba(77,159,255,.04)}
.res-title{font-family:var(--mono);font-size:11px;color:var(--text);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.res-sub{font-family:var(--mono);font-size:10px;color:var(--muted);word-break:break-all}
.c-ok{color:var(--green)} .c-err{color:var(--red)} .c-act{color:var(--blue)} .c-warn{color:var(--yellow)} .c-purple{color:var(--purple)}

/* log lines */
.ll{font-family:var(--mono);font-size:10.5px;padding:2px 0;line-height:1.65;border-bottom:1px solid rgba(255,255,255,.025);white-space:pre-wrap;word-break:break-all;animation:fadeUp .12s ease}
.ll.ok{color:#00c87a} .ll.error{color:var(--red)} .ll.warn{color:var(--yellow)}
.ll.head{color:var(--purple);font-weight:600;padding-top:6px}
.ll.info{color:#8888aa} .ll.dl{color:var(--blue)}

/* summary */
#summary{display:none;margin-top:14px;background:var(--surf2);border:1px solid var(--border);border-radius:10px;padding:13px 16px;font-family:var(--mono);font-size:12px;gap:18px;align-items:center;flex-wrap:wrap}
.s-dir{color:var(--muted);font-size:10px;word-break:break-all;margin-top:3px}

/* server status panel */
.status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-bottom:16px}
.stat-card{background:var(--surf2);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-label{font-family:var(--mono);font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.stat-val{font-family:var(--mono);font-size:18px;font-weight:700;color:var(--text)}
.stat-val.ok{color:var(--green)} .stat-val.err{color:var(--red)} .stat-val.warn{color:var(--yellow)}

/* toast */
#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(80px);
  background:#200a0e;border:1px solid var(--red);color:var(--red);
  font-family:var(--mono);font-size:12px;padding:11px 20px;border-radius:9px;
  z-index:999;transition:transform .3s ease;pointer-events:none;max-width:90vw;text-align:center}
#toast.show{transform:translateX(-50%) translateY(0)}

@keyframes fadeUp{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.spinner{width:11px;height:11px;border:2px solid rgba(255,255,255,.2);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;display:inline-block;flex-shrink:0}
.blink{animation:pulse 1.4s ease infinite}
</style>
</head>
<body>

<header>
  <div class="logo">VT<span>2</span></div>
  <div class="badge">WEB</div>
  <div class="srv-bar">
    <div class="srv-item"><div class="dot" id="dot-server"></div><span id="lbl-server">checking…</span></div>
    <div class="srv-item"><div class="dot" id="dot-ytdlp"></div><span id="lbl-ytdlp">yt-dlp</span></div>
    <div class="srv-item"><div class="dot" id="dot-api"></div><span id="lbl-api">transcript-api</span></div>
    <div class="srv-item" style="margin-left:8px"><span id="lbl-jobs" style="color:var(--muted);font-family:var(--mono);font-size:11px">0 jobs</span></div>
  </div>
</header>

<div class="wrap">

  <div class="tabs" role="tablist">
    <button class="tab active" onclick="switchTab('channel')">📺 Channel</button>
    <button class="tab"        onclick="switchTab('urls')">🔗 URLs / Playlists</button>
    <button class="tab"        onclick="switchTab('settings')">⚙ Settings</button>
    <button class="tab"        onclick="switchTab('monitor')">📊 Monitor</button>
  </div>

  <!-- Channel tab -->
  <div class="panel active" id="tab-channel">
    <div class="card">
      <div class="card-title">YouTube Channel</div>
      <label for="ch-input">Channel handle or URL</label>
      <input type="text" id="ch-input" placeholder="@MrBeast  or  https://www.youtube.com/@NASA" autocomplete="off" spellcheck="false"/>
      <div class="row2" style="margin-top:14px">
        <div>
          <label for="ch-max">Max videos <small style="color:var(--muted)">(0 = all)</small></label>
          <input type="number" id="ch-max" value="0" min="0" step="10"/>
        </div>
        <div>
          <label for="ch-dir">Output directory</label>
          <input type="text" id="ch-dir" value="transcripts" spellcheck="false"/>
        </div>
      </div>
      <p class="note"><b>Tip:</b> The channel's video list is saved as urls.txt in the output directory before transcribing starts.</p>
      <button class="btn" id="btn-ch" onclick="startChannel()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg>
        Transcribe Channel
      </button>
    </div>
  </div>

  <!-- URLs tab -->
  <div class="panel" id="tab-urls">
    <div class="card">
      <div class="card-title">Videos &amp; Playlists</div>
      <label for="url-input">YouTube URLs (one per line)</label>
      <textarea id="url-input" placeholder="https://www.youtube.com/watch?v=XXXXXXXXXXX&#10;https://youtu.be/XXXXXXXXXXX&#10;https://www.youtube.com/playlist?list=PLxxxxxxxxx" spellcheck="false"></textarea>
      <button class="btn" id="btn-url" onclick="startUrls()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg>
        Transcribe URLs
      </button>
    </div>
  </div>

  <!-- Settings tab -->
  <div class="panel" id="tab-settings">
    <div class="card">
      <div class="card-title">Language</div>
      <div class="row2">
        <div>
          <label for="s-lang">Language code</label>
          <input type="text" id="s-lang" value="en" style="max-width:120px" placeholder="en"/>
          <p class="note"><b>en</b> · ar · fr · es · de · ja · zh · ru · <b>auto</b></p>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Format</div>
      <div class="check-row">
        <input type="checkbox" id="s-ts"/>
        <label style="margin:0;cursor:pointer" for="s-ts">Include [MM:SS] timestamps</label>
      </div>
      <div class="check-row">
        <input type="checkbox" id="s-force"/>
        <label style="margin:0;cursor:pointer" for="s-force">Overwrite existing files</label>
      </div>
    </div>
  </div>

  <!-- Monitor tab -->
  <div class="panel" id="tab-monitor">
    <div class="status-grid" id="status-grid">
      <div class="stat-card"><div class="stat-label">Server</div><div class="stat-val" id="m-server">—</div></div>
      <div class="stat-card"><div class="stat-label">yt-dlp</div><div class="stat-val" id="m-ytdlp">—</div></div>
      <div class="stat-card"><div class="stat-label">transcript-api</div><div class="stat-val" id="m-api">—</div></div>
      <div class="stat-card"><div class="stat-label">Active Jobs</div><div class="stat-val" id="m-jobs">—</div></div>
      <div class="stat-card"><div class="stat-label">Output Dir</div><div class="stat-val" style="font-size:11px;word-break:break-all" id="m-outdir">—</div></div>
    </div>
    <button class="btn-ghost" onclick="refreshStatus()" style="margin-bottom:12px">↻ Refresh</button>
    <div class="card" style="padding:0;overflow:hidden">
      <div class="pbox-head">📋 Active Jobs</div>
      <div class="pbox-body" id="jobs-list" style="max-height:300px">
        <div style="padding:16px;color:var(--muted);font-family:var(--mono);font-size:11px">No jobs yet.</div>
      </div>
    </div>
  </div>

  <!-- Progress -->
  <div id="prog-wrap">
    <div class="prog-track"><div class="prog-fill" id="prog-fill"></div></div>
    <div class="prog-lbl" id="prog-lbl">Starting…</div>
  </div>

  <!-- Output grid -->
  <div id="output-grid">
    <div class="pbox">
      <div class="pbox-head">
        📋 Results
        <span id="res-count" style="margin-left:auto;font-size:10px"></span>
      </div>
      <div class="pbox-body" id="results-list"></div>
    </div>
    <div class="pbox">
      <div class="pbox-head">
        <span class="blink" id="log-pulse" style="display:none">🔴</span>
        🖥 Live Log
        <button class="btn-ghost" style="margin-left:auto" onclick="clearLog()">clear</button>
      </div>
      <div class="pbox-body" id="detail-log"></div>
    </div>
  </div>

  <!-- Summary -->
  <div id="summary">
    <span class="c-ok" id="s-ok">—</span>
    <span class="c-err" id="s-err">—</span>
    <div class="s-dir" id="s-dir"></div>
  </div>

</div><!-- /wrap -->

<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentJob = null;
let evtSource  = null;
let okCount    = 0;
let errCount   = 0;
let totalCount = 0;
const videoMap = {};   // id → DOM element

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const names = ['channel','urls','settings','monitor'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'monitor') refreshStatus();
}

// ── Server status polling ──────────────────────────────────────────────────
function setDot(id, state, label) {
  const d = document.getElementById(id);
  d.className = 'dot ' + state;
}

async function refreshStatus() {
  try {
    const r = await fetch('/ping');
    const d = await r.json();

    // Header dots
    setDot('dot-server', 'ok');
    document.getElementById('lbl-server').textContent = 'online';
    setDot('dot-ytdlp',  d.yt_dlp ? 'ok' : 'err');
    document.getElementById('lbl-ytdlp').textContent  = d.yt_dlp ? 'yt-dlp ✔' : 'yt-dlp ✗';
    setDot('dot-api',    d.status === 'ok' ? 'ok' : 'warn');
    document.getElementById('lbl-api').textContent    = `api v${d.transcript_api}`;
    document.getElementById('lbl-jobs').textContent   = `${d.active_jobs} job${d.active_jobs !== 1 ? 's' : ''}`;

    // Monitor tab
    document.getElementById('m-server').textContent = 'Online';
    document.getElementById('m-server').className   = 'stat-val ok';
    document.getElementById('m-ytdlp').textContent  = d.yt_dlp ? 'Installed' : 'Missing';
    document.getElementById('m-ytdlp').className    = 'stat-val ' + (d.yt_dlp ? 'ok' : 'err');
    document.getElementById('m-api').textContent    = `v${d.transcript_api}`;
    document.getElementById('m-api').className      = 'stat-val ' + (d.status === 'ok' ? 'ok' : 'warn');
    document.getElementById('m-jobs').textContent   = d.active_jobs;
    document.getElementById('m-jobs').className     = 'stat-val ' + (d.active_jobs > 0 ? 'warn' : 'ok');
    document.getElementById('m-outdir').textContent = d.output_dir;

    // Jobs list
    loadJobsList();
  } catch(e) {
    setDot('dot-server', 'err');
    document.getElementById('lbl-server').textContent = 'offline';
  }
}

async function loadJobsList() {
  try {
    const r = await fetch('/jobs');
    const jobs = await r.json();
    const el = document.getElementById('jobs-list');
    const keys = Object.keys(jobs);
    if (!keys.length) {
      el.innerHTML = '<div style="padding:16px;color:var(--muted);font-family:var(--mono);font-size:11px">No jobs yet.</div>';
      return;
    }
    el.innerHTML = keys.map(jid => {
      const j = jobs[jid];
      const pct = j.total ? Math.round((j.ok + j.err) / j.total * 100) : 0;
      const color = j.status === 'done' ? (j.err ? 'c-warn' : 'c-ok') : 'c-act';
      return `<div style="padding:10px 12px;border-bottom:1px solid var(--border)">
        <div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:4px">${jid.slice(0,8)}…</div>
        <div style="font-family:var(--mono);font-size:12px" class="${color}">
          ${j.status.toUpperCase()} · ${j.ok}✔ ${j.err}✗ / ${j.total} · ${pct}%
        </div>
        ${j.outdir ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">${j.outdir}</div>` : ''}
      </div>`;
    }).join('');
  } catch(e) {}
}

// Poll status every 5s
setInterval(refreshStatus, 5000);
refreshStatus();

// ── Job control ────────────────────────────────────────────────────────────
function getSettings() {
  return {
    lang      : (document.getElementById('s-lang').value || 'en').trim(),
    timestamps: document.getElementById('s-ts').checked,
    force     : document.getElementById('s-force').checked,
  };
}

function startChannel() {
  const ch = document.getElementById('ch-input').value.trim();
  if (!ch) { toast('Enter a channel handle or URL.'); return; }
  const max = parseInt(document.getElementById('ch-max').value) || 0;
  const dir = document.getElementById('ch-dir').value.trim() || 'transcripts';
  const s   = getSettings();
  startJob({
    mode       : 'channel',
    channel    : ch,
    max_videos : max,
    output_dir : dir,
    ...s,
  }, 'btn-ch');
}

function startUrls() {
  const urls = document.getElementById('url-input').value.trim();
  if (!urls) { toast('Paste at least one YouTube URL.'); return; }
  const s = getSettings();
  startJob({
    mode      : 'urls',
    urls      : urls,
    output_dir: 'transcripts',
    ...s,
  }, 'btn-url');
}

async function startJob(body, btnId) {
  // Reset UI
  okCount = errCount = totalCount = 0;
  Object.keys(videoMap).forEach(k => delete videoMap[k]);
  document.getElementById('results-list').innerHTML = '';
  document.getElementById('detail-log').innerHTML   = '';
  document.getElementById('res-count').textContent  = '';
  document.getElementById('summary').style.display  = 'none';
  document.getElementById('output-grid').style.display = 'grid';
  document.getElementById('prog-wrap').style.display   = 'block';
  document.getElementById('prog-fill').style.width     = '0%';
  document.getElementById('prog-lbl').textContent      = 'Starting…';
  document.getElementById('log-pulse').style.display   = 'inline';

  const btn = document.getElementById(btnId);
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Running…';

  if (evtSource) { evtSource.close(); evtSource = null; }

  try {
    const res = await fetch('/start', {
      method : 'POST',
      headers: {'Content-Type': 'application/json'},
      body   : JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) { toast(data.error); resetBtn(btn); return; }
    currentJob = data.job_id;
    listenToJob(data.job_id, btn);
  } catch(e) {
    toast('Could not reach server: ' + e.message);
    resetBtn(btn);
  }
}

function listenToJob(jid, btn) {
  evtSource = new EventSource(`/events/${jid}`);

  evtSource.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev);
  };

  evtSource.onerror = () => {
    appendLog('⚠ Connection lost — job may still be running.', 'warn');
    document.getElementById('log-pulse').style.display = 'none';
    evtSource.close();
    resetBtn(btn);
  };

  evtSource.addEventListener('open', () => {
    appendLog('✔ Connected to server stream.', 'ok');
  });
}

function handleEvent(ev) {
  switch(ev.type) {

    case 'ping': break;

    case 'start':
      totalCount = ev.total || 0;
      appendLog(`🚀 ${ev.text}  (${totalCount} video${totalCount!==1?'s':''})`, 'head');
      updateProgress(0, totalCount);
      break;

    case 'log':
      appendLog(ev.text, ev.level || 'info');
      break;

    case 'video_start':
      upsertResult(ev.id, ev.url, 'act', `[${ev.index+1}/${ev.total}] Processing…`, '');
      break;

    case 'progress':
      updateProgress(ev.done, ev.total);
      break;

    case 'ok': {
      const kb = ev.size_kb ? ` · ${ev.size_kb} KB` : '';
      const t  = ev.elapsed ? ` · ${ev.elapsed}s` : '';
      upsertResult(ev.id, ev.path, 'ok', ev.path.split(/[\\/]/).pop(), `✔ saved${kb}${t}`);
      okCount++;
      updateCount();
      break;
    }

    case 'err':
      upsertResult(ev.id, ev.id, 'err', ev.id, `✗ ${ev.reason}`);
      errCount++;
      updateCount();
      break;

    case 'fatal':
      appendLog(`💥 FATAL: ${ev.text}`, 'error');
      toast(ev.text);
      break;

    case 'done':
      appendLog(`\n✅ Done — ${ev.ok} saved · ${ev.err} failed`, 'ok');
      appendLog(`📁 ${ev.outdir}`, 'info');
      showSummary(ev.ok, ev.err, ev.outdir);
      updateProgress(totalCount, totalCount);
      document.getElementById('log-pulse').style.display = 'none';
      document.querySelectorAll('.btn').forEach(b => {
        b.disabled = false;
        if (b.id === 'btn-ch')  b.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> Transcribe Channel';
        if (b.id === 'btn-url') b.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> Transcribe URLs';
      });
      if (evtSource) { evtSource.close(); evtSource = null; }
      refreshStatus();
      break;
  }
}

// ── UI helpers ─────────────────────────────────────────────────────────────
function updateProgress(done, total) {
  const pct = total ? Math.round(done / total * 100) : 0;
  document.getElementById('prog-fill').style.width = pct + '%';
  document.getElementById('prog-lbl').textContent  =
    total ? `${done} / ${total} videos  (${pct}%)` : 'Working…';
}

function updateCount() {
  document.getElementById('res-count').textContent =
    `${okCount}✔ ${errCount}✗`;
}

function upsertResult(id, title, cls, label, sub) {
  let el = videoMap[id];
  if (!el) {
    el = document.createElement('div');
    el.className = 'res';
    el.innerHTML = `<div class="res-title"></div><div class="res-sub"></div>`;
    document.getElementById('results-list').prepend(el);
    videoMap[id] = el;
  }
  el.className = `res ${cls}`;
  el.querySelector('.res-title').textContent = label;
  el.querySelector('.res-sub').innerHTML =
    `<span class="${cls==='ok'?'c-ok':cls==='err'?'c-err':'c-act'}">${sub}</span>`;
}

function appendLog(text, level='info') {
  const log = document.getElementById('detail-log');
  const div = document.createElement('div');
  div.className = `ll ${level}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  document.getElementById('detail-log').innerHTML = '';
}

function showSummary(ok, err, dir) {
  document.getElementById('s-ok').textContent  = `✔ ${ok} saved`;
  document.getElementById('s-err').textContent = `✗ ${err} failed`;
  document.getElementById('s-dir').textContent = dir;
  document.getElementById('summary').style.display = 'flex';
}

function resetBtn(btn) {
  if (!btn) return;
  btn.disabled = false;
  btn.innerHTML = btn.id === 'btn-ch'
    ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> Transcribe Channel'
    : '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> Transcribe URLs';
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 4000);
}
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

# ── HTML ──────────────────────────────────────────────────────────────────────
# (paste the full _HTML string from my previous message here)
# It starts with: _HTML = "<!DOCTYPE html>..."

if __name__ == "__main__":
    print("  VT2  |  http://localhost:" + str(PORT))
    print("  pip install flask youtube-transcript-api yt-dlp")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)