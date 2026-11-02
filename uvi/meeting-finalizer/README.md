# meeting-finalizer — after-meeting Hebrew transcript, summary, Slack post

Vexa's live transcript is built from short per-speaker windows, which is weak for Hebrew. When a
meeting completes, this job re-transcribes the **full recording** with `elevenlabs/scribe-v2`
through OpenRouter (10-minute mono MP3 pieces, word timestamps regrouped into sentences; seconds per
meeting, ~$0.22/hour of audio) — chosen from a Hebrew bake-off of OpenRouter's transcription models
(it held up best on noisy room audio; `microsoft/mai-transcribe-2` was a close second) — falling back to a local ivrit.ai
Whisper model (faster-whisper, int8 CPU, ~1.5× the meeting length) if the API fails. It replaces
Vexa's live transcript with the final one (live copy backed up; the live pass is kept running only to
name speakers — Vexa 0.12 records no separate speaker timeline), labels speakers from
Vexa's live segments by time overlap, asks the host's `claude` CLI (Claude Code login) for a Hebrew
summary, and posts one Slack message — title, date, length, participants as @mentions (calendar
invitees via `team_roster.json` → Slack `users.lookupByEmail`, plus Meet speaker names), a link — with
`summary-<date>.md` and `transcript-<date>.md` attached (right-to-left Hebrew reads badly inline).

**Language:** Hebrew by default; a meeting is English when its title carries `[EN]` (calendar event
title/description, or the title of a meeting planned in Vexa). It drives the transcription language
and the summary language.

It runs on the host (not in a container) so it uses — and keeps refreshed — the same Claude Code
login the Vexa agent mounts.

## Install (Ubuntu host)

```bash
sudo apt-get install -y ffmpeg python3-venv
mkdir -p ~/.local/share/uvi-finalizer && python3 -m venv ~/.local/share/uvi-finalizer/venv
~/.local/share/uvi-finalizer/venv/bin/pip install faster-whisper requests numpy
# ~/.config/uvi/finalizer.env (mode 600): VEXA_API_BASE, VEXA_API_KEY, VEXA_UI_URL,
#   SLACK_BOT_TOKEN, SLACK_CHANNEL (channel id), CLAUDE_BIN, FINAL_STT_KEY_FILE, WHISPER_THREADS — see finalize.py
# ~/.config/uvi/team_roster.json: {email: {displayName, slackId}} (copy of skills-poc app/team_roster.json)
sudo cp uvi/meeting-finalizer/uvi-meeting-finalizer.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uvi-meeting-finalizer
journalctl -u uvi-meeting-finalizer -f
```
