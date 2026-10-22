"""Paths, config and the encrypted token store, all kept in Dropbox under /Clips/uploader."""
import datetime as dt
import json
import os

from cryptography.fernet import Fernet

from .dbx import Dropbox

BASE = os.environ.get("UPLOADER_DROPBOX_DIR", "/Clips/uploader")
QUEUE_DIR = os.environ.get("UPLOADER_QUEUE_DIR", f"{BASE}/queue")  # run_local.py --local uses /local
LEDGER = f"{BASE}/ledger.json"
ACCOUNTS = f"{BASE}/accounts.json"   # non-secret: account keys -> ids, handles
TOKENS = f"{BASE}/tokens.enc"        # secret: Fernet-encrypted JSON


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def parse(ts):
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without UTC offset: {ts}")
    return t


def env(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Missing environment variable {name}")
    return v


def dropbox_from_env():
    return Dropbox(env("DROPBOX_APP_KEY"), env("DROPBOX_APP_SECRET"), env("DROPBOX_REFRESH_TOKEN"))


class TokenStore:
    """{platform: {account_key: {...token fields...}}}, encrypted with STATE_KEY."""

    def __init__(self, dbx, key):
        self.dbx, self.f = dbx, Fernet(key.encode() if isinstance(key, str) else key)
        raw = dbx.read_bytes(TOKENS)
        self.data = json.loads(self.f.decrypt(raw)) if raw else {}
        self.dirty = False

    def get(self, platform, account):
        return self.data.get(platform, {}).get(account)

    def put(self, platform, account, value):
        self.data.setdefault(platform, {})[account] = value
        self.dirty = True

    def save(self):
        if self.dirty:
            self.dbx.write_bytes(TOKENS, self.f.encrypt(json.dumps(self.data).encode()))
            self.dirty = False
