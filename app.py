from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory
from yt_dlp import YoutubeDL

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", str(BASE_DIR / "data"))).resolve()
CLIPS_DIR = DATA_DIR / "clips"
LIBRARY_FILE = DATA_DIR / "library.json"
WORDS_FILE = DATA_DIR / "words.json"
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "clips")

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
if not LIBRARY_FILE.exists():
    LIBRARY_FILE.write_text("[]", encoding="utf-8")
if not WORDS_FILE.exists():
    WORDS_FILE.write_text("[]", encoding="utf-8")

library_lock = threading.Lock()
words_lock = threading.Lock()


def use_supabase() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def supabase_rest_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"


def supabase_storage_url(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/{path.lstrip('/')}"


def supabase_headers(*, json_content: bool = True, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def supabase_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | list | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> requests.Response:
    response = requests.request(
        method,
        url,
        params=params,
        json=json_body,
        data=data,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def row_to_library_item(row: dict) -> dict:
    return {
        "id": row["id"],
        "videoId": row.get("video_id", ""),
        "videoTitle": row.get("video_title", ""),
        "start": float(row.get("start", 0.0)),
        "end": float(row.get("end", 0.0)),
        "text": row.get("text", ""),
        "createdAt": row.get("created_at", ""),
        "audioPath": row.get("audio_path", ""),
    }


def library_item_to_row(item: dict) -> dict:
    return {
        "id": item["id"],
        "video_id": item["videoId"],
        "video_title": item.get("videoTitle", ""),
        "start": item["start"],
        "end": item["end"],
        "text": item["text"],
        "created_at": item["createdAt"],
        "audio_path": item.get("audioPath", f"{item['id']}.mp3"),
    }


def row_to_word_item(row: dict) -> dict:
    return {
        "id": row["id"],
        "word": row.get("word", ""),
        "translation": row.get("translation", ""),
        "note": row.get("note", ""),
        "createdAt": row.get("created_at", ""),
        "updatedAt": row.get("updated_at", row.get("created_at", "")),
    }


def word_item_to_row(item: dict) -> dict:
    return {
        "id": item["id"],
        "word": item["word"],
        "word_normalized": normalize_word(item["word"]),
        "translation": item.get("translation", ""),
        "note": item.get("note", ""),
        "created_at": item["createdAt"],
        "updated_at": item.get("updatedAt", item["createdAt"]),
    }


def get_supabase_public_audio_url(audio_path: str) -> str:
    safe_path = "/".join(quote(part) for part in audio_path.split("/") if part)
    return supabase_storage_url(f"object/public/{SUPABASE_BUCKET}/{safe_path}")


def extract_video_id(youtube_url: str) -> str | None:
    if not youtube_url:
        return None

    parsed = urlparse(youtube_url)

    if parsed.netloc in {"youtu.be", "www.youtu.be"}:
        return parsed.path.lstrip("/") or None

    if parsed.netloc in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            query = parse_qs(parsed.query)
            return query.get("v", [None])[0]
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/")[-1].split("/")[0]
        if parsed.path.startswith("/embed/"):
            return parsed.path.split("/embed/")[-1].split("/")[0]

    if len(youtube_url) == 11 and "/" not in youtube_url and "?" not in youtube_url:
        return youtube_url

    return None


def clean_caption_text(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_word_text(text: str) -> str:
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_word(text: str) -> str:
    return clean_word_text(text).casefold()


def visual_text_length(text: str) -> int:
    length = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            length += 2
        else:
            length += 1
    return length


def split_text_fragments(text: str) -> list[tuple[str, int, int, bool]]:
    fragments: list[tuple[str, int, int, bool]] = []
    if not text:
        return fragments

    end_pattern = re.compile(r'[.!?。！？…]+["\'”’」』）)\]]*')

    def is_abbreviation_period(prefix: str) -> bool:
        tail = prefix.lower().strip()
        if re.search(r'(?:\b[a-z]\.){2,}$', tail):
            return True
        if re.search(r'\b(?:mr|mrs|ms|dr|prof|sr|jr|vs|etc|e\.g|i\.e|no)\.$', tail):
            return True
        return False

    def next_non_space_char(s: str, pos: int) -> str:
        i = pos
        while i < len(s) and s[i].isspace():
            i += 1
        while i < len(s) and s[i] in '"\'”’」』）)]':
            i += 1
            while i < len(s) and s[i].isspace():
                i += 1
        return s[i] if i < len(s) else ""

    cursor = 0
    for match in end_pattern.finditer(text):
        end = match.end()
        punct = match.group(0)
        if "." in punct and is_abbreviation_period(text[:end]):
            continue
        nxt = next_non_space_char(text, end)
        if nxt and nxt.isalpha() and nxt == nxt.lower() and "." in punct:
            continue
        part = text[cursor:end].strip()
        if part:
            fragments.append((part, cursor, end, True))
        cursor = end

    tail = text[cursor:].strip()
    if tail:
        fragments.append((tail, cursor, len(text), False))

    return fragments


def merge_short_sentences(rows: list[dict], min_visual_len: int = 56) -> list[dict]:
    if not rows:
        return []

    merged: list[dict] = []
    i = 0
    while i < len(rows):
        current = dict(rows[i])
        while i + 1 < len(rows) and visual_text_length(current["text"]) < min_visual_len:
            nxt = rows[i + 1]
            current["text"] = clean_caption_text(f'{current["text"]} {nxt["text"]}')
            current["end"] = max(current["end"], nxt["end"])
            current["duration"] = max(0.0, current["end"] - current["start"])
            i += 1
        merged.append(current)
        i += 1
    return merged


def chunk_to_timed_segments(chunk: dict) -> list[dict]:
    text = chunk["text"]
    start = chunk["start"]
    end = chunk["end"]
    duration = max(0.0, end - start)
    total_chars = max(1, len(text))

    segments = []
    for part, char_start, char_end, is_end in split_text_fragments(text):
        if duration == 0:
            seg_start = start
            seg_end = end
        else:
            seg_start = start + duration * (char_start / total_chars)
            seg_end = start + duration * (char_end / total_chars)
        segments.append(
            {
                "start": seg_start,
                "end": max(seg_start, seg_end),
                "text": part,
                "is_sentence_end": is_end,
            }
        )

    if not segments:
        segments.append({"start": start, "end": end, "text": text, "is_sentence_end": False})

    return segments


def merge_caption_chunks(chunks: list[dict]) -> list[dict]:
    if not chunks:
        return []

    merged: list[dict] = []
    current_start = None
    current_end = None
    current_text = ""

    def flush_current() -> None:
        nonlocal current_start, current_end, current_text
        if current_start is None or current_end is None:
            return
        text = clean_caption_text(current_text)
        if not text:
            current_start = None
            current_end = None
            current_text = ""
            return
        merged.append(
            {
                "start": current_start,
                "end": current_end,
                "duration": max(0.0, current_end - current_start),
                "text": text,
            }
        )
        current_start = None
        current_end = None
        current_text = ""

    last_seg_end = None
    for chunk in chunks:
        for seg in chunk_to_timed_segments(chunk):
            if current_start is None:
                current_start = seg["start"]
                current_end = seg["end"]
                current_text = seg["text"]
            else:
                gap = seg["start"] - (last_seg_end if last_seg_end is not None else current_end)
                if gap > 1.1:
                    flush_current()
                    current_start = seg["start"]
                    current_end = seg["end"]
                    current_text = seg["text"]
                else:
                    current_text = clean_caption_text(f"{current_text} {seg['text']}")
                    current_end = max(current_end, seg["end"])

            if seg["is_sentence_end"]:
                flush_current()

            last_seg_end = seg["end"]

    flush_current()

    for i in range(len(merged) - 1):
        if merged[i]["duration"] <= 0:
            next_start = merged[i + 1]["start"]
            merged[i]["end"] = max(merged[i]["start"], next_start)
            merged[i]["duration"] = max(0.0, merged[i]["end"] - merged[i]["start"])

    merged = merge_short_sentences(merged, min_visual_len=56)

    rows = []
    for idx, row in enumerate(merged):
        rows.append(
            {
                "id": idx,
                "start": row["start"],
                "duration": row["duration"],
                "end": row["end"],
                "text": row["text"],
            }
        )
    return rows


def choose_caption_track(info: dict) -> tuple[str, bool, str] | None:
    preferred_langs = ["en", "en-US", "en-GB", "zh-Hant", "zh-TW", "zh-Hans", "zh-CN", "zh"]

    def pick_from_tracks(tracks: dict[str, list[dict]]) -> tuple[str, str] | None:
        for lang in preferred_langs:
            items = tracks.get(lang)
            if items:
                url = choose_track_url(items)
                if url:
                    return lang, url

        for lang, items in tracks.items():
            if not items:
                continue
            url = choose_track_url(items)
            if url:
                return lang, url

        return None

    subtitles = info.get("subtitles") or {}
    chosen = pick_from_tracks(subtitles)
    if chosen:
        return chosen[0], False, chosen[1]

    automatic = info.get("automatic_captions") or {}
    chosen = pick_from_tracks(automatic)
    if chosen:
        return chosen[0], True, chosen[1]

    return None


def choose_track_url(items: list[dict]) -> str | None:
    for ext in ("json3", "srv3", "vtt", "ttml"):
        for entry in items:
            if entry.get("ext") == ext and entry.get("url"):
                return entry["url"]
    for entry in items:
        if entry.get("url"):
            return entry["url"]
    return None


def parse_json3_captions(payload: dict) -> list[dict]:
    chunks = []
    for event in payload.get("events", []):
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue

        segments = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segments)
        text = clean_caption_text(text)
        if not text:
            continue

        start = float(start_ms) / 1000.0
        duration = float(event.get("dDurationMs", 0)) / 1000.0

        chunks.append({"start": start, "duration": duration, "end": start + duration, "text": text})

    return merge_caption_chunks(chunks)


def load_json_items(file_path: Path, lock: threading.Lock) -> list[dict]:
    with lock:
        try:
            items = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(items, list):
                return items
            return []
        except json.JSONDecodeError:
            return []


def save_json_items(file_path: Path, lock: threading.Lock, items: list[dict]) -> None:
    with lock:
        file_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def load_library_items() -> list[dict]:
    if use_supabase():
        response = supabase_request(
            "GET",
            supabase_rest_url("library_items"),
            params={"select": "*", "order": "created_at.desc"},
            headers=supabase_headers(json_content=False),
        )
        rows = response.json()
        return [row_to_library_item(row) for row in rows]
    return load_json_items(LIBRARY_FILE, library_lock)


def save_library_items(items: list[dict]) -> None:
    if use_supabase():
        rows = [library_item_to_row(item) for item in items]
        supabase_request(
            "DELETE",
            supabase_rest_url("library_items"),
            params={"id": "neq.__never__"},
            headers=supabase_headers(json_content=False),
        )
        if rows:
            supabase_request(
                "POST",
                supabase_rest_url("library_items"),
                json_body=rows,
                headers=supabase_headers(prefer="return=minimal"),
            )
        return
    save_json_items(LIBRARY_FILE, library_lock, items)


def serialize_library_item(item: dict) -> dict:
    audio_url = (
        get_supabase_public_audio_url(item.get("audioPath", f"{item['id']}.mp3"))
        if use_supabase()
        else f"/api/library/audio/{item['id']}"
    )
    return {
        "id": item["id"],
        "videoId": item["videoId"],
        "videoTitle": item.get("videoTitle", ""),
        "start": item["start"],
        "end": item["end"],
        "text": item["text"],
        "createdAt": item["createdAt"],
        "audioUrl": audio_url,
    }


def load_word_items() -> list[dict]:
    if use_supabase():
        response = supabase_request(
            "GET",
            supabase_rest_url("word_items"),
            params={"select": "*", "order": "created_at.desc"},
            headers=supabase_headers(json_content=False),
        )
        rows = response.json()
        return [row_to_word_item(row) for row in rows]
    return load_json_items(WORDS_FILE, words_lock)


def save_word_items(items: list[dict]) -> None:
    if use_supabase():
        rows = [word_item_to_row(item) for item in items]
        supabase_request(
            "DELETE",
            supabase_rest_url("word_items"),
            params={"id": "neq.__never__"},
            headers=supabase_headers(json_content=False),
        )
        if rows:
            supabase_request(
                "POST",
                supabase_rest_url("word_items"),
                json_body=rows,
                headers=supabase_headers(prefer="return=minimal"),
            )
        return
    save_json_items(WORDS_FILE, words_lock, items)


def serialize_word_item(item: dict) -> dict:
    return {
        "id": item["id"],
        "word": item["word"],
        "translation": item.get("translation", ""),
        "note": item.get("note", ""),
        "createdAt": item["createdAt"],
        "updatedAt": item.get("updatedAt", item["createdAt"]),
    }


def translate_word_to_zh(word: str) -> str:
    response = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={
            "client": "gtx",
            "sl": "en",
            "tl": "zh-TW",
            "dt": "t",
            "q": word,
        },
        timeout=12,
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        return ""

    parts = []
    for row in payload[0]:
        if isinstance(row, list) and row and isinstance(row[0], str):
            parts.append(row[0])
    return clean_word_text("".join(parts))


def fmt_seconds(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    s = (ms // 1000) % 60
    m = (ms // 60000) % 60
    h = ms // 3600000
    mm = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{mm:03d}"


def upload_clip_to_supabase(audio_path: Path, dest_name: str) -> str:
    response = supabase_request(
        "POST",
        supabase_storage_url(f"object/{SUPABASE_BUCKET}/{quote(dest_name)}"),
        data=audio_path.read_bytes(),
        headers={
            **supabase_headers(json_content=False),
            "Content-Type": "audio/mpeg",
            "x-upsert": "true",
        },
        timeout=60,
    )
    payload = response.json()
    stored_path = payload.get("Key") or payload.get("path") or dest_name
    return stored_path


def delete_supabase_clips(paths: list[str]) -> None:
    clip_paths = [path for path in paths if path]
    if not clip_paths:
        return
    supabase_request(
        "DELETE",
        supabase_storage_url(f"object/{SUPABASE_BUCKET}"),
        json_body={"prefixes": clip_paths},
        headers=supabase_headers(),
        timeout=60,
    )


def create_audio_clip(video_id: str, start: float, end: float, clip_id: str) -> Path:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("找不到 ffmpeg，請先安裝 ffmpeg 才能儲存錄音檔")

    url = f"https://www.youtube.com/watch?v={video_id}"
    clip_start = max(0.0, start)
    clip_end = max(clip_start + 0.6, end)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        output_template = tmp_dir / "source.%(ext)s"
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_template),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            requested_downloads = info.get("requested_downloads") or []

            candidates = []
            for item in requested_downloads:
                filepath = item.get("filepath")
                if filepath:
                    path = Path(filepath)
                    if path.exists():
                        candidates.append(path)

            if not candidates:
                prepared = Path(ydl.prepare_filename(info))
                if prepared.exists():
                    candidates.append(prepared)

            if not candidates:
                candidates = sorted(
                    [p for p in tmp_dir.iterdir() if p.is_file()],
                    key=lambda p: p.stat().st_size,
                    reverse=True,
                )

        if not candidates:
            raise RuntimeError("音檔下載失敗: 找不到來源音訊檔")

        source_audio = candidates[0]

        dest = CLIPS_DIR / f"{clip_id}.mp3"
        ffmpeg_cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            str(source_audio),
            "-ss",
            f"{clip_start:.3f}",
            "-to",
            f"{clip_end:.3f}",
            "-vn",
            "-map",
            "a:0",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "2",
            str(dest),
        ]
        proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"音檔裁切失敗: {(proc.stderr or proc.stdout).strip()}")

        if not dest.exists():
            raise RuntimeError("音檔裁切失敗: 找不到輸出檔案")

        return dest


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/transcript")
def get_transcript():
    url = request.args.get("url", "").strip()
    video_id = extract_video_id(url)

    if not video_id:
        return jsonify({"error": "無法解析 YouTube 影片網址"}), 400

    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

        chosen = choose_caption_track(info)
        if not chosen:
            return jsonify({"error": "找不到可用字幕（包含自動字幕）"}), 404

        lang, is_auto, subtitle_url = chosen
        response = requests.get(subtitle_url, timeout=20)
        response.raise_for_status()

        captions = parse_json3_captions(response.json())
        if not captions:
            return jsonify({"error": "字幕格式不支援或內容為空"}), 404

        return jsonify(
            {
                "videoId": video_id,
                "videoTitle": info.get("title") or "",
                "captions": captions,
                "language": lang,
                "isAutoGenerated": is_auto,
            }
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"下載字幕失敗: {exc}"}), 500
    except Exception as exc:
        return jsonify({"error": f"取得字幕失敗: {exc}"}), 500


@app.route("/api/library", methods=["GET"])
def list_library():
    items = load_library_items()
    serialized = [serialize_library_item(x) for x in items]
    return jsonify({"items": serialized})


@app.route("/api/library", methods=["POST"])
def create_library_item():
    payload = request.get_json(silent=True) or {}

    video_id = (payload.get("videoId") or "").strip()
    text = clean_caption_text(payload.get("text") or "")

    try:
        start = float(payload.get("start"))
        end = float(payload.get("end"))
    except (TypeError, ValueError):
        return jsonify({"error": "start/end 格式錯誤"}), 400

    if not video_id or len(video_id) != 11:
        return jsonify({"error": "videoId 無效"}), 400
    if not text:
        return jsonify({"error": "字幕內容不可為空"}), 400

    start = max(0.0, start)
    end = max(start + 0.6, end)

    clip_id = uuid.uuid4().hex
    clip_path = None
    try:
        clip_path = create_audio_clip(video_id=video_id, start=start, end=end, clip_id=clip_id)
        audio_path = f"{clip_id}.mp3"
        if use_supabase():
            audio_path = upload_clip_to_supabase(clip_path, f"{clip_id}.mp3")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if use_supabase() and clip_path and clip_path.exists():
            clip_path.unlink(missing_ok=True)

    item = {
        "id": clip_id,
        "videoId": video_id,
        "videoTitle": clean_caption_text(payload.get("videoTitle") or ""),
        "start": start,
        "end": end,
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "audioPath": audio_path,
    }

    items = load_library_items()
    items.insert(0, item)
    save_library_items(items)
    return jsonify({"item": serialize_library_item(item)})


@app.route("/api/library/<clip_id>", methods=["DELETE"])
def delete_library_item(clip_id: str):
    items = load_library_items()
    target = next((x for x in items if x.get("id") == clip_id), None)
    if not target:
        return jsonify({"error": "找不到片段"}), 404

    items = [x for x in items if x.get("id") != clip_id]
    save_library_items(items)

    if use_supabase():
        delete_supabase_clips([target.get("audioPath", f"{clip_id}.mp3")])
    else:
        audio_path = CLIPS_DIR / f"{clip_id}.mp3"
        if audio_path.exists():
            audio_path.unlink()

    return jsonify({"ok": True})


@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    items = load_library_items()
    save_library_items([])
    if use_supabase():
        delete_supabase_clips([item.get("audioPath", f"{item['id']}.mp3") for item in items])
    else:
        for path in CLIPS_DIR.glob("*.mp3"):
            path.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/library/audio/<clip_id>")
def serve_library_audio(clip_id: str):
    if use_supabase():
        items = load_library_items()
        target = next((x for x in items if x.get("id") == clip_id), None)
        if not target:
            return jsonify({"error": "找不到音檔"}), 404
        return redirect(get_supabase_public_audio_url(target.get("audioPath", f"{clip_id}.mp3")))

    audio_path = CLIPS_DIR / f"{clip_id}.mp3"
    if not audio_path.exists():
        return jsonify({"error": "找不到音檔"}), 404
    return send_from_directory(CLIPS_DIR, audio_path.name, mimetype="audio/mpeg", as_attachment=False)


@app.route("/api/words", methods=["GET"])
def list_words():
    items = load_word_items()
    serialized = [serialize_word_item(x) for x in items]
    return jsonify({"items": serialized})


@app.route("/api/words/translate")
def translate_word():
    word = clean_word_text(request.args.get("word", ""))
    if not word:
        return jsonify({"error": "請先輸入單字"}), 400

    try:
        translation = translate_word_to_zh(word)
    except requests.RequestException as exc:
        return jsonify({"error": f"查詢翻譯失敗: {exc}"}), 502
    except ValueError:
        return jsonify({"error": "翻譯服務回傳格式錯誤"}), 502

    if not translation:
        return jsonify({"error": "暫時找不到對應翻譯，請手動輸入"}), 404

    return jsonify({"word": word, "translation": translation})


@app.route("/api/words", methods=["POST"])
def create_word():
    payload = request.get_json(silent=True) or {}
    word = clean_word_text(payload.get("word") or "")
    translation = clean_word_text(payload.get("translation") or "")
    note = clean_word_text(payload.get("note") or "")

    if not word:
        return jsonify({"error": "單字不可為空"}), 400
    if not translation:
        return jsonify({"error": "中文翻譯不可為空"}), 400

    items = load_word_items()
    normalized = normalize_word(word)
    if any(normalize_word(x.get("word", "")) == normalized for x in items):
        return jsonify({"error": "這個單字已經在生字庫裡"}), 409

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": uuid.uuid4().hex,
        "word": word,
        "translation": translation,
        "note": note,
        "createdAt": now,
        "updatedAt": now,
    }
    items.insert(0, item)
    save_word_items(items)
    return jsonify({"item": serialize_word_item(item)})


@app.route("/api/words/<word_id>", methods=["PUT"])
def update_word(word_id: str):
    payload = request.get_json(silent=True) or {}
    word = clean_word_text(payload.get("word") or "")
    translation = clean_word_text(payload.get("translation") or "")
    note = clean_word_text(payload.get("note") or "")

    if not word:
        return jsonify({"error": "單字不可為空"}), 400
    if not translation:
        return jsonify({"error": "中文翻譯不可為空"}), 400

    items = load_word_items()
    target = next((x for x in items if x.get("id") == word_id), None)
    if not target:
        return jsonify({"error": "找不到單字"}), 404

    normalized = normalize_word(word)
    if any(x.get("id") != word_id and normalize_word(x.get("word", "")) == normalized for x in items):
        return jsonify({"error": "這個單字已經在生字庫裡"}), 409

    target["word"] = word
    target["translation"] = translation
    target["note"] = note
    target["updatedAt"] = datetime.now(timezone.utc).isoformat()

    save_word_items(items)
    return jsonify({"item": serialize_word_item(target)})


@app.route("/api/words/<word_id>", methods=["DELETE"])
def delete_word(word_id: str):
    items = load_word_items()
    exists = any(x.get("id") == word_id for x in items)
    if not exists:
        return jsonify({"error": "找不到單字"}), 404

    items = [x for x in items if x.get("id") != word_id]
    save_word_items(items)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=port, debug=debug)
