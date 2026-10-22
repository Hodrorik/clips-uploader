#!/usr/bin/env python3
"""One-time sign-ins, run on the Mac. Tokens go encrypted into Dropbox for the cloud runner.

  python auth.py keygen                         make STATE_KEY (once)
  python auth.py dropbox                        Dropbox refresh token (once)
  python auth.py meta                           Meta system-user token (once, covers every IG account)
  python auth.py instagram --account warroom --username warr00m.clips
  python auth.py youtube   --account warroom    browser sign-in, pick the channel
  python auth.py tiktok    --account warroom    browser sign-in, paste the code
  python auth.py show                           list stored accounts (no secrets)

Local config (secrets, never share): ~/.clips_uploader/config.json
"""
import argparse
import getpass
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import requests
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).parent))
from uploader import instagram  # noqa: E402
from uploader.dbx import Dropbox  # noqa: E402
from uploader.store import ACCOUNTS, TokenStore, iso, now  # noqa: E402

CONF_DIR = Path.home() / ".clips_uploader"
CONF = CONF_DIR / "config.json"
GOOGLE_CLIENT = Path.home() / "Movies/Clips/tools/uploader/secrets/client_secret.json"
TIKTOK_REDIRECT = "https://hodrorik.github.io/clips-uploader/callback.html"


def load_conf():
    return json.loads(CONF.read_text()) if CONF.exists() else {}


def save_conf(c):
    CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    CONF.write_text(json.dumps(c, indent=2))
    CONF.chmod(0o600)


def need(c, *keys):
    missing = [k for k in keys if not c.get(k)]
    if missing:
        sys.exit(f"Run the earlier setup steps first (missing {', '.join(missing)}).")


def open_store(c):
    need(c, "dropboxAppKey", "dropboxAppSecret", "dropboxRefreshToken", "stateKey")
    dbx = Dropbox(c["dropboxAppKey"], c["dropboxAppSecret"], c["dropboxRefreshToken"])
    return dbx, TokenStore(dbx, c["stateKey"])


def note_account(dbx, platform, account, info):
    accts = dbx.read_json(ACCOUNTS, {})
    accts.setdefault(platform, {})[account] = {**info, "signedInAt": iso()}
    dbx.write_json(ACCOUNTS, accts)


def cmd_keygen(a, c):
    if c.get("stateKey") and not a.force:
        sys.exit("STATE_KEY already exists (use --force only if you mean to lose stored sign-ins).")
    c["stateKey"] = Fernet.generate_key().decode()
    save_conf(c)
    print("Saved. Add this as the GitHub secret STATE_KEY:\n" + c["stateKey"])


def cmd_dropbox(a, c):
    c["dropboxAppKey"] = input("Dropbox app key: ").strip()
    c["dropboxAppSecret"] = getpass.getpass("Dropbox app secret: ").strip()
    url = ("https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(
        {"client_id": c["dropboxAppKey"], "response_type": "code", "token_access_type": "offline"}))
    print(f"Open this, click Allow, copy the code:\n{url}")
    code = input("Code: ").strip()
    r = requests.post("https://api.dropboxapi.com/oauth2/token", timeout=30,
                      data={"grant_type": "authorization_code", "code": code},
                      auth=(c["dropboxAppKey"], c["dropboxAppSecret"]))
    r.raise_for_status()
    c["dropboxRefreshToken"] = r.json()["refresh_token"]
    save_conf(c)
    print("Saved. Add these GitHub secrets: DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN")
    print("(values are in ~/.clips_uploader/config.json)")


def cmd_meta(a, c):
    dbx, store = open_store(c)
    token = getpass.getpass("Meta system-user token (from Business settings > System users): ").strip()
    accounts = instagram.linked_accounts(token)
    store.put("meta", "system_user", {"token": token, "savedAt": iso()})
    store.save()
    print("Token stored. Instagram accounts it can post to:")
    for user, info in sorted(accounts.items()):
        print(f"  @{user}  (Page: {info['page']})")
    if not accounts:
        print("  none: link each Instagram account to a Page and assign the Pages to the system user.")


def cmd_instagram(a, c):
    dbx, store = open_store(c)
    meta = store.get("meta", "system_user")
    if not meta:
        sys.exit("Run `python auth.py meta` first.")
    info = instagram.linked_accounts(meta["token"]).get(a.username.lstrip("@").lower())
    if not info:
        sys.exit(f"@{a.username} isn't reachable: check its Page link and the system user's assets.")
    store.put("instagram", a.account, {"igUserId": info["igUserId"], "username": a.username.lstrip("@")})
    store.save()
    note_account(dbx, "instagram", a.account, {"username": a.username.lstrip("@"), "igUserId": info["igUserId"],
                                               "page": info["page"]})
    print(f"Stored @{a.username.lstrip('@')} (Page: {info['page']}) as '{a.account}'.")


def cmd_youtube(a, c):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from uploader.youtube import SCOPES
    if not GOOGLE_CLIENT.exists():
        sys.exit(f"Download the Google OAuth desktop client JSON to {GOOGLE_CLIENT} first.")
    dbx, store = open_store(c)
    flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    ch = build("youtube", "v3", credentials=creds, cache_discovery=False) \
        .channels().list(part="snippet", mine=True).execute()["items"][0]
    store.put("youtube", a.account, {"refreshToken": creds.refresh_token, "channelId": ch["id"]})
    store.save()
    note_account(dbx, "youtube", a.account, {"channelId": ch["id"], "title": ch["snippet"]["title"],
                                             "handle": ch["snippet"].get("customUrl")})
    client = json.loads(GOOGLE_CLIENT.read_text())["installed"]
    c["googleClientId"], c["googleClientSecret"] = client["client_id"], client["client_secret"]
    save_conf(c)
    print(f"Stored {ch['snippet']['title']} ({ch['snippet'].get('customUrl')}) as '{a.account}'.")
    print("If that's the wrong channel, re-run and pick the right Brand Account.")


def cmd_tiktok(a, c):
    if not c.get("tiktokClientKey"):
        c["tiktokClientKey"] = input("TikTok client key: ").strip()
        c["tiktokClientSecret"] = getpass.getpass("TikTok client secret: ").strip()
        save_conf(c)
    dbx, store = open_store(c)
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": c["tiktokClientKey"], "scope": "user.info.basic,video.upload",
        "response_type": "code", "redirect_uri": TIKTOK_REDIRECT, "state": state})
    print(f"Log in to the '{a.account}' TikTok in the browser first, then open:\n{url}")
    code = urllib.parse.unquote(input("Code shown on the callback page: ").strip())
    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", timeout=30,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      data={"client_key": c["tiktokClientKey"], "client_secret": c["tiktokClientSecret"],
                            "code": code, "grant_type": "authorization_code", "redirect_uri": TIKTOK_REDIRECT})
    d = r.json()
    if "access_token" not in d:
        sys.exit(f"TikTok sign-in failed: {d}")
    store.put("tiktok", a.account, {"accessToken": d["access_token"], "refreshToken": d["refresh_token"],
                                    "accessExpiresAt": now().timestamp() + d["expires_in"],
                                    "openId": d["open_id"], "scope": d.get("scope")})
    store.save()
    note_account(dbx, "tiktok", a.account, {"openId": d["open_id"], "scope": d.get("scope")})
    print(f"Stored TikTok '{a.account}' (scopes: {d.get('scope')}).")


def cmd_show(a, c):
    dbx, _ = open_store(c)
    print(json.dumps(dbx.read_json(ACCOUNTS, {}), indent=2))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("keygen").add_argument("--force", action="store_true")
    sub.add_parser("dropbox")
    sub.add_parser("meta")
    s = sub.add_parser("instagram")
    s.add_argument("--account", required=True)
    s.add_argument("--username", required=True)
    for name in ("youtube", "tiktok"):
        sub.add_parser(name).add_argument("--account", required=True)
    sub.add_parser("show")
    a = p.parse_args()
    c = load_conf()
    globals()[f"cmd_{a.cmd}"](a, c)


if __name__ == "__main__":
    main()
