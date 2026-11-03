"""After-meeting pass: full-recording Hebrew transcript → Claude summary → Slack (+ Vexa).

Vexa's live transcript is built from short per-speaker windows. Once a meeting completes, this job
re-transcribes the whole recording — through an OpenAI-compatible transcription API (default:
OpenRouter's elevenlabs/scribe-v2, sent as 10-minute mono MP3 pieces), falling back to a
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
  FINAL_STT_MODEL      default elevenlabs/scribe-v2 (empty → local ivrit.ai only)
  FINAL_STT_KEY_FILE   file holding the API key for FINAL_STT_URL
  FINAL_STT_CHUNK_S    default 3600 (voice labels from diarization restart in every piece)
  FINAL_STT_DIARIZE    default true — separate speakers by voice; several people on one Meet device become
                       "<name> · voice N" instead of all being labelled with that device's account
  DIARIZE_MAX_SPEAKERS default 6
  WHISPER_MODEL        fallback, default ivrit-ai/whisper-large-v3-turbo-ct2
  WHISPER_THREADS      default 2 (leave room for live bots)
  CLAUDE_BIN           default claude
  PROCESS_SINCE        ISO time; meetings that ended before it are skipped (default: first start)
  STATE_FILE           default ~/.local/state/uvi-meeting-finalizer.json
  POLL_S               default 60
  VEXA_PG_CONTAINER    default vexa-v012-postgres-1   (final transcript replaces the live one)
  VEXA_REDIS_CONTAINER default vexa-v012-redis-1
  BACKUP_DIR           default ~/.local/state/uvi-meeting-finalizer-backups (live transcript kept here)
  META_DIR             default ~/.local/state/uvi-calendar-sync/meetings (calendar-sync's per-meeting title,
                       attendees, language)
  ROSTER_FILE          default ~/.config/uvi/team_roster.json ({email: {displayName, slackId}})
  DEFAULT_LANGUAGE     language of untagged meetings (default en); a tag in the title/invite such as
                       [EN] or [HE] picks any code in LANGUAGES (default "en,he")
  ORG_CONTEXT          one line about the organization for the summary prompt, e.g. "Acme, a fintech startup"
  GLOSSARY             comma-separated names to keep spelled as-is (products, customers, tools)
  SUMMARY_PROMPT_FILE  optional file replacing the built-in prompt; {language}, {org} and {glossary} are filled in
  BOT_EMAILS           the bot's own calendar address(es), excluded from participants
  WORKSPACE_ID         optional — bind each finalized meeting to this shared workspace (covers meetings
                       started by pasting a link, which calendar-sync never planned)

Slack post: one short message — title, date, length, participants as @mentions, a link to the meeting —
plus the summary + transcript as ONE Google Doc in DRIVE_FOLDER_ID (written as the bot's Workspace user
through the service account's domain-wide delegation, Drive scope; Hebrew paragraphs set right-to-left).
Without Drive access the two are attached to the Slack message as .md files instead.
  DRIVE_FOLDER_ID      Google Drive folder for the meeting Docs (empty → .md attachments)
  DRIVE_AS             Workspace user the Docs are created as (required with DRIVE_FOLDER_ID)
  GOOGLE_SA_KEY_FILE   service-account key with domain-wide delegation incl. .../auth/drive
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
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
STT_MODEL = os.environ.get("FINAL_STT_MODEL", "elevenlabs/scribe-v2")
STT_KEY_FILE = os.path.expanduser(os.environ.get("FINAL_STT_KEY_FILE", ""))
CHUNK_S = int(os.environ.get("FINAL_STT_CHUNK_S", "3600"))
DIARIZE = os.environ.get("FINAL_STT_DIARIZE", "true").strip().lower() in ("1", "true", "yes", "on")
MAX_SPEAKERS = int(os.environ.get("DIARIZE_MAX_SPEAKERS", "6"))
THREADS = int(os.environ.get("WHISPER_THREADS", "2"))
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
STATE_FILE = os.path.expanduser(os.environ.get("STATE_FILE", "~/.local/state/uvi-meeting-finalizer.json"))
POLL_S = float(os.environ.get("POLL_S", "60"))
PG = os.environ.get("VEXA_PG_CONTAINER", "vexa-v012-postgres-1")
REDIS = os.environ.get("VEXA_REDIS_CONTAINER", "vexa-v012-redis-1")
BACKUP_DIR = os.path.expanduser(os.environ.get("BACKUP_DIR", "~/.local/state/uvi-meeting-finalizer-backups"))
META_DIR = os.path.expanduser(os.environ.get("META_DIR", "~/.local/state/uvi-calendar-sync/meetings"))
ROSTER_FILE = os.path.expanduser(os.environ.get("ROSTER_FILE", "~/.config/uvi/team_roster.json"))
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en").strip().lower()
LANGUAGES = [c.strip().lower() for c in os.environ.get("LANGUAGES", "en,he").split(",") if c.strip()]
LANG_RE = re.compile(r"(?:\[|\b(?:lang(?:uage)?\s*[:=]\s*))(" + "|".join(map(re.escape, LANGUAGES)) + r")\b\]?",
                     re.IGNORECASE)
ORG_CONTEXT = os.environ.get("ORG_CONTEXT", "a company").strip()
GLOSSARY = os.environ.get("GLOSSARY", "").strip()
SUMMARY_PROMPT_FILE = os.path.expanduser(os.environ.get("SUMMARY_PROMPT_FILE", ""))
RTL_LANGUAGES = {"he", "ar", "fa", "ur", "yi", "ps", "sd", "ug"}
LANGUAGE_NAMES = {"en": "English", "he": "Hebrew", "ar": "Arabic", "fr": "French", "de": "German", "es": "Spanish",
                  "ru": "Russian", "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "fa": "Persian"}
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "").strip()
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip()
DRIVE_AS = os.environ.get("DRIVE_AS", "").strip()
SA_KEY_FILE = os.path.expanduser(os.environ.get("GOOGLE_SA_KEY_FILE", ""))
BOT_EMAILS = {e.strip().lower() for e in os.environ.get("BOT_EMAILS", "").split(",") if e.strip()}

SUMMARY_PROMPT = """You are writing the summary of an internal meeting at {org}.
The text on stdin is an automatic {language} transcript of the recording (it has transcription errors;
infer the intended meaning, never invent facts that aren't there). Speaker labels come from the meeting
platform; labels like "Dana Cohen · voice 2" are DIFFERENT people sharing Dana's device in one room — if the
conversation shows who they are (someone addresses them by name), use that name in the summary.

Output GitHub Markdown, written in {language}. The FIRST line is exactly `TITLE: <a 3–8 word title>`.
Then these four sections — headings written in {language} — omitting any that would be empty:
## Summary — 2–4 sentences.
## Decisions — bullets.
## Action items — bullets "- **<who>:** <what>" (owner if stated, otherwise "unassigned").
## Open questions — bullets.
{glossary}No preamble. If the transcript is too short or garbled to summarize, write `TITLE:` and one line saying so."""


def summary_prompt(language: str) -> str:
    template = SUMMARY_PROMPT
    if SUMMARY_PROMPT_FILE:
        with open(SUMMARY_PROMPT_FILE) as f:
            template = f.read()
    glossary = (f"Keep these names in their usual spelling: {GLOSSARY}.\n" if GLOSSARY else "")
    return template.format(language=LANGUAGE_NAMES.get(language, language), org=ORG_CONTEXT, glossary=glossary)


# Slack-side words stay English (one left-to-right message); words inside the Doc follow its language.
SLACK_LABELS = {"participants": "Participants", "open": "Open in Vexa", "min": "min"}
DOC_LABELS = {"he": {"min": "דק׳", "transcript": "תמלול"}, "ar": {"min": "دقيقة", "transcript": "النص"}}


def doc_labels(language: str) -> dict:
    return DOC_LABELS.get(language, {"min": "min", "transcript": "Transcript"})

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


def words_to_segments(words: list[dict], off: float, piece: int = 0) -> list[tuple]:
    """Group word timestamps into sentence-like segments: break on end punctuation, a pause > 0.8 s, 15 s,
    or a change of diarized speaker. → (start, end, text, voice) with voice = "<piece>:<speaker>" or None."""
    out: list[tuple] = []
    cur: list[dict] = []

    def voice_of(w: dict):
        v = w.get("speaker", w.get("speaker_id"))
        return None if v is None else f"{piece}:{v}"

    def flush():
        out.append((off + cur[0]["start"], off + cur[-1]["end"], " ".join(x["word"].strip() for x in cur), voice_of(cur[0])))

    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.8 or w["end"] - cur[0]["start"] > 15 or voice_of(w) != voice_of(cur[0])):
            flush(); cur = []
        cur.append(w)
        if w["word"].rstrip().endswith((".", "?", "!")):
            flush(); cur = []
    if cur:
        flush()
    return out


def api_segments(src: str, workdir: str, language: str) -> list[tuple[float, float, str]]:
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
                                  data={"model": STT_MODEL, "language": language, "response_format": "verbose_json",
                                        "timestamp_granularities[]": "word",
                                        **({"diarize": "true", "num_speakers": str(MAX_SPEAKERS)} if DIARIZE else {})},
                                  files={"file": (name, f, "audio/mpeg")})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1) ** 2)
                continue
            r.raise_for_status()
            break
        body = r.json()
        off = i * CHUNK_S
        segs = body.get("segments") or []
        words = [w for w in (body.get("words") or []) if w.get("word", "").strip() and "start" in w]
        if words and (len(segs) <= 1 or DIARIZE):  # Scribe: word timings (+ speaker per word when diarized)
            out += words_to_segments(words, off, i)
        elif segs:
            out += [(off + float(s["start"]), off + float(s["end"]), s.get("text", ""), None) for s in segs]
        elif body.get("text"):
            out.append((off, off + CHUNK_S, body["text"], None))
    return out


def local_segments(src: str, workdir: str, language: str) -> list[tuple[float, float, str]]:
    audio = to_wav(src, os.path.join(workdir, "audio.wav"))
    segs, _ = whisper().transcribe(audio, language=language, vad_filter=True, beam_size=5, condition_on_previous_text=False)
    return [(s.start, s.end, s.text, None) for s in segs]


def name_voices(segs: list[tuple], live: list[dict], rec_start: float) -> dict:
    """Diarized voice → display name. Each voice takes the Meet participant it overlaps most; when several
    voices share one participant (people around one device) they become "<name> · voice N"."""
    overlap: dict = {}
    for start, end, _text, voice in segs:
        if voice is None:
            continue
        a, b = rec_start + start, rec_start + end
        for s in live:
            ov = min(b, s["end"]) - max(a, s["start"])
            if ov > 0 and s.get("speaker"):
                overlap.setdefault(voice, {}).setdefault(s["speaker"], 0.0)
                overlap[voice][s["speaker"]] += ov
    voices = sorted({v for *_x, v in segs if v is not None}, key=lambda v: (int(v.split(":")[0]), v))
    owner = {v: (max(overlap[v], key=overlap[v].get) if overlap.get(v) else None) for v in voices}
    names: dict = {}
    counter: dict = {}
    for v in voices:
        base = owner[v]
        shared = base is not None and sum(1 for o in owner.values() if o == base) > 1
        if base is None:
            counter["?"] = counter.get("?", 0) + 1
            names[v] = f"Speaker {counter['?']}"
        elif shared:
            counter[base] = counter.get(base, 0) + 1
            names[v] = f"{base} · voice {counter[base]}"
        else:
            names[v] = base
    return names


def transcribe(meeting: dict, rec: dict, workdir: str, language: str) -> tuple[list[dict], str, list[dict]]:
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
            segs = api_segments(src, workdir, language)
        except Exception as e:
            log.warning("meeting %s: %s failed (%s) — falling back to %s", meeting["id"], STT_MODEL, e, MODEL)
    if segs is None:
        engine = MODEL
        segs = local_segments(src, workdir, language)

    live = vexa(f"/transcripts/google_meet/{meeting['native_meeting_id']}").json().get("segments", [])
    rec_start = (parse(rec.get("created_at")) or parse(meeting.get("start_time"))).timestamp()
    out: list[dict] = []
    voice_names = name_voices(segs, live, rec_start)
    for start, end, raw, voice in segs:
        text = (raw or "").strip()
        if not text:
            continue
        spk = voice_names.get(voice) or speaker_at(live, rec_start + start, rec_start + end) or "?"
        if out and out[-1]["speaker"] == spk and start - out[-1]["end"] < 2.0:
            out[-1]["text"] += " " + text
            out[-1]["end"] = end
        else:
            out.append({"speaker": spk, "start": start, "end": end, "text": text})
    for line in out:
        line["abs_start"], line["abs_end"] = rec_start + line["start"], rec_start + line["end"]
    return out, engine, live


def as_text(lines: list[dict]) -> str:
    return "\n".join(f"[{int(l['start'] // 60):02d}:{int(l['start'] % 60):02d}] {l['speaker']}: {l['text']}" for l in lines)


def summarize(transcript: str, language: str) -> tuple[str, str]:
    """→ (title Claude proposes, Markdown body)."""
    prompt = summary_prompt(language)
    r = subprocess.run([CLAUDE, "-p", prompt, "--output-format", "text"], input=transcript,
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude failed ({r.returncode}): {r.stderr.strip()[:300]}")
    text = r.stdout.strip()
    first, _, rest = text.partition("\n")
    if first.startswith("TITLE:"):
        return first[len("TITLE:"):].strip(), rest.strip()
    return "", text


# --- meeting context: language, title, participants -------------------------------------------

def meeting_meta(code: str) -> dict:
    """calendar-sync's sidecar for this meeting; missing/unreadable → {} (a summary never waits on it)."""
    try:
        with open(os.path.join(META_DIR, f"{code}.json")) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("meeting meta for %s unreadable (%s) — continuing without invitees/title", code, e)
        return {}


def meeting_language(title: str, meta: dict) -> str:
    m = LANG_RE.search(title or "")
    if m:
        return m.group(1).lower()
    return (meta.get("language") or DEFAULT_LANGUAGE).lower()


def clean_title(title: str) -> str:
    return re.sub(r"\s{2,}", " ", LANG_RE.sub("", title or "")).strip(" -·|")


_roster: dict | None = None
_slack_users: list[dict] | None = None


def roster() -> dict:
    global _roster
    if _roster is None:
        try:
            with open(ROSTER_FILE) as f:
                _roster = {k.lower(): v for k, v in json.load(f).items()}
        except (FileNotFoundError, ValueError):
            _roster = {}
    return _roster


def slack_get(method: str, **params) -> dict:
    return requests.get(f"https://slack.com/api/{method}", headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                        params=params, timeout=30).json()


def slack_id_for_email(email: str) -> str | None:
    hit = roster().get(email.lower())
    if hit and hit.get("slackId"):
        return hit["slackId"]
    d = slack_get("users.lookupByEmail", email=email)
    return (d.get("user") or {}).get("id") if d.get("ok") else None


def slack_id_for_name(name: str) -> str | None:
    global _slack_users
    key = name.strip().lower()
    for v in roster().values():
        if v.get("displayName", "").strip().lower() == key and v.get("slackId"):
            return v["slackId"]
    if _slack_users is None:
        _slack_users, cursor = [], ""
        while True:
            d = slack_get("users.list", limit=200, cursor=cursor)
            if not d.get("ok"):
                break
            _slack_users += [u for u in d.get("members", []) if not u.get("deleted") and not u.get("is_bot")]
            cursor = (d.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    for u in _slack_users:
        names = {u.get("real_name", ""), (u.get("profile") or {}).get("real_name", ""), (u.get("profile") or {}).get("display_name", "")}
        if key in {n.strip().lower() for n in names if n}:
            return u["id"]
    return None


def participants(meta: dict, live: list[dict]) -> list[str]:
    """Slack mentions for the invitees + whoever spoke (by Meet name); unknown names stay plain text."""
    out: list[str] = []
    seen: set[str] = set()
    for email in meta.get("attendees") or []:
        if email.lower() in BOT_EMAILS:
            continue
        sid = slack_id_for_email(email)
        tag = f"<@{sid}>" if sid else email
        if tag not in seen:
            seen.add(tag); out.append(tag)
    for name in sorted({s.get("speaker") for s in live if s.get("speaker")}):
        sid = slack_id_for_name(name)
        tag = f"<@{sid}>" if sid else name
        if tag not in seen:
            seen.add(tag); out.append(tag)
    return out


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


def replace_vexa_transcript(meeting_id: int, lines: list[dict], language: str = "he") -> None:
    """Swap the meeting's live segments for the final ones (live rows backed up to BACKUP_DIR first)."""
    mid = int(meeting_id)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"meeting-{mid}-live.json")
    if not os.path.exists(backup_path):  # first write only — a rerun must not overwrite the live copy with final rows
        backup = psql(f"SELECT coalesce(json_agg(t), '[]') FROM transcriptions t WHERE meeting_id = {mid};")
        with open(backup_path, "w") as f:
            f.write(backup)
    values = ",\n".join(
        f"({mid}, {l['abs_start']:.3f}, {l['abs_end']:.3f}, {q(l['text'])}, {q(l['speaker']) if l['speaker'] != '?' else 'NULL'}, {q(language)}, 'final-{i:05d}', now())"
        for i, l in enumerate(lines))
    psql(f"BEGIN;\nDELETE FROM transcriptions WHERE meeting_id = {mid};\n"
         f"INSERT INTO transcriptions (meeting_id, start_time, end_time, text, speaker, language, segment_id, created_at) VALUES\n{values};\nCOMMIT;\n")
    subprocess.run(["docker", "exec", REDIS, "redis-cli", "DEL", f"meeting:{mid}:segments"], capture_output=True, timeout=30)


def line_md(l: dict) -> str:
    stamp = "[%02d:%02d]" % (int(l["start"] // 60), int(l["start"] % 60))
    who = l["speaker"] if l["speaker"] != "?" else ""
    return "**" + (stamp + " " + who).strip() + "** " + l["text"]


def _inline_md(text: str) -> str:
    out = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)


def md_to_html(md: str, rtl: bool) -> str:
    """The small Markdown subset our summaries use (#/## headings, - bullets, **bold**) → HTML for Docs import."""
    d = ' dir="rtl" style="direction:rtl;text-align:right"' if rtl else ""
    parts, in_list = [], False
    for line in md.splitlines():
        t = line.strip()
        if t.startswith(("- ", "* ", "• ")):
            if not in_list:
                parts.append(f"<ul{d}>"); in_list = True
            parts.append(f"<li{d}>{_inline_md(t[2:])}</li>")
            continue
        if in_list:
            parts.append("</ul>"); in_list = False
        if not t:
            continue
        m = re.match(r"(#{1,3})\s+(.*)", t)
        if m:
            n = len(m.group(1))
            parts.append(f"<h{n}{d}>{_inline_md(m.group(2))}</h{n}>")
        else:
            parts.append(f"<p{d}>{_inline_md(t)}</p>")
    if in_list:
        parts.append("</ul>")
    return "".join(parts)


def drive_doc(name: str, body_html: str, rtl: bool) -> str:
    """Create a Google Doc (converted from HTML) in DRIVE_FOLDER_ID as DRIVE_AS → its web link."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_file(
        SA_KEY_FILE, scopes=["https://www.googleapis.com/auth/drive"], subject=DRIVE_AS)
    creds.refresh(Request())
    dir_attr = ' dir="rtl"' if rtl else ""
    page = f'<html{dir_attr}><head><meta charset="utf-8"></head><body{dir_attr}>{body_html}</body></html>'
    boundary = "uvi-finalizer-boundary"
    meta = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.document", "parents": [DRIVE_FOLDER_ID]})
    payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
               f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{page}\r\n--{boundary}--").encode("utf-8")
    r = requests.post("https://www.googleapis.com/upload/drive/v3/files",
                      params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,webViewLink"},
                      headers={"Authorization": f"Bearer {creds.token}", "Content-Type": f"multipart/related; boundary={boundary}"},
                      data=payload, timeout=120)
    r.raise_for_status()
    return r.json()["webViewLink"]


def upload_files(files: list[tuple[str, str, str]], comment: str) -> None:
    """files = [(filename, title, text)] → one Slack message carrying them, with `comment` as its text."""
    ids = []
    for filename, title, text in files:
        data = text.encode("utf-8")
        up = slack_get("files.getUploadURLExternal", filename=filename, length=len(data))
        if not up.get("ok"):
            raise RuntimeError(f"slack files.getUploadURLExternal: {up.get('error')}")
        requests.post(up["upload_url"], data=data, timeout=60).raise_for_status()
        ids.append({"id": up["file_id"], "title": title})
    slack("files.completeUploadExternal", files=ids, channel_id=SLACK_CHANNEL, initial_comment=comment)


def post(meeting: dict, title: str, summary_md: str, lines: list[dict], minutes: int, engine: str,
         who: list[str], language: str) -> None:
    lab, dlab = SLACK_LABELS, doc_labels(language)
    started = parse(meeting.get("start_time"))
    when = started.astimezone().strftime("%d/%m/%Y %H:%M") if started else ""
    head = f":memo: *{title}* · {when} · {minutes} {lab['min']}"
    parts = [head]
    if who:
        parts.append(f"{lab['participants']}: {' '.join(who)}")
    if UI:
        parts.append(f"<{UI}/meetings/{meeting['id']}|{lab['open']}>")
    comment_parts = parts
    plain_who = ", ".join(sorted({l["speaker"] for l in lines if l["speaker"] != "?"}))
    meta_line = f"{when} · {minutes} {dlab['min']}" + (f" · {plain_who}" if plain_who else "")
    summary_doc = f"# {title}\n\n{meta_line}\n\n{summary_md}\n"
    transcript_doc = f"# {dlab['transcript']} — {title}\n\n{meta_line} · {engine.split('/')[-1]}\n\n" + "\n\n".join(
        line_md(l) for l in lines) + "\n"
    stamp = (started or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d")
    if DRIVE_FOLDER_ID and SA_KEY_FILE and DRIVE_AS:
        try:
            rtl = language in RTL_LANGUAGES
            body = md_to_html(summary_doc, rtl) + "<hr>" + md_to_html(transcript_doc.replace("\n\n", "\n"), rtl)
            link = drive_doc(f"{title} — {stamp}", body, rtl)
            slack("chat.postMessage", channel=SLACK_CHANNEL, unfurl_links=False, unfurl_media=False,
                  text="\n".join(comment_parts + [f":page_facing_up: <{link}|Summary + transcript (Google Doc)>"]))
            return
        except Exception as e:
            log.warning("Google Doc failed (%s) — attaching .md files instead", e)
    upload_files([(f"summary-{stamp}.md", f"Summary — {title}", summary_doc),
                  (f"transcript-{stamp}.md", f"Transcript — {title}", transcript_doc)], "\n".join(comment_parts))


def process(meeting: dict, entry: dict) -> str:
    rec = recording_for(meeting["id"])
    if not rec:
        return "waiting-for-recording"
    meta = meeting_meta(meeting["native_meeting_id"])
    raw_title = (meeting.get("data") or {}).get("title") or meeting.get("title") or meta.get("title") or ""
    language = meeting_language(raw_title, meta)
    with tempfile.TemporaryDirectory() as wd:
        t = time.time()
        lines, engine, live = transcribe(meeting, rec, wd, language)
        log.info("meeting %s: transcribed %d lines in %.0fs with %s (%s)", meeting["id"], len(lines), time.time() - t, engine, language)
    if not lines:
        return "empty"
    if WORKSPACE_ID and not meeting.get("workspace_id"):
        requests.post(f"{VEXA}/meetings/google_meet/{meeting['native_meeting_id']}/workspace", headers=VH,
                      json={"workspace_id": WORKSPACE_ID}, timeout=30)
    if not entry.get("written"):
        replace_vexa_transcript(meeting["id"], lines, language)
        entry["written"] = True
        log.info("meeting %s: final transcript written to Vexa", meeting["id"])
    if entry.get("status") == "posted":
        return "posted"
    proposed, summary = summarize(as_text(lines), language)
    title = clean_title(raw_title) or proposed or meeting["native_meeting_id"]
    minutes = max(1, round(lines[-1]["end"] / 60))
    post(meeting, title, summary, lines, minutes, engine, participants(meta, live), language)
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
