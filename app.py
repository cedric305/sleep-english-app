from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory
from yt_dlp import YoutubeDL

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CLIPS_DIR = DATA_DIR / "clips"
LIBRARY_FILE = DATA_DIR / "library.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
if not LIBRARY_FILE.exists():
    LIBRARY_FILE.write_text("[]", encoding="utf-8")

library_lock = threading.Lock()


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


def load_library_items() -> list[dict]:
    with library_lock:
        try:
            items = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            if isinstance(items, list):
                return items
            return []
        except json.JSONDecodeError:
            return []


def save_library_items(items: list[dict]) -> None:
    with library_lock:
        LIBRARY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def serialize_library_item(item: dict) -> dict:
    return {
        "id": item["id"],
        "videoId": item["videoId"],
        "videoTitle": item.get("videoTitle", ""),
        "start": item["start"],
        "end": item["end"],
        "text": item["text"],
        "createdAt": item["createdAt"],
        "audioUrl": f"/api/library/audio/{item['id']}",
    }


def fmt_seconds(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    s = (ms // 1000) % 60
    m = (ms // 60000) % 60
    h = ms // 3600000
    mm = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{mm:03d}"


def create_audio_clip(video_id: str, start: float, end: float, clip_id: str) -> Path:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("找不到 ffmpeg，請先安裝 ffmpeg 才能儲存錄音檔")

    url = f"https://www.youtube.com/watch?v={video_id}"
    clip_start = max(0.0, start)
    clip_end = max(clip_start + 0.6, end)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        output_template = tmp_dir / "clip.%(ext)s"
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            "bestaudio/best",
            "--no-playlist",
            "--no-warnings",
            "--force-overwrites",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--download-sections",
            f"*{fmt_seconds(clip_start)}-{fmt_seconds(clip_end)}",
            "-o",
            str(output_template),
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"音檔截取失敗: {(proc.stderr or proc.stdout).strip()}")

        candidates = sorted(
            [p for p in tmp_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus"}],
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("音檔截取失敗: 找不到輸出檔案")

        dest = CLIPS_DIR / f"{clip_id}.mp3"
        shutil.copyfile(candidates[0], dest)
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
    try:
        create_audio_clip(video_id=video_id, start=start, end=end, clip_id=clip_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    item = {
        "id": clip_id,
        "videoId": video_id,
        "videoTitle": clean_caption_text(payload.get("videoTitle") or ""),
        "start": start,
        "end": end,
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    items = load_library_items()
    items.insert(0, item)
    save_library_items(items)
    return jsonify({"item": serialize_library_item(item)})


@app.route("/api/library/<clip_id>", methods=["DELETE"])
def delete_library_item(clip_id: str):
    items = load_library_items()
    exists = any(x.get("id") == clip_id for x in items)
    if not exists:
        return jsonify({"error": "找不到片段"}), 404

    items = [x for x in items if x.get("id") != clip_id]
    save_library_items(items)

    audio_path = CLIPS_DIR / f"{clip_id}.mp3"
    if audio_path.exists():
        audio_path.unlink()

    return jsonify({"ok": True})


@app.route("/api/library/clear", methods=["POST"])
def clear_library():
    save_library_items([])
    for path in CLIPS_DIR.glob("*.mp3"):
        path.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/library/audio/<clip_id>")
def serve_library_audio(clip_id: str):
    audio_path = CLIPS_DIR / f"{clip_id}.mp3"
    if not audio_path.exists():
        return jsonify({"error": "找不到音檔"}), 404
    return send_from_directory(CLIPS_DIR, audio_path.name, mimetype="audio/mpeg", as_attachment=False)


if __name__ == "__main__":
    app.run(debug=True)
