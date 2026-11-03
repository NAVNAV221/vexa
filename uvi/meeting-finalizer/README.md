# meeting-finalizer — after-meeting Hebrew transcript, summary, Slack post

Vexa's live transcript is built from short per-speaker windows, which is weak for Hebrew. When a
meeting completes, this job re-transcribes the **full recording** with `elevenlabs/scribe-v2`
through OpenRouter (10-minute mono MP3 pieces, word timestamps regrouped into sentences; seconds per
meeting, ~$0.22/hour of audio) — chosen from a Hebrew bake-off of OpenRouter's transcription models
(it held up best on noisy room audio; `microsoft/mai-transcribe-2` was a close second) — falling back to a local ivrit.ai
Whisper model (faster-whisper, int8 CPU, ~1.5× the meeting length) if the API fails. Speakers are separated by voice (Scribe diarization, `FINAL_STT_DIARIZE`) and matched to Meet's
participant names by timing: several people on one device become `<name> · voice N`. It replaces
Vexa's live transcript with the final one (live copy backed up; the live pass is kept running only to
name speakers — Vexa 0.12 records no separate speaker timeline), labels speakers from
Vexa's live segments by time overlap, asks the host's `claude` CLI (Claude Code login) for a Hebrew
summary, and posts one Slack message — title, date, length, participants as @mentions (calendar
invitees via `team_roster.json` → Slack `users.lookupByEmail`, plus Meet speaker names), a link — with
`summary-<date>.md` and `transcript-<date>.md` attached (right-to-left Hebrew reads badly inline).

**Language:** `DEFAULT_LANGUAGE` for untagged meetings; a tag in the title (`[EN]`, `[HE]`, `lang: ar`,
… any code in `LANGUAGES`) picks another. It drives the transcription and summary language; right-to-left
languages (Hebrew, Arabic, Persian, …) get right-to-left Docs.

**Where it goes:** with `DRIVE_FOLDER_ID` + `DRIVE_AS` (a Workspace user the service account may
impersonate with `.../auth/drive`), one Google Doc per meeting (summary + transcript) in that folder,
linked from the Slack post; otherwise `summary-<date>.md` + `transcript-<date>.md` attached to it.

**Your organization:** `ORG_CONTEXT` (one line about the company), `GLOSSARY` (names to keep spelled
as-is) and optionally `SUMMARY_PROMPT_FILE` (a full prompt with `{language}`, `{org}`, `{glossary}`).

It runs on the host (not in a container) so it uses — and keeps refreshed — the same Claude Code
login the Vexa agent mounts.

## Install (Ubuntu host)

```bash
sudo apt-get install -y ffmpeg python3-venv
mkdir -p ~/.local/share/uvi-finalizer && python3 -m venv ~/.local/share/uvi-finalizer/venv
~/.local/share/uvi-finalizer/venv/bin/pip install faster-whisper requests numpy google-auth
# ~/.config/uvi/finalizer.env (mode 600): VEXA_API_BASE, VEXA_API_KEY, VEXA_UI_URL,
#   SLACK_BOT_TOKEN, SLACK_CHANNEL (channel id), CLAUDE_BIN, FINAL_STT_KEY_FILE, WHISPER_THREADS — see finalize.py
# ~/.config/uvi/team_roster.json: {email: {displayName, slackId}} (copy of skills-poc app/team_roster.json)
sudo cp uvi/meeting-finalizer/uvi-meeting-finalizer@.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uvi-meeting-finalizer@$USER
journalctl -u uvi-meeting-finalizer@$USER -f
```
