# meeting-finalizer — after-meeting Hebrew transcript, summary, Slack post

Vexa's live transcript is built from short per-speaker windows, which is weak for Hebrew. When a
meeting completes, this job re-transcribes the **full recording** with an ivrit.ai Whisper model
(faster-whisper, int8 on CPU — roughly 1.5× the meeting length on 2–3 cores), labels speakers from
Vexa's live segments by time overlap, asks the host's `claude` CLI (Claude Code login) for a Hebrew
summary (תקציר / החלטות / משימות / שאלות פתוחות), and posts it to a Slack channel with the full
transcript attached in the thread.

It runs on the host (not in a container) so it uses — and keeps refreshed — the same Claude Code
login the Vexa agent mounts.

## Install (Ubuntu host)

```bash
sudo apt-get install -y ffmpeg python3-venv
mkdir -p ~/.local/share/uvi-finalizer && python3 -m venv ~/.local/share/uvi-finalizer/venv
~/.local/share/uvi-finalizer/venv/bin/pip install faster-whisper requests numpy
# ~/.config/uvi/finalizer.env (mode 600): VEXA_API_BASE, VEXA_API_KEY, VEXA_UI_URL,
#   SLACK_BOT_TOKEN, SLACK_CHANNEL (channel id), CLAUDE_BIN, WHISPER_THREADS — see finalize.py
sudo cp uvi/meeting-finalizer/uvi-meeting-finalizer.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uvi-meeting-finalizer
journalctl -u uvi-meeting-finalizer -f
```
