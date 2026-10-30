import datetime as dt
import json

import pytest
import responses
from cryptography.fernet import Fernet

from uploader import instagram, run, store, tiktok

MB = 1024 * 1024
KEY = Fernet.generate_key().decode()
IG_TOKENS = {("meta", "system_user"): {"token": "t"}, ("instagram", "wr"): {"igUserId": "1"}}


class FakeDropbox:
    def __init__(self):
        self.files = {}

    def read_bytes(self, path):
        return self.files.get(path)

    def write_bytes(self, path, data):
        self.files[path] = data

    def read_json(self, path, default):
        b = self.files.get(path)
        return default if b is None else json.loads(b)

    def write_json(self, path, data):
        self.files[path] = json.dumps(data).encode()

    def list_folder(self, path):
        return [{"name": p.rsplit("/", 1)[1], "path_lower": p} for p in self.files if p.startswith(path + "/")]

    def temp_link(self, path):
        return f"https://dl.example/{path}"

    def download(self, path, dest):
        open(dest, "wb").write(b"x" * 10)
        return dest

    deleted = None

    def delete(self, path):
        self.deleted = (self.deleted or []) + [path]
        return self.deleted.count(path) == 1

    def space_used_pct(self):
        return 42.0


def test_chunk_plan():
    assert tiktok.chunk_plan(3 * MB) == (3 * MB, 1)
    assert tiktok.chunk_plan(64 * MB) == (64 * MB, 1)
    chunk, n = tiktok.chunk_plan(200 * MB)
    assert (chunk, n) == (64 * MB, 3)
    assert 200 * MB - chunk * (n - 1) <= 128 * MB   # last chunk within the 128 MB rule


def test_token_store_roundtrip():
    dbx = FakeDropbox()
    s = store.TokenStore(dbx, KEY)
    s.put("instagram", "warroom", {"token": "abc"})
    s.save()
    assert b"abc" not in dbx.files[store.TOKENS]          # encrypted at rest
    assert store.TokenStore(dbx, KEY).get("instagram", "warroom") == {"token": "abc"}


def _setup(monkeypatch, posts, tokens):
    dbx = FakeDropbox()
    s = store.TokenStore(dbx, KEY)
    for (p, a), v in tokens.items():
        s.put(p, a, v)
    s.save()
    dbx.write_json(f"{store.QUEUE_DIR}/T01.json", {"clip": "T01", "file": "/Clips/ready/t/T01.mp4", "posts": posts})
    monkeypatch.setattr(run, "dropbox_from_env", lambda: dbx)
    monkeypatch.setenv("STATE_KEY", KEY)
    return dbx


def _args(now, dry=False):
    class A:
        pass
    a = A()
    a.dry_run, a.clip, a.now = dry, None, now
    return a


def test_instagram_waits_until_due_then_posts(monkeypatch):
    post = {"platform": "instagram", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00", "caption": "hi"}
    dbx = _setup(monkeypatch, [post], IG_TOKENS)
    calls = []
    monkeypatch.setattr(instagram, "publish_reel", lambda t, url, p: calls.append(url) or {"id": "9", "url": "https://ig/9"})

    run.run(_args("2026-10-24T23:50:00+01:00"))
    assert calls == []
    run.run(_args("2026-10-25T00:05:00+01:00"))
    run.run(_args("2026-10-25T00:15:00+01:00"))       # already done: no second post
    assert calls == ["https://dl.example//Clips/ready/t/T01.mp4"]
    rec = dbx.read_json(store.LEDGER, {})["T01:instagram:wr"]
    assert rec["status"] == "SUCCESS" and rec["url"] == "https://ig/9"


def test_instagram_too_late_is_skipped(monkeypatch):
    post = {"platform": "instagram", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00"}
    dbx = _setup(monkeypatch, [post], IG_TOKENS)
    monkeypatch.setattr(instagram, "publish_reel", lambda *a: pytest.fail("should not post"))
    run.run(_args("2026-10-25T03:00:00+01:00"))
    assert dbx.read_json(store.LEDGER, {})["T01:instagram:wr"]["status"] == "SKIPPED_LATE"


def test_failure_retries_then_stops(monkeypatch):
    post = {"platform": "instagram", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00"}
    dbx = _setup(monkeypatch, [post], IG_TOKENS)
    n = []
    def boom(*a):
        n.append(1)
        raise instagram.InstagramError("nope")
    monkeypatch.setattr(instagram, "publish_reel", boom)
    for m in range(5):
        run.run(_args(f"2026-10-25T00:0{m}:00+01:00"))
    assert len(n) == run.MAX_ATTEMPTS
    assert dbx.read_json(store.LEDGER, {})["T01:instagram:wr"]["status"] == "FAILED"


def test_missing_signin_fails_cleanly(monkeypatch):
    post = {"platform": "instagram", "account": "nobody", "publishAt": "2026-10-25T00:00:00+01:00"}
    dbx = _setup(monkeypatch, [post], {})
    run.run(_args("2026-10-25T00:01:00+01:00"))
    assert "no instagram sign-in" in dbx.read_json(store.LEDGER, {})["T01:instagram:nobody"]["error"]


def test_tiktok_goes_to_inbox_early(monkeypatch):
    post = {"platform": "tiktok", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00"}
    tok = {"accessToken": "a", "refreshToken": "r", "accessExpiresAt": 9e12}
    dbx = _setup(monkeypatch, [post], {("tiktok", "wr"): tok})
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k"); monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setattr(tiktok, "upload_to_inbox", lambda t, path: {"id": "p1", "status": "SEND_TO_USER_INBOX"})
    run.run(_args("2026-10-24T23:30:00+01:00"))
    assert "T01:tiktok:wr" not in dbx.read_json(store.LEDGER, {}) or \
        dbx.read_json(store.LEDGER, {})["T01:tiktok:wr"].get("status") is None
    run.run(_args("2026-10-24T23:41:00+01:00"))
    assert dbx.read_json(store.LEDGER, {})["T01:tiktok:wr"]["status"] == "INBOX"


@responses.activate
def test_publish_reel_http_flow(monkeypatch):
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    api = instagram.API
    responses.post(f"{api}/1/media", json={"id": "C1"})
    responses.get(f"{api}/C1", json={"status_code": "IN_PROGRESS"})
    responses.get(f"{api}/C1", json={"status_code": "FINISHED"})
    responses.post(f"{api}/1/media_publish", json={"id": "M1"})
    responses.get(f"{api}/M1", json={"permalink": "https://www.instagram.com/reel/abc/", "media_product_type": "REELS"})
    out = instagram.publish_reel({"token": "t", "igUserId": "1"}, "https://v", {"caption": "c", "collaborators": ["a"]},
                                 log=lambda *a: None)
    assert out["url"].endswith("/reel/abc/")
    body = responses.calls[0].request.body
    assert "media_type=REELS" in body and "share_to_feed=true" in body and "collaborators=%5B%22a%22%5D" in body


def test_container_params_disclosure_and_tags():
    d = instagram.container_params({"caption": "c", "paidPartnership": True, "userTags": ["@totalwar"],
                                    "collaborators": ["a"], "aiGenerated": False}, "https://v", "t")
    assert d["is_paid_partnership"] == "true"
    assert json.loads(d["user_tags"]) == [{"username": "totalwar"}]
    assert json.loads(d["collaborators"]) == ["a"]
    assert "is_ai_generated" not in d and d["media_type"] == "REELS"


def test_missing_meta_token(monkeypatch):
    post = {"platform": "instagram", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00"}
    dbx = _setup(monkeypatch, [post], {("instagram", "wr"): {"igUserId": "1"}})
    run.run(_args("2026-10-25T00:01:00+01:00"))
    assert "system-user token" in dbx.read_json(store.LEDGER, {})["T01:instagram:wr"]["error"]


@responses.activate
def test_tiktok_inbox_upload_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok.time, "sleep", lambda s: None)
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\0" * (3 * MB))
    responses.post(f"{tiktok.BASE}/post/publish/inbox/video/init/",
                   json={"data": {"publish_id": "p1", "upload_url": "https://up.example/x"}, "error": {"code": "ok"}})
    responses.put("https://up.example/x", status=201)
    responses.post(f"{tiktok.BASE}/post/publish/status/fetch/",
                   json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
    out = tiktok.upload_to_inbox({"accessToken": "a"}, str(f), log=lambda *a: None)
    assert out["status"] == "SEND_TO_USER_INBOX"
    init = json.loads(responses.calls[0].request.body)["source_info"]
    assert init == {"source": "FILE_UPLOAD", "video_size": 3 * MB, "chunk_size": 3 * MB, "total_chunk_count": 1}
    assert responses.calls[1].request.headers["Content-Range"] == f"bytes 0-{3 * MB - 1}/{3 * MB}"


def _run_at(monkeypatch, when, dry=False, clip=None):
    """run.run at a pretend time, with ledger timestamps at that time too."""
    t = store.parse(when)
    monkeypatch.setattr(run, "iso", lambda x=None: (x or t).isoformat(timespec="seconds"))
    a = _args(when, dry)
    a.clip = clip
    run.run(a)


IG_POST = {"platform": "instagram", "account": "wr", "publishAt": "2026-10-25T00:00:00+01:00", "caption": "hi"}


def _posted_ok(monkeypatch):
    monkeypatch.setattr(instagram, "publish_reel", lambda t, url, p: {"id": "9", "url": "https://ig/9"})


def test_cleanup_waits_24h_then_deletes_once(monkeypatch):
    dbx = _setup(monkeypatch, [IG_POST], IG_TOKENS)
    _posted_ok(monkeypatch)
    _run_at(monkeypatch, "2026-10-25T00:05:00+01:00")
    _run_at(monkeypatch, "2026-10-25T12:00:00+01:00")
    assert dbx.deleted is None                       # posted, but still inside the 24 h grace
    _run_at(monkeypatch, "2026-10-26T00:10:00+01:00")
    _run_at(monkeypatch, "2026-10-26T00:20:00+01:00")
    assert dbx.deleted == ["/Clips/ready/t/T01.mp4"]  # deleted once, never retried
    assert dbx.read_json(run.CLEANED, {})["T01"]["deleted"] == ["/Clips/ready/t/T01.mp4"]
    assert dbx.read_json(f"{store.BASE}/heartbeat.json", {})["dropboxUsedPct"] == 42.0


def test_cleanup_keeps_files_that_did_not_post(monkeypatch):
    dbx = _setup(monkeypatch, [IG_POST], IG_TOKENS)
    monkeypatch.setattr(instagram, "publish_reel", lambda *a: pytest.fail("should not post"))
    _run_at(monkeypatch, "2026-10-25T03:00:00+01:00")     # SKIPPED_LATE
    _run_at(monkeypatch, "2026-10-28T03:00:00+01:00")
    assert dbx.deleted is None and run.CLEANED not in dbx.files


def test_cleanup_waits_for_every_platform(monkeypatch):
    tt = {"platform": "tiktok", "account": "wr", "publishAt": "2026-10-26T00:00:00+01:00"}
    dbx = _setup(monkeypatch, [IG_POST, tt], {**IG_TOKENS, ("tiktok", "wr"): {"accessToken": "a", "accessExpiresAt": 9e12}})
    _posted_ok(monkeypatch)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k"); monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setattr(tiktok, "upload_to_inbox", lambda t, path: {"id": "p1", "status": "PROCESSING"})
    _run_at(monkeypatch, "2026-10-25T00:05:00+01:00")      # IG posts
    _run_at(monkeypatch, "2026-10-26T00:00:00+01:00")      # TikTok lands as CHECK (still processing)
    _run_at(monkeypatch, "2026-10-29T00:00:00+01:00")
    assert dbx.deleted is None                       # CHECK needs a human look: keep the file


def test_cleanup_keeps_file_shared_with_waiting_clip(monkeypatch):
    dbx = _setup(monkeypatch, [IG_POST], IG_TOKENS)
    later = {**IG_POST, "publishAt": "2026-11-05T00:00:00+00:00"}
    dbx.write_json(f"{store.QUEUE_DIR}/T02.json", {"clip": "T02", "file": "/Clips/ready/t/T01.mp4", "posts": [later]})
    _posted_ok(monkeypatch)
    _run_at(monkeypatch, "2026-10-25T00:05:00+01:00")
    _run_at(monkeypatch, "2026-10-27T00:00:00+01:00")
    assert dbx.deleted is None
    assert dbx.read_json(run.CLEANED, {})["T01"]["kept"] == ["/Clips/ready/t/T01.mp4"]


def test_cleanup_off_for_keepfile_single_clip_and_dry_run(monkeypatch):
    dbx = _setup(monkeypatch, [IG_POST], IG_TOKENS)
    _posted_ok(monkeypatch)
    _run_at(monkeypatch, "2026-10-25T00:05:00+01:00")
    _run_at(monkeypatch, "2026-10-27T00:00:00+01:00", dry=True)
    _run_at(monkeypatch, "2026-10-27T00:00:00+01:00", clip="T01")
    assert dbx.deleted is None
    job = dbx.read_json(f"{store.QUEUE_DIR}/T01.json", None)
    dbx.write_json(f"{store.QUEUE_DIR}/T01.json", {**job, "keepFile": True})
    _run_at(monkeypatch, "2026-10-27T00:00:00+01:00")
    assert dbx.deleted is None
