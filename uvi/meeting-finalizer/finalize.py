"""After-meeting pass: full-recording Hebrew transcript → Claude summary → Slack (+ Vexa).

Vexa's live transcript is built from short per-speaker windows. Once a meeting completes, this job
re-transcribes the whole recording — through an OpenAI-compatible transcription API (default:
OpenRouter's microsoft/mai-transcribe-2, sent as 10-minute mono MP3 pieces), falling back to a
local ivrit.ai Whisper model (faster-whisper, CPU) if the API fails — attributes speakers from Vexa's live segments by time overlap, asks the
`claude` CLI (the host's Claude Code login) for a Hebrew summary, and posts it to a Slack channel
with the full transcript attached in the thread.

Polls Vexa every POLL_S seconds; each completed meeting is processed once (STATE_FILE).

Env:
  VEXA_API_BASE        e.g. http://127.0.0.1:18056
  VEXA_API_KEY         token of the user who owns the meetings
  VEXA_UI_URL          e.g. https://meetings.example.com (link in the Slack post)
  SLACK_BOT_TOKEN      bot token with chat:write + files:write
  SLACK_CHANNEL        channel id
  FINAL_STT_URL        default https://openrouter.ai/api/v1/audio/transcriptions
  FINAL_STT_MODEL      default microsoft/mai-transcribe-2 (empty → local ivrit.ai only)
  FINAL_STT_KEY_FILE   file holding the API key for FINAL_STT_URL
  FINAL_STT_CHUNK_S    default 600
  WHISPER_MODEL        fallback, default ivrit-ai/whisper-large-v3-turbo-ct2
  WHISPER_THREADS      default 2 (leave room for live bots)
  CLAUDE_BIN           default claude
  PROCESS_SINCE        ISO time; meetings that ended before it are skipped (default: first start)
  STATE_FILE           default ~/.local/state/uvi-meeting-finalizer.json
  POLL_S               default 60
  VEXA_PG_CONTAINER    default vexa-v012-postgres-1   (final transcript replaces the live one)
  VEXA_REDIS_CONTAINER default vexa-v012-redis-1
  BACKUP_DIR           default ~/.local/state/uvi-meeting-finalizer-backups (live transcript kept here)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import wave
from datetime import datetime, timezone

import numpy as np
import requests

log = logging.getLogger("finalizer")

VEXA = os.environ.get("VEXA_API_BASE", "http://127.0.0.1:18056").rstrip("/")
VH = {"X-API-Key": os.environ["VEXA_API_KEY"]}
UI = os.environ.get("VEXA_UI_URL", "").rstrip("/")
SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]
MODEL = os.environ.get("WHISPER_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")
STT_URL = os.environ.get("FINAL_STT_URL", "https://openrouter.ai/api/v1/audio/transcriptions")
STT_MODEL = os.environ.get("FINAL_STT_MODEL", "microsoft/mai-transcribe-2")
STT_KEY_FILE = os.path.expanduser(os.environ.get("FINAL_STT_KEY_FILE", ""))
CHUNK_S = int(os.environ.get("FINAL_STT_CHUNK_S", "600"))
THREADS = int(os.environ.get("WHISPER_THREADS", "2"))
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
STATE_FILE = os.path.expanduser(os.environ.get("STATE_FILE", "~/.local/state/uvi-meeting-finalizer.json"))
POLL_S = float(os.environ.get("POLL_S", "60"))
PG = os.environ.get("VEXA_PG_CONTAINER", "vexa-v012-postgres-1")
REDIS = os.environ.get("VEXA_REDIS_CONTAINER", "vexa-v012-redis-1")
BACKUP_DIR = os.path.expanduser(os.environ.get("BACKUP_DIR", "~/.local/state/uvi-meeting-finalizer-backups"))

SUMMARY_PROMPT = """You are writing the meeting summary for an Israeli startup's internal Slack channel.
The text on stdin is an automatic Hebrew transcript of a recorded meeting (it has transcription errors;
infer the intended meaning, never invent facts that aren't there). Speaker labels come from Google Meet;
in-room participants may share one label.

Write the summary in Hebrew, in Slack mrkdwn (*bold*, bullet lines starting with "• "), with these sections,
omitting any that would be empty:
*תקציר* — 2–4 sentences.
*החלטות*
*משימות* — "• <מי>: <מה>" (owner if stated, otherwise "לא הוגדר").
*שאלות פתוחות*
Keep product/tech names in their usual English spelling (e.g. Uvi, Naboo, Shraga, graf, CSA, SQLite).
Output only the summary, no preamble. If the transcript is too short or garbled to summarize, say so in one line."""

_model = None


def whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS)
    return _model


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def vexa(path: str, **kw) -> requests.Response:
    r = requests.get(f"{VEXA}{path}", headers=VH, timeout=60, **kw)
    r.raise_for_status()
    return r


def recording_for(meeting_id: int) -> dict | None:
    for rec in vexa("/recordings", params={"limit": 100}).json().get("recordings", []):
        if rec.get("meeting_id") == meeting_id and rec.get("status") == "completed":
            audio = [m for m in rec.get("media_files", []) if m.get("type") == "audio"]
            if audio:
                return {"id": rec["id"], "media": audio[0]["id"], "created_at": rec.get("created_at")}
    return None


def to_wav(src: str, dst: str) -> np.ndarray:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-ac", "1", "-ar", "16000", dst], check=True)
    with wave.open(dst) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def speaker_at(live: list[dict], t0: float, t1: float) -> str:
    """Speaker of the live segment overlapping [t0, t1] most; else the nearest one within 5 s."""
    best, best_ov = "", 0.0
    near, near_gap = "", 5.0
    for s in live:
        ov = min(t1, s["end"]) - max(t0, s["start"])
        if ov > best_ov:
            best, best_ov = s.get("speaker") or "", ov
        elif ov <= 0 and -ov < near_gap and s.get("speaker"):
            near, near_gap = s["speaker"], -ov
    return best or near


def api_segments(src: str, workdir: str) -> list[tuple[float, float, str]]:
    """Full recording → (start, end, text) via the transcription API, in CHUNK_S mono MP3 pieces
    (MAI-Transcribe 2 rejects Opus re-encoded from the recording's WebM; MP3 is accepted)."""
    with open(STT_KEY_FILE) as f:
        key = f.read().strip()
    parts = os.path.join(workdir, "part%03d.mp3")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-ac", "1", "-ar", "16000",
                    "-c:a", "libmp3lame", "-b:a", "48k", "-f", "segment", "-segment_time", str(CHUNK_S), parts], check=True)
    out: list[tuple[float, float, str]] = []
    for i, name in enumerate(sorted(n for n in os.listdir(workdir) if n.startswith("part") and n.endswith(".mp3"))):
        for attempt in range(5):
            with open(os.path.join(workdir, name), "rb") as f:
                r = requests.post(STT_URL, headers={"Authorization": f"Bearer {key}"}, timeout=600,
                                  data={"model": STT_MODEL, "language": "he", "response_format": "verbose_json"},
                                  files={"file": (name, f, "audio/mpeg")})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1) ** 2)
                continue
            r.raise_for_status()
            break
        body = r.json()
        off = i * CHUNK_S
        segs = body.get("segments") or []
        if segs:
            out += [(off + float(s["start"]), off + float(s["end"]), s.get("text", "")) for s in segs]
        elif body.get("text"):
            out.append((off, off + CHUNK_S, body["text"]))
    return out


def local_segments(src: str, workdir: str) -> list[tuple[float, float, str]]:
    audio = to_wav(src, os.path.join(workdir, "audio.wav"))
    segs, _ = whisper().transcribe(audio, language="he", vad_filter=True, beam_size=5, condition_on_previous_text=False)
    return [(s.start, s.end, s.text) for s in segs]


def transcribe(meeting: dict, rec: dict, workdir: str) -> tuple[list[dict], str]:
    src = os.path.join(workdir, "audio.webm")
    with requests.get(f"{VEXA}/recordings/{rec['id']}/media/{rec['media']}/download", headers=VH, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(src, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    engine = STT_MODEL
    segs = None
    if STT_MODEL and STT_KEY_FILE:
        try:
            segs = api_segments(src, workdir)
        except Exception as e:
            log.warning("meeting %s: %s failed (%s) — falling back to %s", meeting["id"], STT_MODEL, e, MODEL)
    if segs is None:
        engine = MODEL
        segs = local_segments(src, workdir)

    live = vexa(f"/transcripts/google_meet/{meeting['native_meeting_id']}").json().get("segments", [])
    rec_start = (parse(rec.get("created_at")) or parse(meeting.get("start_time"))).timestamp()
    out: list[dict] = []
    for start, end, raw in segs:
        text = (raw or "").strip()
        if not text:
            continue
        spk = speaker_at(live, rec_start + start, rec_start + end) or "?"
        if out and out[-1]["speaker"] == spk and start - out[-1]["end"] < 2.0:
            out[-1]["text"] += " " + text
            out[-1]["end"] = end
        else:
            out.append({"speaker": spk, "start": start, "end": end, "text": text})
    for line in out:
        line["abs_start"], line["abs_end"] = rec_start + line["start"], rec_start + line["end"]
    return out, engine


def as_text(lines: list[dict]) -> str:
    return "\n".join(f"[{int(l['start'] // 60):02d}:{int(l['start'] % 60):02d}] {l['speaker']}: {l['text']}" for l in lines)


def summarize(transcript: str) -> str:
    r = subprocess.run([CLAUDE, "-p", SUMMARY_PROMPT, "--output-format", "text"], input=transcript,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def slack(method: str, **payload) -> dict:
    r = requests.post(f"https://slack.com/api/{method}", headers={"Authorization": f"Bearer {SLACK_TOKEN}"}, json=payload, timeout=30)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"slack {method}: {d.get('error')}")
    return d


def psql(sql: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", PG, "psql", "-U", "postgres", "-d", "vexa", "-v", "ON_ERROR_STOP=1", "-At"],
                       input=sql, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"psql: {r.stderr.strip()[:300]}")
    return r.stdout


def q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def replace_vexa_transcript(meeting_id: int, lines: list[dict]) -> None:
    """Swap the meeting's live segments for the final ones (live rows backed up to BACKUP_DIR first)."""
    mid = int(meeting_id)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"meeting-{mid}-live.json")
    if not os.path.exists(backup_path):  # first write only — a rerun must not overwrite the live copy with final rows
        backup = psql(f"SELECT coalesce(json_agg(t), '[]') FROM transcriptions t WHERE meeting_id = {mid};")
        with open(backup_path, "w") as f:
            f.write(backup)
    values = ",\n".join(
        f"({mid}, {l['abs_start']:.3f}, {l['abs_end']:.3f}, {q(l['text'])}, {q(l['speaker']) if l['speaker'] != '?' else 'NULL'}, 'he', 'final-{i:05d}', now())"
        for i, l in enumerate(lines))
    psql(f"BEGIN;\nDELETE FROM transcriptions WHERE meeting_id = {mid};\n"
         f"INSERT INTO transcriptions (meeting_id, start_time, end_time, text, speaker, language, segment_id, created_at) VALUES\n{values};\nCOMMIT;\n")
    subprocess.run(["docker", "exec", REDIS, "redis-cli", "DEL", f"meeting:{mid}:segments"], capture_output=True, timeout=30)


def post(meeting: dict, title: str, summary: str, transcript: str, minutes: int, engine: str) -> None:
    speakers = sorted({l.split("] ", 1)[1].split(":", 1)[0] for l in transcript.splitlines() if "] " in l} - {"?"})
    started = parse(meeting.get("start_time"))
    when = started.astimezone().strftime("%d/%m/%Y %H:%M") if started else ""
    link = f"<{UI}/meetings/{meeting['id']}|פתיחה ב-Vexa>" if UI else ""
    header = f":memo: *{title}* · {when} · {minutes} דק׳" + (f" · {', '.join(speakers)}" if speakers else "")
    body = f"{header}\n\n{summary}\n\n{link}".strip()
    # verbatim: Slack must not auto-link bare domains in Hebrew text ("מ-Naboo.ai" → a broken link)
    chunks, cur = [], ""
    for para in body.split("\n\n"):
        if len(cur) + len(para) > 2800 and cur:
            chunks.append(cur); cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    chunks.append(cur)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c[:3000], "verbatim": True}} for c in chunks]
    msg = slack("chat.postMessage", channel=SLACK_CHANNEL, text=header, blocks=blocks, unfurl_links=False, unfurl_media=False)
    data = transcript.encode("utf-8")
    up = requests.get("https://slack.com/api/files.getUploadURLExternal", headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                      params={"filename": "transcript.txt", "length": len(data)}, timeout=30).json()
    if not up.get("ok"):
        raise RuntimeError(f"slack files.getUploadURLExternal: {up.get('error')}")
    requests.post(up["upload_url"], data=data, timeout=60).raise_for_status()
    slack("files.completeUploadExternal", files=[{"id": up["file_id"], "title": f"תמלול — {title}"}],
          channel_id=SLACK_CHANNEL, thread_ts=msg["ts"], initial_comment=f"התמלול המלא מההקלטה ({engine.split('/')[-1]})")


def process(meeting: dict, entry: dict) -> str:
    rec = recording_for(meeting["id"])
    if not rec:
        return "waiting-for-recording"
    title = (meeting.get("data") or {}).get("title") or meeting.get("title") or meeting["native_meeting_id"]
    with tempfile.TemporaryDirectory() as wd:
        t = time.time()
        lines, engine = transcribe(meeting, rec, wd)
        log.info("meeting %s: transcribed %d lines in %.0fs with %s", meeting["id"], len(lines), time.time() - t, engine)
    if not lines:
        return "empty"
    if not entry.get("written"):
        replace_vexa_transcript(meeting["id"], lines)
        entry["written"] = True
        log.info("meeting %s: final transcript written to Vexa", meeting["id"])
    if entry.get("status") == "posted":
        return "posted"
    transcript = as_text(lines)
    summary = summarize(transcript)
    minutes = max(1, round(lines[-1]["end"] / 60))
    post(meeting, title, summary, transcript, minutes, engine)
    log.info("meeting %s: posted to Slack", meeting["id"])
    return "posted"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = load_state()
    since = parse(os.environ.get("PROCESS_SINCE") or state.get("_since")) or datetime.now(timezone.utc)
    state["_since"] = since.isoformat()
    save_state(state)
    log.info("finalizer up: model=%s threads=%d since=%s", MODEL, THREADS, since.isoformat())
    while True:
        try:
            for m in vexa("/meetings", params={"limit": 100}).json().get("meetings", []):
                key = str(m["id"])
                ended = parse(m.get("end_time"))
                if m.get("status") != "completed" or not ended or ended < since or m.get("platform") != "google_meet":
                    continue
                entry = state.get(key) or {}
                if isinstance(entry, str):  # first-version state: a bare status string
                    entry = {"status": entry}
                if (entry.get("status") in ("posted", "empty") and entry.get("written", entry.get("status") == "empty")) or entry.get("attempts", 0) >= 3:
                    continue
                if entry.get("status") == "waiting-for-recording" and (datetime.now(timezone.utc) - ended).total_seconds() > 3600:
                    continue  # no recording an hour after the end — give up quietly
                try:
                    entry["status"] = process(m, entry)
                except Exception as e:
                    log.error("meeting %s failed: %s", key, e)
                    entry["status"] = f"error: {e}"[:200]
                    entry["attempts"] = entry.get("attempts", 0) + 1
                state[key] = entry
                save_state(state)
        except Exception as e:
            log.error("poll failed: %s", e)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
