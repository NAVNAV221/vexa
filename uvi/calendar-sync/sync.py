"""Google Calendar → Vexa planned meetings, through a service account (no secret ICS link).

Reads one Google Workspace calendar (the bot's own) through the Calendar API with a service
account that has domain-wide delegation, and keeps Vexa's planned meetings in step with it:
every upcoming event that carries a Google Meet link becomes a `scheduled` Vexa meeting with
auto-join on, so Vexa's scheduler sends the bot ~60 s before it starts.

  * new event                → POST   /meetings            (title, scheduled_at, meeting_url)
  * moved event              → PATCH  /meetings/{id}       (scheduled_at, title)
  * cancelled / declined /
    uninvited / link removed → DELETE /meetings/{id}       (only plans this job created, only
                                                            while still planned)

One Vexa meeting per Meet link (Vexa's own rule): a recurring series tracks its next occurrence.
State (which plans this job owns) lives in STATE_FILE so it never touches meetings it didn't create.

For each planned meeting it also writes META_DIR/<meet-code>.json — title, start/end, attendee emails
and the meeting language — for the after-meeting job (Slack mentions, transcription language). The
language comes from a tag in the event title or description, e.g. `[EN]` or `lang: en`, for any code
listed in LANGUAGES; untagged meetings get the after-meeting job's default.

Bots end with the calendar event (END_GRACE_MIN after its end). Meet gives a bot no reliable "alone in
the call" signal, so IDLE_STOP_MIN also stops ANY running bot the key can see — calendar or started from a
pasted link — once its live transcript has had no new speech for that long.

Env:
  CALENDAR_ID                 the calendar to read, e.g. bot@example.com (also the impersonated user)
  GOOGLE_SA_KEY_FILE          service-account JSON key (domain-wide delegation)
  GOOGLE_CALENDAR_SCOPE       default .../auth/calendar.readonly — set to the scope the grant authorizes
  VEXA_API_BASE               e.g. http://gateway:8000
  VEXA_API_KEY                Vexa user token the plans are created under
  SYNC_INTERVAL_S             default 60
  LOOKAHEAD_DAYS              default 7
  STATE_FILE                  default /state/calendar-sync.json
  END_GRACE_MIN               stop a still-running bot this long after the event ends (default 5)
  IDLE_STOP_MIN               stop any running bot after this many minutes without new speech (0 = off; default 0)
  LANGUAGES                   language codes a title/description tag may select (default "en,he")
  META_DIR                    default /state/meetings
  WORKSPACE_ID                optional — bind every planned meeting to this shared Vexa workspace, so its
                              members (not only the key's owner) see the meeting and its transcript
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

log = logging.getLogger("calendar-sync")
# Must be a scope the domain-wide delegation grant authorizes; the job only ever reads.
SCOPES = [os.environ.get("GOOGLE_CALENDAR_SCOPE", "https://www.googleapis.com/auth/calendar.readonly")]
MEET_RE = re.compile(r"https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})")
PLANNED = {"idle", "scheduled"}

CALENDAR_ID = os.environ["CALENDAR_ID"]
VEXA = os.environ.get("VEXA_API_BASE", "http://gateway:8000").rstrip("/")
VEXA_HEADERS = {"X-API-Key": os.environ["VEXA_API_KEY"], "Content-Type": "application/json"}
INTERVAL = float(os.environ.get("SYNC_INTERVAL_S", "60"))
LOOKAHEAD = timedelta(days=float(os.environ.get("LOOKAHEAD_DAYS", "7")))
STATE_FILE = os.environ.get("STATE_FILE", "/state/calendar-sync.json")
END_GRACE = timedelta(minutes=float(os.environ.get("END_GRACE_MIN", "5")))
LIVE = {"requested", "joining", "awaiting_admission", "needs_help", "active"}
ENDS: dict[str, datetime] = {}  # Meet code → calendar end of the occurrence the bot is for
META_DIR = os.environ.get("META_DIR", "/state/meetings")
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "").strip()
IDLE_STOP = timedelta(minutes=float(os.environ.get("IDLE_STOP_MIN", "0")))
LANGUAGES = [c.strip().lower() for c in os.environ.get("LANGUAGES", "en,he").split(",") if c.strip()]
LANG_RE = re.compile(r"(?:\[|\b(?:lang(?:uage)?\s*[:=]\s*))(" + "|".join(map(re.escape, LANGUAGES)) + r")\b\]?",
                     re.IGNORECASE)


def meeting_language(*texts: str) -> str | None:
    for t in texts:
        m = LANG_RE.search(t or "")
        if m:
            return m.group(1).lower()
    return None


def write_meta(code: str, ev: dict) -> None:
    os.makedirs(META_DIR, exist_ok=True)
    path = os.path.join(META_DIR, f"{code}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"title": ev["title"], "start": iso(ev["start"]), "end": iso(ev["end"]) if ev["end"] else None,
                   "attendees": ev["attendees"], "language": ev["language"]}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

_creds = service_account.Credentials.from_service_account_file(
    os.environ["GOOGLE_SA_KEY_FILE"], scopes=SCOPES, subject=CALENDAR_ID
)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def load_state() -> dict[str, int]:
    try:
        with open(STATE_FILE) as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict[str, int]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def meet_code(event: dict) -> str | None:
    """The Meet code of an event: hangoutLink, a conference entry point, or a link in location/description."""
    candidates = [event.get("hangoutLink") or ""]
    candidates += [ep.get("uri", "") for ep in (event.get("conferenceData") or {}).get("entryPoints", [])]
    candidates += [event.get("location") or "", event.get("description") or ""]
    for text in candidates:
        m = MEET_RE.search(text)
        if m:
            return m.group(1)
    return None


def bot_declined(event: dict) -> bool:
    for a in event.get("attendees", []):
        if a.get("self") or a.get("email", "").lower() == CALENDAR_ID.lower():
            return a.get("responseStatus") == "declined"
    return False


def wanted_events() -> dict[str, dict]:
    """Meet code → {start, title} for the next occurrence of every joinable upcoming event."""
    if not _creds.valid:
        _creds.refresh(Request())
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": iso(now - timedelta(minutes=10)),
        "timeMax": iso(now + LOOKAHEAD),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    }
    url = f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(CALENDAR_ID)}/events"
    out: dict[str, dict] = {}
    page = None
    while True:
        if page:
            params["pageToken"] = page
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {_creds.token}"}, timeout=20)
        r.raise_for_status()
        body = r.json()
        for ev in body.get("items", []):
            start = parse((ev.get("start") or {}).get("dateTime"))  # all-day events have no dateTime
            end = parse((ev.get("end") or {}).get("dateTime"))
            code = meet_code(ev)
            if not start or not code or ev.get("status") == "cancelled" or bot_declined(ev):
                continue
            if end and end < now:
                continue
            if code not in out:  # ordered by start → first seen is the next occurrence
                attendees = [a["email"].lower() for a in ev.get("attendees", [])
                             if a.get("email") and not a.get("resource") and a.get("email", "").lower() != CALENDAR_ID.lower()
                             and a.get("responseStatus") != "declined"]
                org = (ev.get("organizer") or {}).get("email")
                if org and org.lower() not in attendees and org.lower() != CALENDAR_ID.lower():
                    attendees.insert(0, org.lower())
                out[code] = {"start": start, "end": end, "title": ev.get("summary") or "Meeting", "attendees": attendees,
                             "language": meeting_language(ev.get("summary", ""), ev.get("description", ""))}
        page = body.get("nextPageToken")
        if not page:
            return out


def vexa_meetings() -> dict[str, dict]:
    """Meet code → newest Vexa meeting record for that link (any status)."""
    r = requests.get(f"{VEXA}/meetings", headers=VEXA_HEADERS, params={"limit": 100}, timeout=20)
    r.raise_for_status()
    by_code: dict[str, dict] = {}
    for m in r.json().get("meetings", []):
        if m.get("platform") != "google_meet" or not m.get("native_meeting_id"):
            continue
        code = m["native_meeting_id"]
        if code not in by_code or m["id"] > by_code[code]["id"]:
            by_code[code] = m
    return by_code


def sync_once(state: dict[str, int]) -> None:
    wanted = wanted_events()
    existing = vexa_meetings()

    for code, ev in wanted.items():
        if ev["end"]:
            ENDS[code] = ev["end"]
        try:
            write_meta(code, ev)
        except OSError as e:
            log.warning("could not write meeting meta for %s: %s", code, e)
        cur = existing.get(code)
        if cur and cur.get("status") not in PLANNED and cur.get("status") not in {"completed", "failed"}:
            continue  # a bot is live on this link right now
        if cur and cur.get("status") in {"completed", "failed"}:
            began = parse(cur.get("scheduled_at")) or parse(cur.get("start_time")) or parse(cur.get("created_at"))
            if began and abs((began - ev["start"]).total_seconds()) < 3 * 3600:
                continue  # this occurrence already happened (or failed) — don't re-join it
        if cur and cur.get("status") in PLANNED:
            if state.get(code) != cur["id"]:
                continue  # someone planned it by hand — theirs, not ours
            have = parse(cur.get("scheduled_at"))
            if have is None or abs((have - ev["start"]).total_seconds()) > 30:
                r = requests.patch(f"{VEXA}/meetings/{cur['id']}", headers=VEXA_HEADERS,
                                   json={"scheduled_at": iso(ev["start"]), "auto_join": True}, timeout=20)
                log.info("moved %s → %s (%s)", code, iso(ev["start"]), r.status_code)
            continue
        body = {"title": ev["title"], "scheduled_at": iso(ev["start"]),
                "meeting_url": f"https://meet.google.com/{code}", "auto_join": True}
        if WORKSPACE_ID:
            body["workspace_id"] = WORKSPACE_ID
        r = requests.post(f"{VEXA}/meetings", headers=VEXA_HEADERS, timeout=20, json=body)
        if r.status_code in (200, 201):
            state[code] = r.json()["id"]
            log.info("planned %s at %s: %s", code, iso(ev["start"]), ev["title"])
        else:
            log.warning("could not plan %s: %s %s", code, r.status_code, r.text[:200])

    for code, mid in list(state.items()):
        if code in wanted:
            continue
        cur = existing.get(code)
        if cur and cur["id"] == mid and cur.get("status") in PLANNED:
            r = requests.delete(f"{VEXA}/meetings/{mid}", headers=VEXA_HEADERS, timeout=20)
            log.info("unplanned %s (event gone; %s)", code, r.status_code)
        if not cur or cur["id"] != mid or cur.get("status") in PLANNED | {"completed", "failed"}:
            state.pop(code, None)

    # Meet gives the bot no reliable "alone" signal, so end it with the calendar event.
    now = datetime.now(timezone.utc)
    for code, end in list(ENDS.items()):
        cur = existing.get(code)
        if now > end + END_GRACE and cur and cur.get("status") in LIVE:
            r = requests.delete(f"{VEXA}/bots/google_meet/{code}", headers=VEXA_HEADERS, timeout=20)
            log.info("stopped bot in %s: event ended %s (%s)", code, iso(end), r.status_code)
        if now > end + timedelta(hours=2) or (cur and cur.get("status") in {"completed", "failed"} and now > end):
            ENDS.pop(code, None)

    if IDLE_STOP:
        for code, cur in existing.items():
            if cur.get("status") != "active":
                continue
            began = parse(cur.get("start_time"))
            r = requests.get(f"{VEXA}/transcripts/google_meet/{code}", headers=VEXA_HEADERS, timeout=20)
            if r.status_code != 200:
                continue  # e.g. a workspace meeting another member owns — its owner's key stops it
            last = max((float(s.get("end") or 0) for s in r.json().get("segments", [])), default=0.0)
            quiet_since = datetime.fromtimestamp(last, timezone.utc) if last else began
            if quiet_since and now - quiet_since > IDLE_STOP:
                r = requests.delete(f"{VEXA}/bots/google_meet/{code}", headers=VEXA_HEADERS, timeout=20)
                log.info("stopped bot in %s: no speech since %s (%s)", code, iso(quiet_since), r.status_code)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("syncing %s every %ss (lookahead %s)", CALENDAR_ID, INTERVAL, LOOKAHEAD)
    state = load_state()
    while True:
        try:
            sync_once(state)
            save_state(state)
        except Exception as e:  # keep the loop alive; the next tick retries
            log.error("sync failed: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
