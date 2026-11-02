# calendar-sync — Google Calendar → Vexa planned meetings

Invite the bot's Workspace account to a meeting and the bot joins it. This job reads the bot's
calendar through the Calendar API with a service account that has domain-wide delegation
(`calendar.readonly`), so no secret ICS address and no external-sharing admin change is needed.

Every `SYNC_INTERVAL_S` (default 60 s) it lists upcoming events (`LOOKAHEAD_DAYS`, default 7) and,
for each one with a Google Meet link that the bot hasn't declined, keeps one Vexa planned meeting
(`scheduled`, auto-join on). Vexa's scheduler sends the bot ~60 s before the start. Moved events are
re-timed, removed/cancelled/declined events are unplanned. It only ever edits plans it created
(tracked in its state file); a recurring series tracks its next occurrence; an occurrence that
already ran isn't re-joined.

## Run

```bash
# secrets, outside the repo
~/.config/uvi/cal-service-account.json   # service-account key (domain-wide delegation)
~/.config/uvi/calendar-sync.env          # CALENDAR_ID=<bot@domain>  VEXA_API_KEY=<user token>
docker compose -p vexa-calendar-sync -f uvi/docker-compose.calendar-sync.yml up -d --build
docker logs -f vexa-calendar-sync-calendar-sync-1
```

The plans are created under the user that owns `VEXA_API_KEY`, so they appear in that user's
Meetings view. It also writes `<state>/meetings/<meet-code>.json` (title, times, attendee emails,
language) for the after-meeting job; state lives in a host directory
(`~/.local/state/uvi-calendar-sync`, writable by the container's uid 10001).

**Language tag:** `[EN]` (or `lang: en`) in the event title or description marks an English meeting;
`[HE]`/none = Hebrew. Vexa's own calendar page will say "No calendar connected" — this job plans the
meetings through the API instead of an ICS feed, which is expected.
