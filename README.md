# Clips uploader (cloud)

Posts clips from Dropbox to Instagram, YouTube and TikTok on official APIs, from GitHub Actions. No laptop needed at post time.

- **Runner:** `.github/workflows/post.yml` runs `python -m uploader.run` every 10 min. It posts whatever in the queue is due and writes results to the ledger.
- **Everything else lives in Dropbox** under `/Clips/uploader/`:
  - `queue/<clip>.json`: what to post, where, when (see `queue_example.json`; file paths are Dropbox paths, times need a UTC offset)
  - `ledger.json`: one entry per `clip:platform:account` with `status` (SUCCESS / INBOX / CHECK / FAILED / SKIPPED_LATE), `url`, `error`
  - `accounts.json`: which accounts are signed in (no secrets)
  - `tokens.enc`: sign-in tokens, encrypted with `STATE_KEY`
  - `heartbeat.json`: last run time
- **Sign-ins:** run `auth.py` on the Mac (see its header). Local secrets: `~/.clips_uploader/config.json`.

Per platform:
- **Instagram:** Instagram API with Facebook Login (each IG account linked to a Facebook Page; one non-expiring system-user token). Published at `publishAt`; skipped if the runner is more than `latestMinutes` (default 120) late. Sets caption, paid-partnership label (`paidPartnership`), Tag people (`userTags`), collaborators, location (`locationId`), AI label, cover frame.
- **YouTube:** uploaded right away with `publishAt`, and YouTube publishes it. Until the Google API audit passes, API uploads are locked private (ledger shows CHECK). Recording location isn't in the API any more: tick it in Studio (background Chrome tab) for US-target campaigns.
- **TikTok:** sent to the account's TikTok inbox `inboxLeadMinutes` (default 20) before `publishAt`; the post is finished in the TikTok app (caption pasted there).

Failed posts retry on the next runs, 3 attempts in total.

Tests: `pip install -r requirements.txt responses pytest && python -m pytest -q tests`

`docs/` is the GitHub Pages site: the TikTok sign-in callback page and the privacy/terms page the developer apps ask for.

Scheduled workflows in a public repo stop after 60 days without a commit; push something at least monthly.
