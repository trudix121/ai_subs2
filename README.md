# AI Subs 2

Automatic subtitle translation from English to **Romanian**, powered by **Google Gemini**.

A Flask web app + Stremio addon that:

- looks up English subtitles by **IMDb ID**
- translates them automatically into Romanian
- supports **uploading** your own `.srt` files
- exposes a **Stremio addon** for Romanian subtitles on movies and series

---

## Features

| Feature | Description |
|---------|-------------|
| **IMDb search** | Enter `tt1234567` (or an IMDb link) → download EN subtitles → translate to RO |
| **Upload .srt** | Upload an English `.srt` → AI translates it while preserving the exact format |
| **Stremio addon** | Manifest + endpoints for Romanian subtitles on movies/series |
| **Cache** | Translations are stored locally (`subs/`) — subsequent requests are instant |
| **Live progress** | UI shows stage: search → download → splitting → translation → save |
| **Robust validation** | Encoding detection, SRT structure checks, 2MB limit, API retries |

---

## Tech stack

- **Backend**: Flask + Waitress
- **AI**: Google Gemini (`google-genai`) — model `gemini-3.1-flash-lite`
- **Frontend**: Vanilla HTML/CSS/JS (dark UI)
- **Subtitle source**: [Wyzie](https://sub.wyzie.io) API
- **Extras**: `python-dotenv`, `requests`, `chardet`

---

## Project structure

```
ai_subs2/
├── main.py              # Flask app (routes, translation, Stremio)
├── start.py             # Entry point (Waitress)
├── requirements.txt
├── templates/
│   └── index.html       # Web UI
├── subs/                # Translated subtitles (generated, gitignored)
├── cache/               # Temporary cache (gitignored)
├── .env                 # Environment variables (do not commit)
└── logs.log             # Request logs
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/trudix121/ai_subs2.git
cd ai_subs2
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure `.env`

Create a `.env` file in the project root:

```env
# Required
GEMINI_API_KEY=your_gemini_api_key
WYZIE_API_KEY=your_wyzie_api_key

# Server
HOST_NAME=0.0.0.0
PORT=5000

# Stremio (optional, for the addon)
REDIRECT_HOST_NAME=your.domain.com
STREMIO_PORT=
```

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini API key |
| `WYZIE_API_KEY` | Wyzie API key (subtitle search) |
| `HOST_NAME` | Host Waitress listens on |
| `PORT` | Server port |
| `REDIRECT_HOST_NAME` | Public domain for Stremio subtitle URLs |
| `STREMIO_PORT` | Public port (if different / required) |

### 5. Start the server

```bash
python start.py
```

The app will run at `http://HOST_NAME:PORT`.

---

## Usage

### Web UI

1. Open `http://localhost:5000` (or your domain).
2. **“Search by ID” tab**:
   - enter an IMDb ID (`tt0111161`) or a full IMDb link
   - click **Search**
   - wait for translation → the `.srt` file downloads automatically
3. **“Upload file” tab**:
   - drag or select an English `.srt` (max 2MB)
   - click **Translate**
   - after processing, the RO subtitle downloads

### Stremio addon

1. Make sure the server is publicly reachable (HTTPS recommended).
2. In Stremio → **Addons** → **Community** / **Install from URL**:
   ```
   https://YOUR_DOMAIN/manifest.json
   ```
3. The addon appears as **AI Romanian Subtitles**.
4. On a movie/series with an IMDb ID (`tt...`), Stremio requests the Romanian subtitle; if it doesn’t exist yet, the server generates it.

**Relevant Stremio endpoints:**

| Route | Role |
|-------|------|
| `GET /manifest.json` | Addon manifest |
| `GET /subtitles/<type>/<id>.json` | Lists the RO subtitle (generates it if missing) |
| `GET /stremio/subtitles/<imdb_id>.srt` | Serves the translated `.srt` file |

---

## API overview

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/api/titles/<imdb_id>` | Search + translate + download subtitle |
| `GET` | `/api/titles/<imdb_id>/status` | Job status (search / translating / done…) |
| `POST` | `/api/upload` | Upload `.srt` → translation job |
| `GET` | `/api/jobs/<job_id>/status` | Upload job status |
| `GET` | `/api/jobs/<job_id>/download` | Download the translated subtitle |

Upload returns a `job_id` (content hash). If the same file was translated before, the response includes `cached: true`.

---

## How translation works

1. The subtitle is split into **chunks** based on token estimation (~5000 input tokens limit, with a safety margin).
2. Each chunk is sent to Gemini with a strict **system prompt**:
   - translate **only** dialogue
   - preserve numbering, timestamps, blank lines, HTML/ASS tags
   - no explanations, no markdown — only the SRT file
3. Translated chunks are joined and saved to `subs/<id>.srt`.
4. There are **retries** (up to 5 attempts) and **per-job locks** so the same job never runs twice concurrently.

---

## Notes

- Only **`.srt`** files are accepted on upload.
- Maximum upload size: **2MB** (configurable in code).
- Encoding is detected automatically (`chardet` + common East-European fallbacks).
- Folders `subs/`, `cache/`, `.env`, and `logs.log` are gitignored.

---

## License

Private / personal project. Use at your own risk; comply with Google Gemini and subtitle provider terms of service.
