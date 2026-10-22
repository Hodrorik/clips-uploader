"""TikTok Content Posting API, inbox (draft) upload only.

The video lands in the account's TikTok inbox; the post is finished by tapping it in the app.
Direct posting is not built: TikTok's guidelines don't approve apps that upload to your own
accounts, so an unaudited app could only ever post privately.
"""
import os
import time

import requests

BASE = "https://open.tiktokapis.com/v2"
MB = 1024 * 1024
MAX_SINGLE = 64 * MB


class TikTokError(RuntimeError):
    pass


def _post(path, token, body):
    r = requests.post(f"{BASE}{path}", json=body, timeout=60,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=UTF-8"})
    d = r.json() if r.content else {}
    err = d.get("error", {})
    if r.status_code != 200 or err.get("code") not in (None, "ok"):
        raise TikTokError(f"{path}: {r.status_code} {err.get('code')} {err.get('message')}")
    return d.get("data", {})


def refresh(tok, client_key, client_secret, now):
    """TikTok access tokens last 24 h; refresh tokens rotate, so always store the new one."""
    if now.timestamp() < tok.get("accessExpiresAt", 0) - 300:
        return None
    r = requests.post(f"{BASE}/oauth/token/", timeout=30,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      data={"client_key": client_key, "client_secret": client_secret,
                            "grant_type": "refresh_token", "refresh_token": tok["refreshToken"]})
    d = r.json()
    if "access_token" not in d:
        raise TikTokError(f"token refresh failed: {d}")
    return {**tok, "accessToken": d["access_token"], "refreshToken": d["refresh_token"],
            "accessExpiresAt": now.timestamp() + d["expires_in"], "openId": d.get("open_id", tok.get("openId"))}


def chunk_plan(size):
    """Media transfer rules: chunks 5-64 MB, last chunk may run to 128 MB, files <= 64 MB in one go."""
    if size <= MAX_SINGLE:
        return size, 1
    return MAX_SINGLE, size // MAX_SINGLE


def upload_to_inbox(tok, path, log=print):
    token = tok["accessToken"]
    size = os.path.getsize(path)
    chunk, count = chunk_plan(size)
    d = _post("/post/publish/inbox/video/init/", token, {"source_info": {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
    publish_id, url = d["publish_id"], d["upload_url"]

    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = requests.put(url, data=body, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"chunk {i + 1}/{count}: {r.status_code} {r.text[:200]}")
    log(f"    uploaded {size / MB:.1f} MB in {count} chunk(s), publish_id {publish_id}")

    deadline = time.time() + 3 * 60   # inbox processing can lag; never re-upload because of it
    while time.time() < deadline:
        s = _post("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        status = s.get("status")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return {"id": publish_id, "status": status}
        if status == "FAILED":
            raise TikTokError(f"upload failed: {s.get('fail_reason')}")
        time.sleep(15)
    return {"id": publish_id, "status": "PROCESSING"}
