"""YouTube Shorts via the YouTube Data API v3 (same request body as tools/uploader/yt_upload.py)."""
import random
import time

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
RETRY_STATUS = {500, 502, 503, 504}


def service(tok, client_id, client_secret):
    creds = Credentials(None, refresh_token=tok["refreshToken"], token_uri=TOKEN_URI,
                        client_id=client_id, client_secret=client_secret, scopes=SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def build_body(y):
    status = {"privacyStatus": "private" if y.get("publishAt") else y.get("privacy", "private"),
              "selfDeclaredMadeForKids": bool(y.get("madeForKids", False)),
              "containsSyntheticMedia": bool(y.get("containsSyntheticMedia", False))}
    if y.get("publishAt"):
        status["publishAt"] = y["publishAt"]
    snippet = {"title": y["title"][:100], "description": y.get("description", ""),
               "tags": y.get("tags", []), "categoryId": str(y.get("categoryId", "20"))}
    if y.get("language"):
        snippet["defaultLanguage"] = snippet["defaultAudioLanguage"] = y["language"]
    body = {"snippet": snippet, "status": status}
    if y.get("paidPromotion"):
        body["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
    return body


def upload(yt, path, y, log=print):
    body = build_body(y)
    req = yt.videos().insert(part=",".join(body), body=body, notifySubscribers=True,
                             media_body=MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True))
    resp, attempt = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status not in RETRY_STATUS or attempt >= 6:
                raise
            attempt += 1
            time.sleep(min(60, 2 ** attempt + random.random()))
    vid = resp["id"]
    time.sleep(5)
    item = yt.videos().list(part="status", id=vid).execute()["items"][0]["status"]
    problems = []
    if y.get("publishAt") and not item.get("publishAt"):
        problems.append("schedule missing: locked private (API project not audited yet)")
    if item.get("uploadStatus") in ("failed", "rejected", "deleted"):
        problems.append(f"uploadStatus={item.get('uploadStatus')}")
    log(f"    {vid} privacy={item.get('privacyStatus')} publishAt={item.get('publishAt')}")
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}", "problems": problems}
