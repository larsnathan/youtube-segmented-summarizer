#!/usr/bin/env python3
"""
chunk_worker.py  (schema v2: downloads.summary path)

Silences imageio-ffmpeg chatter and produces:
  • timestamped chunk summaries
  • overall video summary
  • {uuid}-summary.txt in /filestore
  • DB status -> summarized, summary -> path
"""
from __future__ import annotations
import logging, os, time, sys
from pathlib import Path
from typing import List, Tuple

# ── mute imageio-ffmpeg BEFORE it gets imported via MoviePy ──────────────
os.environ["IMAGEIO_FFMPEG_LOGLEVEL"] = "error"  # ‘quiet’ / ‘warning’ / ‘error’
os.environ["IMAGEIO_FFMPEG_VERBOSE"] = "0"  # legacy flag for older versions
# also hush MoviePy’s own logger
logging.getLogger("moviepy").setLevel(logging.ERROR)

import cv2, numpy as np, webvtt
from moviepy.video.io.VideoFileClip import VideoFileClip
from ollama import Client
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from database import ENGINE, downloads, init_db

init_db()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ─────────────────────────── configuration ────────────────────────────────
CHUNK_SEC = 60
SAMPLED_FRAMES = 20
JPEG_QUALITY = 90
OLLAMA_MODEL = "gemma3:27b"
os.environ["OLLAMA_HOST"] = "ollama:11434"
SUMMARY_DIR = Path("/filestore")  # where txt files go

ollama_client = Client()
ollama_client.pull(OLLAMA_MODEL)


# ─────────────────────────── helpers ──────────────────────────────────────
def ts(total: float) -> str:
    h, rem = divmod(int(total), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((total - int(total)) * 1_000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def bucket_captions(vtt_path: Path, n_chunks: int) -> List[str]:
    vtt = webvtt.read(str(vtt_path))
    buckets: List[List[webvtt.Caption]] = [[] for _ in range(n_chunks)]
    for cue in vtt:
        idx = int(cue.start_in_seconds // CHUNK_SEC)
        if idx < n_chunks:
            buckets[idx].append(cue)
    out: List[str] = []
    for i, cues in enumerate(buckets):
        if not cues:
            out.append("")
            continue
        start_base = i * CHUNK_SEC
        lines = ["WEBVTT", ""]
        for n, c in enumerate(cues, 1):
            ls = c.start_in_seconds - start_base
            le = c.end_in_seconds - start_base
            lines += [
                str(n),
                f"{ts(ls)} --> {ts(le)}",
                c.raw_text,
                "",
            ]
        out.append("\n".join(lines))
    return out


def cut_video(mp4: Path) -> List[VideoFileClip]:
    clip = VideoFileClip(str(mp4), audio=False)
    parts, t = [], 0.0
    while t < clip.duration:
        parts.append(clip.subclipped(t, min(t + CHUNK_SEC, clip.duration)))
        t += CHUNK_SEC
    return parts


def sample_frames(clip: VideoFileClip, k=SAMPLED_FRAMES) -> List[bytes]:
    if k <= 0 or clip.duration == 0:
        return []
    pts = np.linspace(0, clip.duration, k + 2, endpoint=False)[1:]
    out = []
    for t in pts:
        frame = clip.get_frame(t)  # RGB ndarray
        ok, buf = cv2.imencode(
            ".jpg",
            cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
        )
        if ok:
            out.append(buf.tobytes())
    return out


# ────────────────────────── per-row processing ────────────────────────────
def process_row(row) -> None:
    vid, vtt = Path(row.video), Path(row.caption)
    logging.info("🎬 summarizing %s", vid.name)

    vids = cut_video(vid)
    caps = bucket_captions(vtt, len(vids))
    chunks = list(zip(vids, caps))

    chunk_summaries = []
    for idx, (clip, cap_vtt) in enumerate(chunks):  # idx = 0,1,2…
        img_bytes = sample_frames(clip)

        prompt = (
            f"You see still frames from a ~{CHUNK_SEC}-second video clip and its captions.\n"
            "In ONE concise sentence, describe what is happening visually. "
            "Use captions only if helpful.\n\nCaptions:\n" + cap_vtt
        )

        try:
            resp = ollama_client.generate(
                model=OLLAMA_MODEL, prompt=prompt, images=img_bytes
            )
            text = resp.response.strip()
        except Exception as e:
            logging.exception("chunk %d failed: %s", idx, e)
            text = "[summary unavailable]"

        start_abs = idx * CHUNK_SEC  # 0-based now
        end_abs = start_abs + clip.duration  # precise end
        chunk_summaries.append(f"[{ts(start_abs)}–{ts(end_abs)}] {text}")
        logging.info("📝 chunk %d/%d summarized", idx + 1, len(chunks))

    # ─ overall summary ────────────────────────────────────────────────────
    overall_prompt = (
        "You are given sequential one-sentence summaries of a full video. "
        "Write a short paragraph (≤4 sentences) that captures the overall "
        "content and main events.\n\nSummaries:\n"
        + "\n".join(chunk_summaries)
        + "\n I am also giving you the captions of the video, only use them if necessary:\n"
        + "\n".join(caps)
    )
    try:
        overall = ollama_client.generate(
            model=OLLAMA_MODEL, prompt=overall_prompt
        ).response.strip()
    except Exception as e:
        logging.exception("overall summary failed: %s", e)
        overall = "[overall summary unavailable]"

    # ─ save to file ───────────────────────────────────────────────────────
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = SUMMARY_DIR / f"{row.uuid}-summary.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chunk_summaries))
        f.write("\n\n== OVERALL SUMMARY ==\n")
        f.write(overall + "\n")
    logging.info("📄 wrote %s", txt_path)

    # ─ update DB ──────────────────────────────────────────────────────────
    with Session(ENGINE) as ses:
        ses.execute(
            update(downloads)
            .where(downloads.c.id == row.id)
            .values(status="summarized", summary=str(txt_path))
        )
        ses.commit()
    logging.info("✅ row %s marked summarized", row.id)


# ────────────────────────── watcher loop ───────────────────────────────────
def main_loop(interval: int = 10):
    logging.info("👀 worker polling every %d s", interval)
    while True:
        with Session(ENGINE) as s:
            rows = s.execute(
                select(downloads).where(downloads.c.status == "downloaded")
            ).all()
        if not rows:
            time.sleep(interval)
            continue
        for row in rows:
            try:
                process_row(row)
            except Exception as e:
                logging.exception("row %s failed: %s", getattr(row, "id", "?"), e)
        time.sleep(1)


if __name__ == "__main__":
    main_loop()
