"""腾讯会议 HTTP 传输和限流识别。"""
import json
import os
import urllib.request

from settings import SECRETS_FILE

ENDPOINT = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
SKILL_VERSION = "v1.1.0"


class PollerQuotaError(RuntimeError):
    """腾讯会议调用额度不足。"""


def token():
    value = os.environ.get("TENCENT_MEETING_TOKEN", "").strip()
    if not value:
        value = json.loads(SECRETS_FILE.read_text()).get("TENCENT_MEETING_TOKEN", "")
    if not value:
        raise RuntimeError("TENCENT_MEETING_TOKEN missing")
    return value


def rpc(body, *, timeout=60, access_token=None):
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Tencent-Meeting-Token": access_token or token(),
            "X-Skill-Version": SKILL_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def check_quota(value):
    if "500246" in json.dumps(value, ensure_ascii=False):
        raise PollerQuotaError("quota500246")


def unwrap_version_notice(value):
    """只剥离版本提示包装，保留 HTTP 状态和原始业务响应。"""
    if isinstance(value, dict) and isinstance(value.get("_meta"), dict):
        data = value.get("data")
        if isinstance(data, dict) and "status_code" in data and "body" in data:
            return data
    return value
