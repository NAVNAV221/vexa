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

Env:
  CALENDAR_ID                 the calendar to read, e.g. bot@example.com (also the impersonated user)
  GOOGLE_SA_KEY_FILE          service-account JSON key (domain-wide delegation)
  GOOGLE_CALENDAR_SCOPE       default .../auth/calendar.readonly — set to the scope the grant authorizes
  VEXA_API_BASE               e.g. http://gateway:8000
  VEXA_API_KEY                Vexa user token the plans are created under
  SYNC_INTERVAL_S             default 60
  LOOKAHEAD_DAYS              default 7
  STATE_FILE                  default /state/calendar-sync.json
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
                out[code] = {"start": start, "title": ev.get("summary") or "Meeting"}
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
        r = requests.post(f"{VEXA}/meetings", headers=VEXA_HEADERS, timeout=20, json={
            "title": ev["title"], "scheduled_at": iso(ev["start"]),
            "meeting_url": f"https://meet.google.com/{code}", "auto_join": True,
        })
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
