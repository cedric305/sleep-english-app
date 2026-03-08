from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, jsonify, render_template, request
from yt_dlp import YoutubeDL

app = Flask(__name__)


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
    # CJK characters usually occupy wider width; use weighted length for UI heuristics.
    length = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            length += 2
        else:
            length += 1
    return length


def is_sentence_end(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    # Include paired punctuation to better detect sentence boundaries in mixed languages.
    return bool(re.search(r'[.!?。！？…]["\'」』）)\]]*$', stripped))


def split_text_fragments(text: str) -> list[tuple[str, int, int, bool]]:
    fragments: list[tuple[str, int, int, bool]] = []
    if not text:
        return fragments

    # Keep trailing quote/bracket with sentence-ending punctuation.
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
        segments.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "is_sentence_end": False,
            }
        )

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
                "start": row["start"],  # sentence first word timestamp
                "duration": row["duration"],
                "end": row["end"],
                "text": row["text"],
            }
        )
    return rows


def choose_caption_track(info: dict) -> tuple[str, bool, str] | None:
    preferred_langs = [
        "en",
        "en-US",
        "en-GB",
        "zh-Hant",
        "zh-TW",
        "zh-Hans",
        "zh-CN",
        "zh",
    ]

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

        chunks.append(
            {
                "start": start,
                "duration": duration,
                "end": start + duration,
                "text": text,
            }
        )

    return merge_caption_chunks(chunks)


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
                "captions": captions,
                "language": lang,
                "isAutoGenerated": is_auto,
            }
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"下載字幕失敗: {exc}"}), 500
    except Exception as exc:
        return jsonify({"error": f"取得字幕失敗: {exc}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
