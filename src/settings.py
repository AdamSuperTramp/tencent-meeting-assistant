"""Local host settings for the Tencent Meeting Assistant.

Sensitive identifiers and machine paths live in config/runtime.local.json, which is
ignored by Git. Secret values remain in the OpenClaw secrets file.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = PROJECT_ROOT / "src"
LOCAL_CONFIG = PROJECT_ROOT / "config" / "runtime.local.json"
_defaults = {
    "data_dir": str(Path.home() / ".private-tencent-inventory/privacy-policy-v1"),
    "source_archive_dir": str(Path.home() / ".private-tencent-inventory/2026-11-07"),
    "export_dir": str(Path.home() / ".openclaw/media/tencent-owner-exports"),
    "openclaw_config": str(Path.home() / ".openclaw/openclaw.json"),
    "openclaw_automation_config": str(Path.home() / ".openclaw/automations/post-meeting-export.json"),
    "openclaw_log_dir": str(Path.home() / "Library/Logs/openclaw"),
    "openclaw_cli": str(Path.home() / "Projects/openclaw/node_modules/.bin/openclaw"),
    "node_bin": str(Path.home() / ".nvm/versions/node/v24.22.1/bin"),
    "python": "/opt/homebrew/opt/python@3.12/libexec/bin/python3",
    "owner_open_id": "",
    "auto_send_group_id": "",
    "feishu_node_sdk_entry": "",
    "secrets_file": str(Path.home() / ".config/openclaw-pending/secrets.json"),
}
if LOCAL_CONFIG.exists():
    _defaults.update(json.loads(LOCAL_CONFIG.read_text()))

def _path(key: str) -> Path:
    env_key = "TMA_" + key.upper()
    return Path(os.environ.get(env_key, _defaults[key])).expanduser()

DATA_DIR = _path("data_dir")
SOURCE_ARCHIVE_DIR = _path("source_archive_dir")
EXPORT_DIR = _path("export_dir")
OPENCLAW_CONFIG = _path("openclaw_config")
OPENCLAW_AUTOMATION_CONFIG = _path("openclaw_automation_config")
OPENCLAW_LOG_DIR = _path("openclaw_log_dir")
OPENCLAW_CLI = str(_path("openclaw_cli"))
NODE_BIN = str(_path("node_bin"))
PYTHON = str(_path("python"))
OWNER_OPEN_ID = os.environ.get("TMA_OWNER_OPEN_ID", _defaults["owner_open_id"])
AUTO_SEND_GROUP_ID = os.environ.get("TMA_AUTO_SEND_GROUP_ID", _defaults["auto_send_group_id"])
FEISHU_NODE_SDK_ENTRY = str(_path("feishu_node_sdk_entry"))
SECRETS_FILE = _path("secrets_file")
