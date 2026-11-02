# uvi/ — our self-hosted Vexa deployment

Vexa runs our meeting bot ("Shraga"): it joins Google Meet signed in as a Workspace user, transcribes
Hebrew live, and keeps transcripts and notes. This folder holds what is specific to our deployment;
everything else is stock Vexa from upstream (`Vexa-ai/vexa`), plus the small patches on `main`.

## Patches (vs upstream `Vexa-ai/vexa`)

| Change | Why |
|---|---|
| Signed-in bots launch without `--incognito` | incognito ignored the restored Google session → bots landed in the guest lobby |
| Gemini consent detector is dialog-scoped | the "Use Gemini to take notes" offer card held an admitted bot in `joining` |
| `DEFAULT_BOT_LANGUAGE` (meeting-api) | calendar/auto-join spawns get a language (we use `he`) |
| `CALENDAR_SYNC_INTERVAL_S` surfaced in compose | faster ICS polling when ICS is used |
| Terminal signs in with a verified Cloudflare Access identity | one sign-in at the edge, no second Vexa login |
| `calendar-sync/` | calendar → planned meetings through a service account instead of a secret ICS link |

## Setup on a server (Linux, amd64)

1. `git clone <this repo> vexa && cd vexa && make all`
   (seeds `deploy/compose/.env`, pulls images, starts the stack on 127.0.0.1 ports).
2. Build the patched images: `make -C deploy/compose bot BOT_IMAGE=vexa/vexa-bot:uvi`, then
   `docker compose -p vexa-v012 --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml build meeting-api terminal`.
3. In `deploy/compose/.env` set:
   - STT (live): `TRANSCRIPTION_SERVICE_URL=https://openrouter.ai/api`, `TRANSCRIPTION_SERVICE_TOKEN=<OpenRouter key>`,
     `TRANSCRIPTION_MODEL=microsoft/mai-transcribe-2` (best Hebrew in our bake-off; the after-meeting pass uses it too)
   - Agent model: `HOST_CLAUDE_CREDENTIALS=/home/<user>/.claude/.credentials.json` after `claude` → `/login` on the host
   - Bot: `BROWSER_IMAGE=vexa/vexa-bot:uvi`, `DEFAULT_BOT_NAME=Shraga`, `DEFAULT_BOT_LANGUAGE=he`
   - Signed-in bot: `BOT_AUTHENTICATED=true`, `BOT_USERDATA_S3_PATH=userdata/<bot>`, `BOT_S3_ENDPOINT=http://storage:9000`,
     `BOT_S3_BUCKET=vexa`, `BOT_S3_ACCESS_KEY=<scoped name, not the root key>`, `BOT_S3_SECRET_KEY=<random>`
   - Web UI behind Cloudflare Access: `NEXTAUTH_URL=https://<host>`, `CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com`, `CF_ACCESS_AUD=<app AUD>`
   Then `docker compose -p vexa-v012 --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml up -d`.
4. Sign the bot's Google account in **from a Linux browser** (cookies made on macOS don't decrypt in the
   Linux bot): run `core/meetings/modules/remote-browser/dist/provision-cli.js` inside the bot image with
   Xvfb + x11vnc, connect with a VNC client over an SSH tunnel, sign in. The session lands in the
   scoped userdata prefix.
5. Calendar: see [`calendar-sync/`](calendar-sync/README.md).
6. After-meeting transcript + summary to Slack: see [`meeting-finalizer/`](meeting-finalizer/README.md).

## Exposure

All ports bind to 127.0.0.1. The web UI is published only through a Cloudflare tunnel + an Access
application (Google sign-in, org email domains, plus a service token for machine callers).
