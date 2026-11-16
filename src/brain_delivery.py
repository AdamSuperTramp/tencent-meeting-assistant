"""将已经准备好的会议逐字稿交给企业大脑，每份材料开启新会话。"""
import hashlib
import http.client
import json
import re
import time
import unicodedata
import uuid
from pathlib import Path

from settings import DATA_DIR, EXPORT_DIR, SECRETS_FILE


class Rejected(Exception):
    """服务明确拒绝请求，可以稍后重试。"""


def post(path, body, content_type, token):
    conn = http.client.HTTPSConnection("brain.grevoya.com", timeout=15)
    try:
        try:
            conn.connect()
        except OSError:
            raise Rejected("connection_unavailable") from None
        conn.request("POST", path, body, {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "Content-Type": content_type,
        })
        response = conn.getresponse()
        if response.status in (400, 401, 403, 404, 413, 415, 422, 429):
            raise Rejected("HTTP_" + str(response.status))
        if response.status != 200 and response.status != 201 and response.status != 202:
            raise ValueError("unexpected_http_status")
        raw = response.read().decode("utf-8")
        if response.getheader("Content-Type", "").startswith("text/event-stream"):
            for event in raw.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                if data:
                    obj = json.loads(data)
                    if obj.get("id") == 1:
                        return obj
            raise ValueError("missing_rpc_response")
        return json.loads(raw)
    finally:
        conn.close()


def transcript_file(mid, export_id):
    if not re.fullmatch(r"[a-f0-9]{24}", export_id):
        raise ValueError("invalid_export")
    manifest = json.loads((DATA_DIR / "exports" / (export_id + ".json")).read_text())
    name = manifest["file_name"]
    if (manifest["meeting_id"] != mid or manifest["kind"] != "transcript"
            or manifest["format"] != "md" or manifest["export_id"] != export_id
            or Path(name).name != name or not name.lower().endswith(".md")):
        raise ValueError("invalid_manifest")
    path = EXPORT_DIR / export_id / name
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("invalid_path")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
        raise ValueError("hash_mismatch")
    return name, content


def upload(name, content, token):
    boundary = uuid.uuid4().hex
    safe_name = "".join("_" if c in '"\\' or unicodedata.category(c) == "Cc" else c for c in name)
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\n逐字稿\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
              'Content-Type: text/markdown\r\n\r\n').encode()
    result = post("/mcp/upload", prefix + content + f"\r\n--{boundary}--\r\n".encode(),
                  "multipart/form-data; boundary=" + boundary, token)
    source = result.get("source_id")
    if not isinstance(source, str) or not re.fullmatch(r"src_[\w-]+", source):
        raise ValueError("missing_source_id")
    return source


def new_chat(source, token):
    args = {"message": "这是一份腾讯会议逐字稿，请按原文处理。", "source_ids": [source], "wait": False}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "chat", "arguments": args}}
    reply = post("/mcp", json.dumps(body).encode(), "application/json", token)
    result = reply.get("result") or {}
    if reply.get("id") != 1 or reply.get("error") or result.get("isError"):
        raise ValueError("chat_not_confirmed")
    receipt = result.get("structuredContent") or {}
    texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
    if not receipt and texts:
        try:
            receipt = json.loads(texts[0])
        except ValueError:
            pass
    if isinstance(receipt, dict) and receipt.get("conversation_id") and not receipt.get("error"):
        if receipt.get("status") in ("stopped", "failed", "已停止"):
            raise ValueError("chat_stopped")
        return str(receipt["conversation_id"])
    if any("已交给大脑" in t and "已停止" not in t for t in texts):
        return None
    raise ValueError("missing_chat_receipt")


def deliver(mid, export_id, job, save):
    """轮询锁内运行。外部写入前持久化，未收到确定回执时不重发。"""
    if job.get("status") in ("accepted", "uncertain", "uploading", "opening"):
        return
    if job.get("retry_at", 0) > time.time():
        return
    try:
        token = json.loads(SECRETS_FILE.read_text())["BRAIN_TOKEN"]
        if not isinstance(token, str) or not token.strip():
            raise ValueError("missing_token")
        if not job.get("source_id"):
            name, content = transcript_file(mid, export_id)
    except (OSError, ValueError, KeyError, TypeError):
        job.update(status="retry", reason="credential_or_export_unavailable", retry_at=time.time() + 300)
        save()
        return
    try:
        if not job.get("source_id"):
            job.update(status="uploading", export_id=export_id)
            save()
            job["source_id"] = upload(name, content, token)
            job["status"] = "uploaded"
            save()
        job["status"] = "opening"
        save()
        conv = new_chat(job["source_id"], token)
    except Rejected as exc:
        job.update(status="retry", reason=str(exc), retry_at=time.time() + 300)
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError):
        job.update(status="uncertain", reason="receipt_unconfirmed")
    else:
        job.update(status="accepted", conversation_id=conv, accepted_at=time.time())
        job.pop("reason", None)
        job.pop("retry_at", None)
    save()
