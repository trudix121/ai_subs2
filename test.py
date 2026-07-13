import os
import requests
from dotenv import load_dotenv
import zipfile
from google import genai
from google.genai import types
import re
import time
load_dotenv()

cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
subs_dir = os.path.join(os.path.dirname(__file__), 'subs')

MAX_INPUT_TOKENS = 5000
MAX_RETRIES = 5
RETRY_DELAY = 5  # secunde

CHARS_PER_TOKEN = 3.2
SAFETY_MARGIN = 0.85
EFFECTIVE_MAX_TOKENS = int(MAX_INPUT_TOKENS * SAFETY_MARGIN)

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


client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options={'timeout': 60 * 60 * 1000})
config = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    temperature=0
)

def translate_subs(file_content, imdb_id):
    print(f"GENERATING SUBS FOR {imdb_id}")

    chunks = split_by_tokens(file_content)
    translated = []


    for i, chunk in enumerate(chunks, start=1):
        print(f"Chunk {i}/{len(chunks)}")

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


                if attempt == MAX_RETRIES:
                    raise

                time.sleep(RETRY_DELAY * attempt)



    result = "\n\n".join(translated)

    with open(
        os.path.join(subs_dir, f"{imdb_id}.srt"),
        "w",
        encoding="utf-8"
    ) as f:
        f.write(result)

    return result



def get_subs_ro(imdbID):
    base_url = f"https://api.subs.ro/v1.0/search/imdbid/{imdbID}"

    response = requests.get(
    base_url,
    headers={
        "X-Subs-Api-Key": os.getenv("SUBS_RO_API_KEY")
    },
    params={
        "language": "en"
    }
)
    response.raise_for_status()

    data = response.json()

    if data["count"] == 0:
        print("No subtitles found.")
        return

    # Preferă subtitrarea în română
    en_sub = next(
        (item for item in data["items"] if item["language"] == "en"),
        data["items"][0]
    )

    download_url = en_sub["downloadLink"]

    subtitle = requests.get(
        download_url,
        headers={
            "X-Subs-Api-Key": os.getenv("SUBS_RO_API_KEY")
        }
    ) 
    subtitle.raise_for_status()

    zip_path = os.path.join(cache_dir, f"{imdbID}.zip")

    try:
        # Salvează arhiva
        with open(zip_path, "wb") as f:
            f.write(subtitle.content)

        # Extrage arhiva
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(cache_dir)

            srt_path = next(
                (name for name in zip_ref.namelist() if name.lower().endswith(".srt")),
                None
            )

        if srt_path is None:
            print("No .srt file found in archive.")
            return

        full_path = os.path.join(cache_dir, srt_path)

        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            file_content = f.read()

        translate_subs(file_content, imdbID)

        print("Subtitle downloaded and processed.")

    finally:
        # Șterge ZIP-ul
        if os.path.exists(zip_path):
            os.remove(zip_path)

if __name__ == "__main__":
    get_subs_ro("tt3620860")