#!/usr/bin/env python3
"""Operator view of the after-meeting pass — answers "where is my summary?" without reading code.

  sudo python3 meetingctl.py list                 recent meetings + finalizer state
  sudo python3 meetingctl.py status <code|id>     one meeting: Vexa status, recording, transcript, state, log
  sudo python3 meetingctl.py reprocess <code|id>  run the after-meeting pass again (--force if already posted)

Run it with sudo from the deployment owner's checkout: the owner (and so the env file, the state file and
the systemd instance uvi-meeting-finalizer@<owner>) is the user that owns this file. Stdlib only — no venv.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import urllib.request

OWNER = pwd.getpwuid(os.stat(os.path.abspath(__file__)).st_uid)
ENV_FILE = os.path.join(OWNER.pw_dir, ".config/uvi/finalizer.env")
SERVICE = f"uvi-meeting-finalizer@{OWNER.pw_name}"


def env() -> dict:
    out = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


E = env()
API = E.get("VEXA_API_BASE", "http://127.0.0.1:18056").rstrip("/")
STATE_FILE = (E.get("STATE_FILE") or "~/.local/state/uvi-meeting-finalizer.json").replace("~", OWNER.pw_dir, 1)
BACKUP_DIR = os.path.join(OWNER.pw_dir, ".local/state/uvi-meeting-finalizer-backups")
META_DIR = os.path.join(OWNER.pw_dir, ".local/state/uvi-calendar-sync/meetings")


def get(path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"X-API-Key": E["VEXA_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def meetings() -> list[dict]:
    return get("/meetings?limit=100").get("meetings", [])


def find(ref: str) -> dict:
    ms = meetings()
    hits = [m for m in ms if str(m["id"]) == ref or m.get("native_meeting_id") == ref]
    if not hits:
        sys.exit(f"no meeting {ref!r} in the last {len(ms)} meetings")
    return max(hits, key=lambda m: m["id"])


def title(m: dict) -> str:
    return (m.get("data") or {}).get("title") or m.get("title") or ""


def cmd_list(_a) -> None:
    st = state()
    print(f"{'id':>4}  {'code':<13} {'vexa':<10} {'ended (UTC)':<17} {'finalizer':<24} title")
    for m in sorted(meetings(), key=lambda m: m["id"], reverse=True)[:15]:
        entry = st.get(str(m["id"])) or {}
        fin = entry.get("status", "-") if isinstance(entry, dict) else entry
        print(f"{m['id']:>4}  {m.get('native_meeting_id', ''):<13} {m.get('status', ''):<10} "
              f"{(m.get('end_time') or '')[:16]:<17} {fin[:24]:<24} {title(m)}")


def cmd_status(a) -> None:
    m = find(a.ref)
    mid, code = m["id"], m.get("native_meeting_id")
    data = m.get("data") or {}
    print(f"meeting   {mid} · {code} · {title(m)!r}")
    print(f"vexa      {m.get('status')} · {m.get('start_time')} → {m.get('end_time')} · "
          f"reason={data.get('completion_reason') or m.get('completion_reason')} · live segments captured={data.get('segments_captured')}")
    recs = [r for r in get("/recordings?limit=100").get("recordings", []) if r.get("meeting_id") == mid]
    for r in recs:
        sizes = [f"{(f.get('file_size_bytes') or 0) / 1e6:.1f} MB {f.get('type')}" for f in r.get("media_files", [])]
        print(f"recording {r['id']} · {r.get('status')} · {', '.join(sizes) or 'no media'}")
    if not recs:
        print("recording none yet (the finalizer waits up to 1 h after the end)")
    segs = get(f"/transcripts/google_meet/{code}").get("segments", [])
    backup = os.path.join(BACKUP_DIR, f"meeting-{mid}-live.json")
    kind = "final (live copy backed up)" if os.path.exists(backup) else "live"
    print(f"transcript {len(segs)} segments in Vexa — {kind}")
    meta = os.path.join(META_DIR, f"{code}.json")
    if os.path.exists(meta):
        with open(meta) as f:
            md = json.load(f)
        print(f"calendar  {md.get('title')!r} · {len(md.get('attendees') or [])} attendees · language={md.get('language') or 'default'}")
    entry = state().get(str(mid))
    print(f"finalizer {json.dumps(entry) if entry else 'not processed yet'}")
    log = subprocess.run(["journalctl", "-u", SERVICE, "--since", "-2d", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout.splitlines()
    mine = [l for l in log if f"meeting {mid}:" in l or f"meeting {mid} " in l]
    print("log       " + ("\n          ".join(mine[-8:]) if mine else "(no lines for this meeting)"))


def cmd_reprocess(a) -> None:
    m = find(a.ref)
    key = str(m["id"])
    entry = state().get(key) or {}
    if isinstance(entry, dict) and entry.get("status") == "posted" and not a.force:
        sys.exit(f"meeting {key} was already posted to Slack — a re-run posts again; pass --force if that's wanted")
    subprocess.run(["systemctl", "stop", SERVICE], check=True)  # it holds the state in memory
    try:
        st = state()
        st.pop(key, None)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
        os.chown(tmp, OWNER.pw_uid, OWNER.pw_gid)
        os.replace(tmp, STATE_FILE)
    finally:
        subprocess.run(["systemctl", "start", SERVICE], check=True)
    print(f"meeting {key} queued again — follow with: journalctl -fu {SERVICE} -o cat")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("status"); s.add_argument("ref", help="Meet code (abc-defg-hij) or Vexa meeting id")
    r = sub.add_parser("reprocess"); r.add_argument("ref"); r.add_argument("--force", action="store_true")
    a = p.parse_args()
    {"list": cmd_list, "status": cmd_status, "reprocess": cmd_reprocess}[a.cmd](a)


if __name__ == "__main__":
    main()
