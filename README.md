# 🎬 VT2 Harvester

> Bulk-fetch transcripts from any YouTube channel or playlist — fast, resilient, and proxy-ready.

![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white)
![youtube-transcript-api](https://img.shields.io/badge/youtube--transcript--api-v1.x-FF0000?style=flat-square&logo=youtube&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)
![Status](https://img.shields.io/badge/status-active-22c55e?style=flat-square)

---

## ✨ Features

- 📺 **Channel & playlist scraping** — point it at a `@handle`, channel ID, or playlist URL
- 📝 **Smart transcript fetching** — tries your language first, then 10 fallback languages, then auto-discovers whatever's available
- 🌍 **Multi-language support** — manual captions preferred over auto-generated, with graceful fallback
- ⏱ **Optional timestamps** — include or strip `[MM:SS]` markers
- 🔄 **Proxy & cookie support** — bypass YouTube IP bans with Webshare proxies or browser cookies
- 🚀 **Concurrent processing** — transcribes multiple videos in parallel
- 📄 **URL list export** — writes resolved video URLs to `urls.txt` before processing
- 🖥 **Live log streaming** — real-time status updates via server-sent events

---

## 🚀 Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/mouloudelarram/videoToText.git
cd videoToText

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python main.py --url https://www.youtube.com/@googlecloudtech --limit 10
```

---

## 📦 Installation

```bash
pip install youtube-transcript-api yt-dlp
```

---

## 🛠 Usage

```python
from harvester import get_transcript

# Basic usage
text, error = get_transcript("VIDEO_ID", lang="en", timestamps=False)

# With timestamps
text, error = get_transcript("VIDEO_ID", lang="en", timestamps=True)

# Verbose mode off
text, error = get_transcript("VIDEO_ID", verbose=False)
```

### CLI

```bash
# Scrape a channel (latest 50 videos)
python main.py --url https://www.youtube.com/@mkbhd --limit 50

# Specific language + timestamps
python main.py --url https://www.youtube.com/@lexfridman --lang fr --timestamps

# Output to custom directory
python main.py --url https://www.youtube.com/@googlecloudtech --output ./transcripts
```

---

## 🌐 Language Selection Logic

The fetcher tries languages in this priority order:

```
1. Your requested language (e.g. "en")
2. Fallback chain: en → ar → fr → es → de → ja → zh → pt → ru → ko
3. Auto-discover all available transcripts
   └── Ranks by: manual > auto-generated, preferred lang > other
```

---

## 🔒 Bypassing YouTube IP Bans

Cloud servers (AWS, GCP, Azure, etc.) are often blocked by YouTube. Two workarounds:

### Option A — Webshare Proxy *(recommended)*

1. Sign up at [webshare.io](https://www.webshare.io) (free tier available)
2. Grab your `proxy_username` and `proxy_password` from the dashboard
3. Configure:

```python
from youtube_transcript_api.proxies import WebshareProxyConfig

api = YouTubeTranscriptApi(
    proxy_config=WebshareProxyConfig(
        proxy_username="YOUR_USERNAME",
        proxy_password="YOUR_PASSWORD",
    )
)
```

### Option B — Browser Cookies

1. Install **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** (Chrome) or equivalent Firefox extension
2. Log into youtube.com, click the extension, export `cookies.txt`
3. Upload to your server and configure:

```python
api = YouTubeTranscriptApi(cookie_path="/path/to/cookies.txt")
```

> ⚠️ Use a **throwaway Google account** for cookies. Cookies expire and will need periodic re-export.

---

## 📁 Output Structure

```
output/
├── GDm_uH6VxPY.txt
├── aB3cD4eF5gH.txt
└── ...
urls.txt               ← all resolved video URLs
```

Each transcript file:
```
# video_id: GDm_uH6VxPY
# language: en
# ─────────────────────────────────────────
[00:04] Welcome to Google Cloud Tech...
[00:12] Today we're looking at...
...
```

---

## ⚙️ Configuration

| Parameter    | Default | Description                              |
|-------------|---------|------------------------------------------|
| `lang`       | `en`    | Preferred transcript language            |
| `timestamps` | `False` | Include `[MM:SS]` markers in output      |
| `verbose`    | `True`  | Print live progress to stdout            |
| `limit`      | `None`  | Max videos to process (None = all)       |
| `output`     | `./output` | Directory to write transcript files  |
| `cookie_path`| `None`  | Path to `cookies.txt` for auth           |

---

## 📊 Live Log Output

```
🖥 Live Log
✔ Connected to server stream.
📺 Resolving: https://www.youtube.com/@googlecloudtech
  ✔ Found 1509 videos: Google Cloud Tech
  Trimmed to 5
  📄 urls.txt written (5 URLs)
🚀 Starting 5 video(s)...
📝 Transcribing 5 → output
 [1/5] https://www.youtube.com/watch?v=GDm_uH6VxPY
     Trying: en...
     ✔ Found captions: en
  ✔ Done
```

---

## 🤝 Contributing

PRs welcome. Please open an issue first for major changes.

---

## 📄 License

MIT © 2024 — do whatever you want, just don't blame us if YouTube bans your IP.

---

<p align="center">
  Built with ☕ and a healthy disregard for rate limits.
</p>


<img width="1901" height="863" alt="image" src="https://github.com/user-attachments/assets/eb936583-7a23-4db8-90b2-fabafbb8cf14" />
