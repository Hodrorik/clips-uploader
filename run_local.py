#!/usr/bin/env python3
"""Run the uploader on this Mac (same code the cloud runner uses), e.g. for a demo or a one-off post.

  .venv/bin/python run_local.py --clip TEST01 [--dry-run]
"""
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
c = json.loads((Path.home() / ".clips_uploader/config.json").read_text())
env = {"DROPBOX_APP_KEY": "dropboxAppKey", "DROPBOX_APP_SECRET": "dropboxAppSecret",
       "DROPBOX_REFRESH_TOKEN": "dropboxRefreshToken", "STATE_KEY": "stateKey",
       "GOOGLE_CLIENT_ID": "googleClientId", "GOOGLE_CLIENT_SECRET": "googleClientSecret",
       "TIKTOK_CLIENT_KEY": "tiktokClientKey", "TIKTOK_CLIENT_SECRET": "tiktokClientSecret"}
for name, key in env.items():
    if c.get(key):
        os.environ[name] = c[key]
sys.path.insert(0, str(Path(__file__).parent))
from uploader.run import main  # noqa: E402

sys.exit(main())
