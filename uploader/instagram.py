"""Instagram Reels via the Instagram API with Facebook Login (graph.facebook.com).

Facebook Login is needed for the paid-partnership label, Tag people and collaborators.
Each Instagram account is linked to a Facebook Page in the RodClip business portfolio, and
one non-expiring system-user token covers all of them.
"""
import json
import os
import time

import requests

VERSION = os.environ.get("IG_API_VERSION", "v26.0")
API = f"https://graph.facebook.com/{VERSION}"


class InstagramError(RuntimeError):
    pass


def _check(r, what):
    if r.status_code != 200:
        raise InstagramError(f"{what}: {r.status_code} {r.text[:300]}")
    return r.json()


def linked_accounts(token):
    """Every Instagram account reachable through the Pages this token can manage."""
    r = requests.get(f"{API}/me/accounts", timeout=30, params={
        "fields": "name,instagram_business_account{id,username}", "limit": 100, "access_token": token})
    out = {}
    for page in _check(r, "me/accounts").get("data", []):
        ig = page.get("instagram_business_account")
        if ig:
            out[ig["username"].lower()] = {"igUserId": ig["id"], "page": page["name"]}
    return out


def container_params(post, video_url, token):
    data = {"media_type": "REELS", "video_url": video_url, "caption": post.get("caption", ""),
            "share_to_feed": str(post.get("shareToFeed", True)).lower(), "access_token": token}
    if post.get("thumbOffsetMs") is not None:
        data["thumb_offset"] = str(int(post["thumbOffsetMs"]))
    if post.get("coverUrl"):
        data["cover_url"] = post["coverUrl"]
    if post.get("locationId"):
        data["location_id"] = post["locationId"]
    if post.get("collaborators"):
        data["collaborators"] = json.dumps(post["collaborators"][:3])
    if post.get("userTags"):
        data["user_tags"] = json.dumps([{"username": u.lstrip("@")} for u in post["userTags"]])
    if post.get("paidPartnership"):
        data["is_paid_partnership"] = "true"
        if post.get("sponsorIds"):
            data["branded_content_sponsor_ids"] = json.dumps(post["sponsorIds"])
    if post.get("aiGenerated"):
        data["is_ai_generated"] = "true"
    return data


def publish_reel(tok, video_url, post, log=print):
    """Create a REELS container from a public video URL, wait for processing, publish."""
    token, ig_id = tok["token"], tok["igUserId"]
    container = _check(requests.post(f"{API}/{ig_id}/media", timeout=60,
                                     data=container_params(post, video_url, token)), "create container")["id"]
    log(f"    container {container}, waiting for processing")

    deadline = time.time() + 15 * 60
    while True:
        st = _check(requests.get(f"{API}/{container}", timeout=30,
                                 params={"fields": "status_code,status", "access_token": token}), "status")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {code}: {st.get('status')}")
        if time.time() > deadline:
            raise InstagramError(f"container still {code} after 15 min")
        time.sleep(20)

    media_id = _check(requests.post(f"{API}/{ig_id}/media_publish", timeout=60,
                                    data={"creation_id": container, "access_token": token}), "publish")["id"]
    info = _check(requests.get(f"{API}/{media_id}", timeout=30,
                               params={"fields": "permalink,media_product_type", "access_token": token}), "permalink")
    return {"id": media_id, "url": info.get("permalink"), "productType": info.get("media_product_type")}
