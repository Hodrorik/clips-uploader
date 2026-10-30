"""Runner: posts every queued item that is due, records results in the Dropbox ledger.

  python -m uploader.run [--dry-run] [--clip MR04] [--now 2026-10-24T00:05:00+01:00]

Timing per platform:
  youtube   uploaded as soon as it is queued; YouTube itself publishes at publishAt
  instagram published at publishAt (skipped if the runner is more than latestMinutes late)
  tiktok    sent to the account's TikTok inbox inboxLeadMinutes before publishAt

Clean-up: once every post of a clip is SUCCESS or INBOX and CLEANUP_HOURS have passed since the
last one went out, the clip's video file is deleted from Dropbox (restorable from Dropbox's deleted
files for 30 days) and logged in cleaned.json. CHECK, FAILED and SKIPPED_LATE keep the file, as does
"keepFile": true in the queue file. Only runs on a full pass of the real queue (not --clip, not --local).
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import traceback

from . import instagram, tiktok, youtube
from .store import (LEDGER, QUEUE_DIR, BASE, TokenStore, dropbox_from_env, env, iso, now, parse)

DONE = {"SUCCESS", "INBOX", "CHECK", "SKIPPED_LATE"}   # CHECK = uploaded but needs a human look
MAX_ATTEMPTS = 3
CLEANED = f"{BASE}/cleaned.json"
CLEANABLE = {"SUCCESS", "INBOX"}                      # the platform has its own copy of the video
CLEANUP_HOURS = 24
VIDEO_EXT = (".mp4", ".mov", ".m4v")


def due(post, t):
    when = parse(post["publishAt"])
    p = post["platform"]
    if p == "youtube":
        return True, None
    if p == "tiktok":
        return t >= when - dt.timedelta(minutes=post.get("inboxLeadMinutes", 20)), None
    late = t - when
    if late > dt.timedelta(minutes=post.get("latestMinutes", 120)):
        return False, f"runner {int(late.total_seconds() // 60)} min late, not posted"
    return t >= when, None


def run(args):
    dbx = dropbox_from_env()
    tokens = TokenStore(dbx, env("STATE_KEY"))
    ledger = dbx.read_json(LEDGER, {})
    t = parse(args.now) if args.now else now()
    files = {}
    tmp = tempfile.mkdtemp(prefix="clips_")

    def local_copy(path):
        if path not in files:
            dest = os.path.join(tmp, os.path.basename(path))
            print(f"    downloading {path}")
            files[path] = dbx.download(path, dest)
        return files[path]

    jobs = [e for e in dbx.list_folder(QUEUE_DIR) if e["name"].endswith(".json")]
    if args.clip:
        jobs = [e for e in jobs if e["name"] == f"{args.clip}.json"]
    print(f"{iso(t)}  {len(jobs)} queued clip(s)")
    loaded = []

    for entry in sorted(jobs, key=lambda e: e["name"]):
        job = dbx.read_json(entry["path_lower"], None)
        loaded.append(job)
        clip = job["clip"]
        for post in job["posts"]:
            key = f"{clip}:{post['platform']}:{post['account']}"
            rec = ledger.setdefault(key, {})
            if rec.get("status") in DONE or rec.get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            ready, skip_reason = due(post, t)
            if skip_reason:
                rec.update(status="SKIPPED_LATE", error=skip_reason, at=iso())
                dbx.write_json(LEDGER, ledger)
                print(f"{key}: SKIPPED_LATE ({skip_reason})")
                continue
            if not ready:
                continue
            print(f"{key}: posting (due {post['publishAt']})")
            if args.dry_run:
                continue
            rec["attempts"] = rec.get("attempts", 0) + 1
            try:
                result = post_one(post, job, tokens, dbx, local_copy, t)
                status = result.pop("status", "SUCCESS")
                rec.update(result)
                rec.update(status=status, error=None, at=iso())
                print(f"{key}: {rec['status']} {rec.get('url') or ''}")
            except Exception as e:  # keep going with the other posts
                rec.update(status="FAILED", error=f"{type(e).__name__}: {e}"[:500], at=iso())
                print(f"{key}: FAILED {rec['error']}")
                traceback.print_exc()
            tokens.save()
            dbx.write_json(LEDGER, ledger)

    if not args.clip and QUEUE_DIR == f"{BASE}/queue":
        try:
            clean_posted(dbx, loaded, ledger, t, dry=args.dry_run)
        except Exception:  # never let clean-up stop the heartbeat
            traceback.print_exc()

    beat = {"lastRun": iso(), "queued": len(jobs)}
    try:
        beat["dropboxUsedPct"] = dbx.space_used_pct()
    except Exception as e:
        beat["dropboxUsedPct"] = None
        print(f"Dropbox space check skipped: {e}")
    dbx.write_json(f"{BASE}/heartbeat.json", beat)
    failed = [k for k, v in ledger.items() if v.get("status") == "FAILED"]
    if failed:
        print(f"FAILED so far: {', '.join(failed)}")


def job_files(job):
    paths = {job["file"]} if job.get("file") else set()
    return paths | {p["file"] for p in job.get("posts", []) if p.get("file")}


def finished(job, ledger, t):
    """True when every post is SUCCESS/INBOX and the last one went out keepHours (default 24) ago."""
    posts = job.get("posts") or []
    if not posts:
        return False
    times = []
    for post in posts:
        rec = ledger.get(f"{job['clip']}:{post['platform']}:{post['account']}", {})
        if rec.get("status") not in CLEANABLE:
            return False
        times.append(parse(post["publishAt"]))
        if rec.get("at"):
            times.append(parse(rec["at"]))
    return t - max(times) >= dt.timedelta(hours=job.get("keepHours", CLEANUP_HOURS))


def clean_posted(dbx, jobs, ledger, t, dry=False):
    """Delete the video of every fully posted clip, unless a clip still waiting to post uses the same file."""
    cleaned = dbx.read_json(CLEANED, {})
    todo, still_needed = [], set()
    for job in jobs:
        if job["clip"] in cleaned:
            continue
        if not job.get("keepFile") and finished(job, ledger, t):
            todo.append(job)
        else:
            still_needed |= job_files(job)
    for job in todo:
        removed, kept = [], []
        for path in sorted(job_files(job)):
            if path in still_needed or not path.lower().endswith(VIDEO_EXT):
                kept.append(path)
            elif dry:
                print(f"{job['clip']}: would delete {path}")
            elif dbx.delete(path):
                removed.append(path)
        if dry:
            continue
        cleaned[job["clip"]] = {"deleted": removed, "kept": kept, "at": iso()}
        print(f"{job['clip']}: cleaned up, deleted {', '.join(removed) or 'nothing'}")
    if todo and not dry:
        dbx.write_json(CLEANED, cleaned)


def post_one(post, job, tokens, dbx, local_copy, t):
    p, acct = post["platform"], post["account"]
    tok = tokens.get(p, acct)
    if not tok:
        raise RuntimeError(f"no {p} sign-in stored for account '{acct}' (run auth.py)")
    src = post.get("file", job["file"])

    if p == "instagram":
        meta = tokens.get("meta", "system_user")
        if not meta:
            raise RuntimeError("no Meta system-user token stored (run auth.py meta)")
        return instagram.publish_reel({**tok, "token": meta["token"]}, dbx.temp_link(src), post)

    if p == "tiktok":
        new = tiktok.refresh(tok, env("TIKTOK_CLIENT_KEY"), env("TIKTOK_CLIENT_SECRET"), t)
        if new:
            tokens.put(p, acct, new); tokens.save(); tok = new
        r = tiktok.upload_to_inbox(tok, local_copy(src))
        if r["status"] == "PROCESSING":
            return {"id": r["id"], "status": "CHECK", "note": "uploaded; TikTok still processing - check the app inbox"}
        return {"id": r["id"], "status": "INBOX", "note": "finish the post from the TikTok app inbox"}

    if p == "youtube":
        y = dict(post)
        if parse(y["publishAt"]) <= t + dt.timedelta(minutes=10):
            y.pop("publishAt"); y["privacy"] = y.get("privacy", "public")
        yt = youtube.service(tok, env("GOOGLE_CLIENT_ID"), env("GOOGLE_CLIENT_SECRET"))
        r = youtube.upload(yt, local_copy(src), y)
        return {"id": r["id"], "url": r["url"], "status": "CHECK" if r["problems"] else "SUCCESS",
                "problems": r["problems"]}

    raise RuntimeError(f"unknown platform {p}")


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--clip")
    a.add_argument("--now", help="pretend time, ISO with offset (testing)")
    run(a.parse_args())


if __name__ == "__main__":
    sys.exit(main())
