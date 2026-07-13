from google import genai
from dotenv import load_dotenv
import os
import requests
from flask import Flask, request, jsonify, render_template, send_file
from google.genai import types
import re
import time
import threading
import io
import rarfile
import zipfile
load_dotenv()


app = Flask(__name__)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options={'timeout': 60 * 60 * 1000})

rarfile.UNRAR_TOOL = r"C:\Program Files\7-Zip\7z.exe"

subs_dir = os.path.join(os.path.dirname(__file__), 'subs')
cache_dir = os.path.join(os.path.dirname(__file__), "cache")


SYSTEM_INSTRUCTION = '''
You are a subtitle translator.

Translate ONLY the subtitle text into Romanian.

Rules:

- Preserve the subtitle format EXACTLY.
- Preserve numbering.
- Preserve timestamps.
- Preserve empty lines.
- Preserve HTML tags.
- Preserve ASS tags.
- Preserve styling.
- Do not renumber subtitles.
- Do not merge subtitles.
- Do not split subtitles.
- Translate ONLY dialogue.

Return ONLY the translated subtitle file.

DO NOT:

- explain
- comment
- use markdown
- use bullet lists
- write "Translation:"
- wrap the output in code fences
- add any text before or after the subtitle file

The output MUST be a valid subtitle file.
'''

config = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    temperature=0
)


MAX_INPUT_TOKENS = 5000
MAX_RETRIES = 5
RETRY_DELAY = 5  # secunde

CHARS_PER_TOKEN = 3.2
SAFETY_MARGIN = 0.85
EFFECTIVE_MAX_TOKENS = int(MAX_INPUT_TOKENS * SAFETY_MARGIN)

os.makedirs(subs_dir, exist_ok=True)
os.makedirs(cache_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Coordonare joburi: evită traduceri duplicate pentru același imdb_id și
# expune un status "live" pe care frontend-ul îl poate interoga.
# ---------------------------------------------------------------------------
_locks_guard = threading.Lock()
_id_locks = {}          # imdb_id -> threading.Lock, unul per titlu
_job_status = {}         # imdb_id -> dict cu stage / progres
_status_guard = threading.Lock()





def get_lock_for(imdb_id):
    """Returnează (creând-o dacă lipsește) o încuietoare dedicată unui imdb_id,
    ca să nu pornim două traduceri simultane pentru același titlu."""
    with _locks_guard:
        if imdb_id not in _id_locks:
            _id_locks[imdb_id] = threading.Lock()
        return _id_locks[imdb_id]


def set_status(imdb_id, **fields):
    with _status_guard:
        current = _job_status.get(imdb_id, {})
        current.update(fields)
        current.setdefault("started_at", time.time())
        _job_status[imdb_id] = current


def clear_status(imdb_id):
    with _status_guard:
        _job_status.pop(imdb_id, None)


def get_status(imdb_id):
    with _status_guard:
        return dict(_job_status.get(imdb_id, {}))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def split_by_tokens(content):
    try:
        if not content:
            raise ValueError("Subtitle content is empty.")

        content = content.replace("\r\n", "\n").strip()
        blocks = re.split(r"\n\s*\n", content)

        if not blocks:
            raise ValueError("No subtitle blocks found.")

        chunks = []
        current_blocks = []
        current_tokens = 0

        for block in blocks:
            block = block.strip()

            if not block:
                continue

            block_tokens = estimate_tokens(block)

            if block_tokens > EFFECTIVE_MAX_TOKENS:
                if current_blocks:
                    chunks.append("\n\n".join(current_blocks))
                    current_blocks = []
                    current_tokens = 0

                chunks.append(block)
                continue

            if current_tokens + block_tokens > EFFECTIVE_MAX_TOKENS:
                chunks.append("\n\n".join(current_blocks))
                current_blocks = [block]
                current_tokens = block_tokens
            else:
                current_blocks.append(block)
                current_tokens += block_tokens

        if current_blocks:
            chunks.append("\n\n".join(current_blocks))

        if not chunks:
            raise ValueError("No chunks generated.")

        print(f"Generated {len(chunks)} chunks")

        for i, chunk in enumerate(chunks, start=1):
            try:
                tokens = client.models.count_tokens(
                    model="gemini-3.5-flash",
                    contents=chunk
                ).total_tokens

                print(f"Chunk {i}: ~{estimate_tokens(chunk)} estimated / {tokens} real tokens")

            except Exception as e:
                print(f"Failed to count tokens for chunk {i}: {e}")

        return chunks

    except Exception as e:
        print(f"split_by_tokens failed: {e}")
        raise


def translate_subs(file_content, imdb_id):
    print(f"GENERATING SUBS FOR {imdb_id}")

    set_status(imdb_id, stage="splitting", message="Se împarte subtitrarea în bucăți…")
    chunks = split_by_tokens(file_content)
    translated = []

    set_status(
        imdb_id,
        stage="translating",
        total_chunks=len(chunks),
        current_chunk=0,
        message="Se traduce în română…"
    )

    for i, chunk in enumerate(chunks, start=1):
        print(f"Chunk {i}/{len(chunks)}")
        set_status(imdb_id, current_chunk=i)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    config=config,
                    contents=chunk
                )

                candidate = response.candidates[0]
                print(candidate.finish_reason)

                translated.append(response.text)
                break

            except Exception as e:
                print(f"[Chunk {i}] Attempt {attempt}/{MAX_RETRIES} failed: {e}")
                set_status(
                    imdb_id,
                    message=f"Reîncercare pentru bucata {i}/{len(chunks)} (încercarea {attempt}/{MAX_RETRIES})…"
                )

                if attempt == MAX_RETRIES:
                    raise

                time.sleep(RETRY_DELAY * attempt)

    set_status(imdb_id, stage="saving", message="Se salvează fișierul final…")

    result = "\n\n".join(translated)

    with open(
        os.path.join(subs_dir, f"{imdb_id}.srt"),
        "w",
        encoding="utf-8"
    ) as f:
        f.write(result)
    path = os.path.join(subs_dir, f"{imdb_id}.srt")

    print("\n===== FILE SAVED =====")
    print("Path:", path)
    print("Exists:", os.path.exists(path))
    print("Size:", os.path.getsize(path))
    print("======================\n")

    return result


def get_subs_opensubs(imdb_id):
    print("========================================")
    print(f"Searching subtitles for {imdb_id}")

    set_status(imdb_id, stage="searching", message="Se caută subtitrarea în engleză…")

    search = requests.get(
        "https://sub.wyzie.io/search",
        params={
            "id": imdb_id,
            "language": "en",
            "format": "srt",
            "key": os.getenv('WYZIE_API_KEY')
        }
    )

    search.raise_for_status()
    subtitles = search.json()

    if not subtitles:
        raise Exception("No subtitles found.")

    subtitle = subtitles[0]

    print("Release:", subtitle["release"])
    print("Source:", subtitle["source"])
    print("Language:", subtitle["language"])
    print("Downloading:", subtitle["url"])

    set_status(imdb_id, stage="downloading", message="Se descarcă subtitrarea originală…")

    file = requests.get(subtitle["url"])
    file.raise_for_status()

    extension = subtitle.get("format", "srt")
    cache_file = os.path.join(cache_dir, f"{imdb_id}.{extension}")

    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(file.text)

    try:
        translate_subs(file.text, imdb_id=imdb_id)
    except Exception as e:
        print(e)
        raise
    finally:
        if os.path.exists(cache_file):
            os.remove(cache_file)

def get_subs_ro(imdb_id):
    api_key = os.getenv("SUBS_RO_API_KEY")

    if not api_key:
        raise RuntimeError("SUBS_RO_API_KEY nu este configurată.")

    set_status(
        imdb_id,
        stage="searching",
        message="Se caută subtitrarea..."
    )

    print("========================================")
    print("SUBS.RO")
    print("IMDb:", imdb_id)

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    response = requests.get(
        f"https://api.subs.ro/v1.0/search/imdbid/{imdb_id}",
        headers={
            "X-Subs-Api-Key": api_key
        },
        params={
            "language": "en"
        },
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    print("Search status:", response.status_code)
    print("Found subtitles:", data.get("count"))

    if data.get("count", 0) == 0:
        raise Exception("Nu s-au găsit subtitrări.")

    subtitle_info = next(
        (x for x in data["items"] if x.get("language") == "en"),
        data["items"][0]
    )

    download_url = subtitle_info["downloadLink"]

    print("Download:", download_url)

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    set_status(
        imdb_id,
        stage="downloading",
        message="Se descarcă subtitrarea..."
    )

    subtitle = requests.get(
        download_url,
        headers={
            "X-Subs-Api-Key": api_key
        },
        timeout=60
    )

    subtitle.raise_for_status()

    raw = subtitle.content
    print(raw)

    print("========================================")
    print("DOWNLOAD")
    print("Status:", subtitle.status_code)
    print("Content-Type:", subtitle.headers.get("Content-Type"))
    print("Size:", len(raw))
    print("Magic:", raw[:8])
    print("========================================")

    file_content = None

    # --------------------------------------------------------
    # ZIP
    # --------------------------------------------------------

    if raw.startswith(b"PK"):

        print("ZIP detected")

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:

            print("Files:", archive.namelist())

            srt_name = next(
                (
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".srt")
                ),
                None
            )

            if srt_name is None:
                raise Exception("ZIP-ul nu conține fișier .srt")

            with archive.open(srt_name) as fp:
                file_content = fp.read().decode(
                    "utf-8",
                    errors="replace"
                )

    # --------------------------------------------------------
    # RAR
    # --------------------------------------------------------

    elif raw.startswith(b"Rar!"):

        print("RAR detected")

        rar_path = os.path.join(
            cache_dir,
            f"{imdb_id}.rar"
        )

        with open(rar_path, "wb") as f:
            f.write(raw)

        try:

            with rarfile.RarFile(rar_path) as archive:

                print("Files:", archive.namelist())

                srt_name = next(
                    (
                        name
                        for name in archive.namelist()
                        if name.lower().endswith(".srt")
                    ),
                    None
                )

                if srt_name is None:
                    raise Exception("RAR-ul nu conține fișier .srt")

                with archive.open(srt_name) as fp:
                    file_content = fp.read().decode(
                        "utf-8",
                        errors="replace"
                    )

        finally:

            if os.path.exists(rar_path):
                os.remove(rar_path)

    # --------------------------------------------------------
    # DIRECT SRT
    # --------------------------------------------------------

    else:

        print("Trying direct SRT...")

        try:
            file_content = raw.decode(
                "utf-8",
                errors="replace"
            )

            if "-->" not in file_content:
                raise Exception()

            print("Direct SRT detected.")

        except Exception:

            print(file_content[:500] if file_content else raw[:200])

            raise Exception(
                "Subs.ro nu a returnat ZIP, RAR sau SRT."
            )

    # --------------------------------------------------------
    # VALIDARE
    # --------------------------------------------------------

    if not file_content:
        raise Exception("Subtitrarea este goală.")

    print("--------------------------------")
    print("Subtitle length:", len(file_content))
    print(file_content[:1000])
    print("--------------------------------")

    # --------------------------------------------------------
    # TRADUCERE
    # --------------------------------------------------------

    translate_subs(file_content, imdb_id)

    print("Subtitle processed successfully.")


@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@app.route('/api/titles/<string:title_id>')
def get_title(title_id):
    provider = request.args.get("provider")

    print("\n==============================")
    print("NEW REQUEST")
    print("Title:", title_id)
    print("Provider:", provider)

    file_path = os.path.join(
        os.path.dirname(__file__),
        "subs",
        f"{title_id}.srt"
    )

    print("File path:", file_path)
    print("Exists before:", os.path.exists(file_path))

    # dacă există deja
    if os.path.exists(file_path):
        print("Serving cached subtitle.")
        return send_file(
            file_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"{title_id}.srt"
        )

    lock = get_lock_for(title_id)

    acquired = lock.acquire(blocking=False)

    if not acquired:
        print("Lock already acquired, waiting...")

        set_status(
            title_id,
            message=get_status(title_id).get(
                "message",
                "Traducere deja în curs..."
            )
        )

        lock.acquire()
        lock.release()

        print("Lock released.")

        print("Exists after waiting:", os.path.exists(file_path))

        if os.path.exists(file_path):
            print("Serving generated subtitle.")
            return send_file(
                file_path,
                mimetype="text/plain",
                as_attachment=True,
                download_name=f"{title_id}.srt"
            )

        print("Subtitle still missing.")
        return jsonify({
            "ok": False,
            "message": "Title not found"
        }), 404

    try:
        print("Lock acquired.")

        if provider == "opensubtitles":
            print("Using OpenSubtitles provider")
            get_subs_opensubs(title_id)

        elif provider == "subsro":
            print("Using Subs.ro provider")
            get_subs_ro(title_id)

        else:
            print("Invalid provider:", provider)
            return jsonify({
                "ok": False,
                "message": "Incorrect provider"
            }), 400

        print("Provider function finished.")

    except Exception as e:
        import traceback

        print("\n========== EXCEPTION ==========")
        traceback.print_exc()
        print("Exception:", e)
        print("===============================\n")

        return jsonify({
            "ok": False,
            "message": str(e)
        }), 500

    finally:
        print("Clearing status")
        clear_status(title_id)

        print("Releasing lock")
        lock.release()

    print("Checking generated file...")
    print("Exists:", os.path.exists(file_path))

    if os.path.exists(file_path):
        print("SUCCESS -> sending subtitle")

        return send_file(
            file_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"{title_id}.srt"
        )

    print("FAILED -> file was never created")
    print("==============================\n")

    return jsonify({
        "ok": False,
        "message": "Subtitle file was not created"
    }), 404


@app.route('/api/titles/<string:title_id>/status')
def get_title_status(title_id):
    file_path = os.path.join(subs_dir, f"{title_id}.srt")

    if os.path.exists(file_path):
        return jsonify({"ok": True, "stage": "done", "message": "Fișier disponibil."})

    status = get_status(title_id)

    if not status:
        return jsonify({"ok": True, "stage": "idle", "message": "Nicio traducere activă pentru acest titlu."})

    return jsonify({"ok": True, **status})