# Bamberg News

Independent Russian-language video news service for Boris's family in Bamberg.
It has its own Telegram control bot, workspace, source policy, run history,
YouTube OAuth credentials and public destination channel. It never reads the
personal Butler vault or publishes through Boris's existing YouTube channel.

## What is already enforced

- Two schedules: 05:00 and 17:00 in `Europe/Berlin`, including daylight-saving
  changes.
- Six editorial sections: Bamberg/Landkreis local news, world politics,
  Russia's war against Ukraine, **daily Bamberg weather**, **daily health**,
  and garden work.
- Weather is a mandatory, first-class daily section for an older audience: the
  next 24–48 hour DWD forecast, temperature, conditions and the result of an
  official-warning check. Heat, storm, frost, heavy-rain, ice and UV warnings
  lead the weather section and include their official timing and area.
- Health is a daily short public-information section for the 70+ audience. It
  uses only primary German public-health sources (BIÖG/gesund.bund.de, RKI or
  Bavaria's health ministry). It may give a sourced prevention or preparation
  reminder, but never diagnoses, changes medication, prescribes treatment or
  substitutes for a clinician.
- A local/world fact requires a catalogued primary source or two independent
  sources. Ukraine/Russia facts always require two independent publishers and
  domains. Unknown sites cannot label themselves primary.
- Every weather segment cites DWD. Every garden segment cites DWD; seasonal work advice also cites LWG, AELF
  Bamberg or another primary gardening authority.
- `bamberg-news validate` rejects an unsupported source, missing source dates,
  missing category coverage, opinion-unmarked factual scene, weak war evidence,
  or garden advice without the required evidence.
- Uploads use the established idempotent Butler YouTube uploader, but separate
  credentials and a persisted allowlist of the exact target YouTube channel ID.
  An OAuth token for another channel blocks the upload.

The catalogued anchors are in `sources.json`. They cover the city, Landkreis,
police, regional government, transport, DWD, Bavarian garden authorities,
UN/IAEA/EU/NATO and independent news services. They are a mandatory first pass,
not a false claim of exhaustive web coverage.

## Create the YouTube channel

Create a **Brand Account** channel, not a channel tied to the visible name of
the personal Google account. Suggested name: `Bamberg News`; suggested handle:
`@BambergNewsRU` (choose another if unavailable). A Brand Account can have a
different public name and lets one Google account manage several channels.

1. Sign in to YouTube with the Google account that owns Boris's channels.
2. Open the channel list: profile picture → **Switch account** / **View all
   channels**, then choose **Create a channel**.
3. Enter the final name and handle, then select **Create channel**. Do not move
   an existing channel or select **Replace** in any channel-transfer screen.
4. Open YouTube Studio while switched into the new channel; copy its channel ID
   (it starts with `UC`). Keep the new channel selected during the OAuth device
   authorization below.

Google's current Brand Account flow is documented in its [channel-creation
help](https://support.google.com/youtube/answer/1646861) and [channel-management
guide](https://support.google.com/youtube/answer/4642409).

## One-time installation and destination setup

Do this after creating the channel. It needs Boris's bot token and the Google
OAuth client secret; neither belongs in this repository.

1. Create `@BambergNews...bot` in BotFather. Put its token in
   `~/.openclaw/.env` as `BAMBERG_NEWS_BOT_TOKEN`. Do not put the token in an
   agent prompt or any Markdown file.
2. Create `/Users/boris/.openclaw/workspace-bamberg-news/` and copy this
   directory's `AGENTS.md`, `sources.json`, `bin/`, `lib/`, `state/` and
   `skills/` into it. Also install the existing shared renderer at
   `lib/news_video.py`; it is sourced from
   `implementation/news-video/lib/news_video.py`.
3. Create a separate Google OAuth client for this publication (or deliberately
   reuse the existing client). Store `client_secret.json` only at:
   `/Users/boris/.openclaw/workspace-bamberg-news/credentials/youtube/`.
4. Run the existing device-flow uploader with an isolated credential directory:

   ```sh
   BUTLER_YOUTUBE_CREDENTIALS=/Users/boris/.openclaw/workspace-bamberg-news/credentials/youtube \
     /Users/boris/.openclaw/workspace-butler/bin/butler-youtube auth
   ```

   During the device-code flow, ensure the **Bamberg News** Brand Account is
   selected. Then verify the reported name/ID.
5. Persist the expected ID. This file contains only the public channel ID, not
   an OAuth secret:

   ```sh
   ./bin/bamberg-news configure-youtube UC...your-channel-id...
   ./bin/bamberg-news youtube-status
   ```

6. Review and apply [`openclaw.patch-bamberg-news.json5`](../../openclaw.patch-bamberg-news.json5)
   with `--dry-run` first. It is disabled by default. After the bot token is in
   the environment and the dry-run is correct, enable the account and restart
   the gateway:

   ```sh
   openclaw config patch --file ./openclaw.patch-bamberg-news.json5 --dry-run
   openclaw config patch --file ./openclaw.patch-bamberg-news.json5
   openclaw config set channels.telegram.accounts.bamberg-news.enabled true
   openclaw gateway restart
   ```

7. Add the schedules only after a successful render and `youtube-status` check:

   ```sh
   openclaw cron add --agent bamberg-news --name bamberg-news-morning \
     --description "Bamberg factual news video at 05:00 Europe/Berlin" \
     --cron "0 5 * * *" --tz Europe/Berlin --exact --session isolated \
     --no-deliver --timeout-seconds 3600 \
     --message "$(cat /Users/boris/.openclaw/workspace-bamberg-news/state/bamberg-news-morning-cron-prompt.txt)"

   openclaw cron add --agent bamberg-news --name bamberg-news-evening \
     --description "Bamberg factual news video at 17:00 Europe/Berlin" \
     --cron "0 17 * * *" --tz Europe/Berlin --exact --session isolated \
     --no-deliver --timeout-seconds 3600 \
     --message "$(cat /Users/boris/.openclaw/workspace-bamberg-news/state/bamberg-news-evening-cron-prompt.txt)"
   ```

These commands create external configuration and schedules. Do not run them
until the target channel name/ID and OAuth account have been verified.

## Local verification

```sh
cd implementation/bamberg-news
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=../news-video:. \
  /opt/miniconda3/bin/python -m unittest discover -s tests -v
./bin/bamberg-news validate tests/fixtures/valid-bamberg-episode.json
```

Use `publish --dry-run` to inspect metadata without an upload. A real upload
needs its explicit authorization reference and will refuse a mismatched channel
before calling the shared uploader.
