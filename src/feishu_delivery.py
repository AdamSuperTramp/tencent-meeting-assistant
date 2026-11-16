"""飞书投递后的消息回读与内容核验。"""
import json
import os
import subprocess


def read_message(message_id, *, node_bin, script):
    env = dict(os.environ)
    env["PATH"] = node_bin + ":/opt/homebrew/bin:/usr/bin:/bin"
    process = subprocess.run(
        [node_bin + "/node", str(script), message_id],
        capture_output=True, text=True, env=env, timeout=60,
    )
    try:
        return json.loads(process.stdout or "{}")
    except (ValueError, TypeError):
        return {}


def text_readback_ok(message, expected_text, destination):
    if not message or not message.get("ok"):
        return False
    if message.get("msg_type") not in ("text", "post"):
        return False
    if expected_text and expected_text not in (message.get("text_preview") or ""):
        return False
    if destination and destination.get("kind") == "group":
        return message.get("chat_id") == destination.get("chat_id")
    return True


def reconcile_file(result, destination, *, readback):
    message_id = result.get("message_id")
    if not message_id:
        raise RuntimeError("Delivery outcome unknown; no message ID to verify; no blind resend")
    message = readback(message_id)
    valid = (
        message.get("ok") and message.get("msg_type") == "file"
        and message.get("file_name") == result.get("file_name") and message.get("file_key")
        and (destination["kind"] != "group" or message.get("chat_id") == destination["chat_id"])
    )
    if not valid:
        raise RuntimeError("Existing send still unverified; no upload retry")
    return dict(
        result, status="file-message-readback-verified", file_key=message["file_key"],
        msg_type="file", readback_file_name=message["file_name"],
        readback_chat_id=message.get("chat_id"), readback_ok=True,
    )
