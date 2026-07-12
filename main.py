from google import genai
from dotenv import load_dotenv
import os
import requests
from flask import Flask, request, jsonify, render_template, send_file
import asyncio
from google.genai import types
import re
import time
load_dotenv()


app = Flask(__name__)
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
os.makedirs(subs_dir, exist_ok=True)
os.makedirs(cache_dir, exist_ok=True)


def split_by_tokens(content):
    try:
        if not content:
            raise ValueError("Subtitle content is empty.")

        # Normalizează newline-urile
        content = content.replace("\r\n", "\n").strip()

        # Împarte în blocuri SRT
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

            try:
                block_tokens = client.models.count_tokens(
                    model="gemini-3.5-flash",
                    contents=block
                ).total_tokens
            except Exception as e:
                print(f"Error counting tokens: {e}")
                raise

            # Dacă un singur bloc este prea mare
            if block_tokens > MAX_INPUT_TOKENS:
                if current_blocks:
                    chunks.append("\n\n".join(current_blocks))
                    current_blocks = []
                    current_tokens = 0

                chunks.append(block)
                continue

            if current_tokens + block_tokens > MAX_INPUT_TOKENS:
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

                print(f"Chunk {i}: {tokens} tokens")

            except Exception as e:
                print(f"Failed to count tokens for chunk {i}: {e}")

        return chunks

    except Exception as e:
        print(f"split_by_tokens failed: {e}")
        raise


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

                # backoff: 5s, 10s, 15s...
                time.sleep(RETRY_DELAY * attempt)

    result = "\n\n".join(translated)

    with open(
        os.path.join(subs_dir, f"{imdb_id}.srt"),
        "w",
        encoding="utf-8"
    ) as f:
        f.write(result)

    return result





def get_subs(imdb_id):
    print("========================================")
    print(f"Searching subtitles for {imdb_id}")



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
    finally:
        if os.path.exists(cache_file):
           os.remove(cache_file)
            



@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@app.route('/api/titles/<string:title_id>')
def get_title(title_id):
    file_path = os.path.join(
        os.path.dirname(__file__),
        "subs",
        f"{title_id}.srt"
    )

    if not os.path.exists(file_path):
        try:
            get_subs(title_id)
        except Exception:
            return jsonify({
                "ok": False,
                "message": "Title not found"
            }), 404

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

   
   
if __name__ == '__main__':
    app.run(debug=True, port=os.getenv('PORT'))
    