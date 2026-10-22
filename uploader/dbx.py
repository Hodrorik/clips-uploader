"""Minimal Dropbox API client: refresh-token auth, temp links, download, JSON read/write."""
import json
import time

import requests

API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"


class DropboxError(RuntimeError):
    pass


class Dropbox:
    def __init__(self, app_key, app_secret, refresh_token):
        self.app_key, self.app_secret, self.refresh_token = app_key, app_secret, refresh_token
        self._access, self._expires = None, 0

    def _token(self):
        if not self._access or time.time() > self._expires - 60:
            r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token",
                                               "refresh_token": self.refresh_token},
                              auth=(self.app_key, self.app_secret), timeout=30)
            if r.status_code != 200:
                raise DropboxError(f"token refresh failed: {r.status_code} {r.text[:200]}")
            d = r.json()
            self._access, self._expires = d["access_token"], time.time() + d.get("expires_in", 14400)
        return self._access

    def _rpc(self, endpoint, payload):
        r = requests.post(f"{API}/{endpoint}", json=payload, timeout=60,
                          headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 409:
            return None  # path not found / conflict: caller decides
        if r.status_code != 200:
            raise DropboxError(f"{endpoint}: {r.status_code} {r.text[:200]}")
        return r.json()

    def temp_link(self, path):
        """Public, single-file link valid for 4 hours (what Instagram fetches the video from)."""
        d = self._rpc("files/get_temporary_link", {"path": path})
        if d is None:
            raise DropboxError(f"not found: {path}")
        return d["link"]

    def list_folder(self, path):
        d = self._rpc("files/list_folder", {"path": path})
        if d is None:
            return []
        entries = d["entries"]
        while d.get("has_more"):
            d = self._rpc("files/list_folder/continue", {"cursor": d["cursor"]})
            entries += d["entries"]
        return [e for e in entries if e[".tag"] == "file"]

    def _download(self, path, stream=False):
        r = requests.post(f"{CONTENT}/files/download", stream=stream, timeout=600,
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Dropbox-API-Arg": json.dumps({"path": path})})
        if r.status_code == 409:
            return None
        if r.status_code != 200:
            raise DropboxError(f"download {path}: {r.status_code} {r.text[:200]}")
        return r

    def download(self, path, dest):
        r = self._download(path, stream=True)
        if r is None:
            raise DropboxError(f"not found: {path}")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8 * 1024 * 1024):
                f.write(chunk)
        return dest

    def read_bytes(self, path):
        r = self._download(path)
        return None if r is None else r.content

    def write_bytes(self, path, data):
        arg = {"path": path, "mode": "overwrite", "mute": True}
        r = requests.post(f"{CONTENT}/files/upload", data=data, timeout=120,
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Dropbox-API-Arg": json.dumps(arg),
                                   "Content-Type": "application/octet-stream"})
        if r.status_code != 200:
            raise DropboxError(f"upload {path}: {r.status_code} {r.text[:200]}")

    def read_json(self, path, default):
        b = self.read_bytes(path)
        return default if b is None else json.loads(b)

    def write_json(self, path, data):
        self.write_bytes(path, json.dumps(data, indent=2, ensure_ascii=False).encode())
