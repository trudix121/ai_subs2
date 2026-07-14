from google import genai
from dotenv import load_dotenv
import os
import requests
from flask import Flask, request, jsonify, render_template, send_file
from google.genai import types
import re
import time
import threading
import hashlib
import logging
load_dotenv()

try:
    import chardet
except ImportError:  # fallback minimal dacă pachetul nu e instalat
    chardet = None


app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024  # 3MB — limită hard la nivel Flask, înainte de orice citire

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options={'timeout': 60 * 60 * 1000})

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

# --- validare upload -------------------------------------------------------
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2MB
MIN_SUBTITLE_BLOCKS = 3
MIN_VALID_BLOCK_RATIO = 0.8

SRT_BLOCK_RE = re.compile(
    r"^\d+\s*\n"
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}.*\n"
    r"(?:.+\n?)+",
    re.MULTILINE
)

os.makedirs(subs_dir, exist_ok=True)
os.makedirs(cache_dir, exist_ok=True)


class SubtitleValidationError(Exception):
    """Ridicată când fișierul încărcat de utilizator nu trece validarea."""
    pass




logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s"
)
request_logger = logging.getLogger("http")


@app.before_request
def log_request_start():
    request._start_time = time.time()
    request_logger.info(
        f"--> {request.method} {request.path} "
        f"| query={dict(request.args)} "
        f"| ip={request.remote_addr} "
        f"| ua={request.headers.get('User-Agent', '-')}"
    )


@app.after_request
def log_request_end(response):
    duration_ms = None
    if hasattr(request, "_start_time"):
        duration_ms = round((time.time() - request._start_time) * 1000, 1)

    request_logger.info(
        f"<-- {request.method} {request.path} "
        f"| status={response.status_code} "
        f"| duration={duration_ms}ms"
    )

    return response


# ---------------------------------------------------------------------------
# Coordonare joburi: evită traduceri duplicate pentru același job_id și
# expune un status "live" pe care frontend-ul îl poate interoga.
#
# job_id poate fi:
#   - un IMDb ID (ex: "tt1234567"), pentru fluxul de căutare online
#   - un hash "up_<sha256>" al conținutului, pentru fluxul de upload manual
# ---------------------------------------------------------------------------
_locks_guard = threading.Lock()
_id_locks = {}          # job_id -> threading.Lock, unul per titlu/upload
_job_status = {}         # job_id -> dict cu stage / progres
_status_guard = threading.Lock()


def get_lock_for(job_id):
    """Returnează (creând-o dacă lipsește) o încuietoare dedicată unui job_id,
    ca să nu pornim două traduceri simultane pentru același conținut."""
    with _locks_guard:
        if job_id not in _id_locks:
            _id_locks[job_id] = threading.Lock()
        return _id_locks[job_id]


def set_status(job_id, **fields):
    with _status_guard:
        current = _job_status.get(job_id, {})
        current.update(fields)
        current.setdefault("started_at", time.time())
        _job_status[job_id] = current


def clear_status(job_id):
    with _status_guard:
        _job_status.pop(job_id, None)


def get_status(job_id):
    with _status_guard:
        return dict(_job_status.get(job_id, {}))


def estimate_tokens(text: str) -> int:
    """
    Estimare rapidă a numărului de tokeni, fără apel API.
    Nu e exactă, dar e suficient de bună pentru decizia de splitting
    și nu consumă CPU/rețea suplimentar.
    """
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


# ---------------------------------------------------------------------------
# Validare fișiere încărcate manual de utilizator
# ---------------------------------------------------------------------------
def decode_upload(raw_bytes: bytes) -> str:
    """Decodează bytes brut într-un string text, încercând să detecteze
    encoding-ul real al fișierului (multe .srt sunt salvate în cp1250/latin-1)."""
    if not raw_bytes:
        raise SubtitleValidationError("Fișierul este gol.")

    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise SubtitleValidationError("Fișierul depășește dimensiunea maximă permisă (2MB).")

    encoding = None
    if chardet is not None:
        detected = chardet.detect(raw_bytes)
        encoding = detected.get("encoding")

    candidates = [encoding] if encoding else []
    candidates += ["utf-8-sig", "utf-8", "cp1250", "iso-8859-2", "latin-1"]

    for enc in candidates:
        if not enc:
            continue
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    raise SubtitleValidationError("Nu am putut determina encoding-ul fișierului.")


def validate_srt_structure(content: str) -> str:
    """Verifică rapid, fără AI, că fișierul chiar arată ca un .srt valid:
    numerotare, timestamp-uri în formatul corect, text asociat.
    Blochează fișiere goale, corupte, sau texte arbitrare deghizate în .srt."""
    content = content.replace("\r\n", "\n").strip()

    if not content:
        raise SubtitleValidationError("Fișierul este gol.")

    if len(content) > MAX_UPLOAD_BYTES:
        raise SubtitleValidationError("Conținutul fișierului este prea mare.")

    blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]

    if len(blocks) < MIN_SUBTITLE_BLOCKS:
        raise SubtitleValidationError(
            "Structura fișierului nu corespunde formatului .srt "
            "(sunt necesare cel puțin 3 blocuri cu număr / timestamp / text)."
        )

    valid_blocks = [b for b in blocks if SRT_BLOCK_RE.match(b)]
    ratio = len(valid_blocks) / len(blocks)

    if ratio < MIN_VALID_BLOCK_RATIO:
        raise SubtitleValidationError(
            "Prea multe blocuri au un format invalid — fișierul nu pare a fi un .srt corect."
        )

    return content


def compute_job_id(content: str) -> str:
    """ID de job derivat din conținut (hash), pentru deduplicare și cache:
    dacă doi utilizatori încarcă exact același fișier, îl traducem o singură dată."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
    return f"up_{digest}"


# ---------------------------------------------------------------------------
# Traducere (comună pentru fluxul IMDb și fluxul de upload)
# ---------------------------------------------------------------------------
def translate_and_save(file_content, job_id):
    print(f"GENERATING SUBS FOR {job_id}")

    set_status(job_id, stage="splitting", message="Se împarte subtitrarea în bucăți…")
    chunks = split_by_tokens(file_content)
    translated = []

    set_status(
        job_id,
        stage="translating",
        total_chunks=len(chunks),
        current_chunk=0,
        message="Se traduce în română…"
    )

    for i, chunk in enumerate(chunks, start=1):
        print(f"Chunk {i}/{len(chunks)}")
        set_status(job_id, current_chunk=i)

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
                    job_id,
                    message=f"Reîncercare pentru bucata {i}/{len(chunks)} (încercarea {attempt}/{MAX_RETRIES})…"
                )

                if attempt == MAX_RETRIES:
                    raise

                time.sleep(RETRY_DELAY * attempt)

    set_status(job_id, stage="saving", message="Se salvează fișierul final…")

    result = "\n\n".join(translated)

    with open(
        os.path.join(subs_dir, f"{job_id}.srt"),
        "w",
        encoding="utf-8"
    ) as f:
        f.write(result)

    set_status(job_id, stage="done", message="Fișier disponibil.")

    return result


def get_subs_from_imdb(imdb_id):
    """Fluxul original: caută subtitrarea în engleză online, o descarcă,
    apoi o trimite la traducere."""
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
        translate_and_save(file.text, job_id=imdb_id)
    except Exception as e:
        print(e)
        raise
    finally:
        if os.path.exists(cache_file):
            os.remove(cache_file)


# ---------------------------------------------------------------------------
# Rute
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@app.route('/api/titles/<string:title_id>')
def get_title(title_id):
    """Flux original — căutare + traducere sincronă după IMDb ID."""
    file_path = os.path.join(subs_dir, f"{title_id}.srt")

    # Cazul rapid: fișierul există deja (cache permanent) — servim direct,
    # fără să atingem lock-ul sau API-ul Gemini.
    if os.path.exists(file_path):
        return send_file(
            file_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"{title_id}.srt"
        )

    lock = get_lock_for(title_id)

    # Încercăm să prindem lock-ul fără să blocăm. Dacă altcineva îl ține deja,
    # înseamnă că exact acest titlu se traduce chiar acum — nu mai pornim
    # o a doua traducere, ci așteptăm să se termine prima.
    acquired = lock.acquire(blocking=False)

    if not acquired:
        set_status(title_id, message=get_status(title_id).get("message", "Traducere deja în curs…"))
        lock.acquire()  # blocăm până se eliberează (adică până termină celălalt request)
        lock.release()

        if os.path.exists(file_path):
            return send_file(
                file_path,
                mimetype="text/plain",
                as_attachment=True,
                download_name=f"{title_id}.srt"
            )

        return jsonify({
            "ok": False,
            "message": "Title not found"
        }), 404

    try:
        get_subs_from_imdb(title_id)
    except Exception:
        return jsonify({
            "ok": False,
            "message": "Title not found"
        }), 404
    finally:
        clear_status(title_id)
        lock.release()

    if os.path.exists(file_path):
        return send_file(
            file_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=f"{title_id}.srt"
        )

    return jsonify({
        "ok": False,
        "message": "Title not found"
    }), 404


@app.route('/api/titles/<string:title_id>/status')
def get_title_status(title_id):
    """Endpoint optional pentru progres real (folosit de frontend prin polling,
    în loc de un timer aproximativ)."""
    file_path = os.path.join(subs_dir, f"{title_id}.srt")

    if os.path.exists(file_path):
        return jsonify({"ok": True, "stage": "done", "message": "Fișier disponibil."})

    status = get_status(title_id)

    if not status:
        return jsonify({"ok": True, "stage": "idle", "message": "Nicio traducere activă pentru acest titlu."})

    return jsonify({"ok": True, **status})


@app.route('/api/upload', methods=['POST'])
def upload_subtitle():
    """Flux nou — utilizatorul încarcă manual un .srt.

    Trece prin validare (encoding + structură) ÎNAINTE de a ajunge la Gemini,
    apoi pornește traducerea asincron (thread separat) și returnează imediat
    un job_id pe care frontend-ul îl folosește pentru polling și download.
    """
    if 'file' not in request.files:
        return jsonify({"ok": False, "message": "Niciun fișier trimis."}), 400

    file = request.files['file']

    if not file.filename:
        return jsonify({"ok": False, "message": "Niciun fișier selectat."}), 400

    if not file.filename.lower().endswith('.srt'):
        return jsonify({"ok": False, "message": "Sunt acceptate doar fișiere cu extensia .srt."}), 400

    raw = file.read()

    try:
        text = decode_upload(raw)
        text = validate_srt_structure(text)
    except SubtitleValidationError as e:
        return jsonify({"ok": False, "message": str(e)}), 422

    job_id = compute_job_id(text)
    file_path = os.path.join(subs_dir, f"{job_id}.srt")

    # Deja tradus anterior (cineva a mai încărcat exact același conținut) —
    # nu mai apelăm Gemini încă o dată.
    if os.path.exists(file_path):
        return jsonify({"ok": True, "job_id": job_id, "cached": True})

    lock = get_lock_for(job_id)
    acquired = lock.acquire(blocking=False)

    if not acquired:
        # E deja o traducere în curs pentru exact acest conținut —
        # frontend-ul va face polling pe status cu același job_id.
        return jsonify({"ok": True, "job_id": job_id, "already_running": True})

    set_status(job_id, stage="queued", message="Fișierul a trecut de verificare, se pregătește traducerea…")

    def run():
        try:
            translate_and_save(text, job_id=job_id)
        except Exception as e:
            print(f"[upload:{job_id}] translation failed: {e}")
            set_status(job_id, stage="error", message="Traducerea a eșuat. Încearcă din nou.")
        finally:
            lock.release()

    threading.Thread(target=run, daemon=True).start()

    return jsonify({"ok": True, "job_id": job_id, "cached": False})


@app.route('/api/jobs/<string:job_id>/status')
def job_status(job_id):
    """Status generic — folosit atât pentru joburi de upload, cât și,
    dacă vrei, ca alias pentru joburi IMDb (job_id = imdb_id)."""
    file_path = os.path.join(subs_dir, f"{job_id}.srt")

    if os.path.exists(file_path):
        return jsonify({"ok": True, "stage": "done", "message": "Fișier disponibil."})

    status = get_status(job_id)

    if not status:
        return jsonify({"ok": True, "stage": "idle", "message": "Niciun job activ pentru acest ID."})

    return jsonify({"ok": True, **status})


@app.route('/api/jobs/<string:job_id>/download')
def job_download(job_id):
    file_path = os.path.join(subs_dir, f"{job_id}.srt")

    if not os.path.exists(file_path):
        return jsonify({"ok": False, "message": "Fișierul nu este (încă) disponibil."}), 404

    return send_file(
        file_path,
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"{job_id}.srt"
    )

# Stremio Addon Routes

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "id": "trudx.aisubs",
        "version": "1.0.0",
        "name": "AI Romanian Subtitles",
        "description": "Automatic Romanian subtitle translation using Gemini AI",
        "resources": ["subtitles"],
        "types": [
            "movie",
            "series"
        ],
        "idPrefixes": [
            "tt"
        ],
        "catalogs": [],
        "behaviorHints": {
            "configurable": False
        }
    })


@app.route("/subtitles/<string:content_type>/<string:video_id>.json")
@app.route("/subtitles/<string:content_type>/<string:video_id>/<string:extra_params>.json")
def subtitles(content_type, video_id, extra_params=None):

    if content_type not in ("movie", "series"):
        return jsonify({"subtitles": []})

    parts = video_id.split(":")
    imdb_id = parts[0]

    if not imdb_id.startswith("tt"):
        return jsonify({"subtitles": []})

    # extra_params arată ca "videoSize=4315566895&videoHash=edf79028d3e79586"
    # — deocamdată doar îl logăm, nu-l folosim, dar e util pentru debugging
    if extra_params:
        request_logger.info(f"[subtitles] extra_params for {imdb_id}: {extra_params}")

    file_path = os.path.join(subs_dir, f"{imdb_id}.srt")

    if not os.path.exists(file_path):
        lock = get_lock_for(imdb_id)
        acquired = lock.acquire(blocking=False)

        if not acquired:
            lock.acquire()
            lock.release()
        else:
            try:
                get_subs_from_imdb(imdb_id)
            except Exception as e:
                print(e)
                lock.release()
                return jsonify({"subtitles": []})
            finally:
                clear_status(imdb_id)
                if lock.locked():
                    lock.release()

    if not os.path.exists(file_path):
        return jsonify({"subtitles": []})

    host = os.getenv("REDIRECT_HOST_NAME")
    port = os.getenv("PORT")

    base_url = f"https://{host}"
    if port:
        base_url += f":{port}"

    return jsonify({
        "subtitles": [
            {
                "id": imdb_id,
                "lang": "ron",
                "url": f"{base_url}/stremio/subtitles/{imdb_id}.srt"
            }
        ]
    })

@app.route("/stremio/subtitles/<string:imdb_id>.srt")
def stremio_subtitle(imdb_id):
    """Doar servește fișierul deja tradus — nu caută, nu traduce.
    Presupune că /subtitles/<type>/<id>.json a fost apelat înainte
    și a generat fișierul."""
    file_path = os.path.join(subs_dir, f"{imdb_id}.srt")

    print(f"[stremio_subtitle] request for {imdb_id} -> {file_path}")
    print(f"[stremio_subtitle] exists: {os.path.exists(file_path)}")

    if not os.path.exists(file_path):
        return ("", 404)

    response = send_file(
        file_path,
        mimetype="text/plain",
        as_attachment=False,
        conditional=False,   # important: evită 304 Not Modified silențios
        etag=False,
        last_modified=None
    )

    response.headers["Content-Disposition"] = f'inline; filename="{imdb_id}.srt"'
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length, Content-Type"
    response.headers["Cache-Control"] = "no-store"

    print(f"[stremio_subtitle] responding, status={response.status_code}, "
          f"content-length={response.headers.get('Content-Length')}")

    return response
    
    
@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"ok": False, "message": "Fișierul depășește dimensiunea maximă permisă (3MB)."}), 413

