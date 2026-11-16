#!/usr/bin/env python3
"""Stdio MCP bridge to official Tencent Meeting MCP HTTP endpoint.
Reads TENCENT_MEETING_TOKEN from env or ~/.config/openclaw-pending/secrets.json.
Never logs the token. Unwraps the official skill HTTP envelope into MCP tool results.
"""
import json, os, sys, urllib.request
from pathlib import Path
from settings import SECRETS_FILE

ENDPOINT = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
SKILL_VERSION = "v1.1.0"

from tencent_client import rpc, token

def unwrap_content(result: dict) -> dict:
    """If tool result text is {status_code,headers,body}, promote body to JSON text."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return result
    new_content = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            new_content.append(block); continue
        text = block.get("text", "")
        try:
            envelope = json.loads(text)
        except Exception:
            new_content.append(block); continue
        if isinstance(envelope, dict) and "body" in envelope and "status_code" in envelope:
            body = envelope.get("body")
            if isinstance(body, (dict, list)):
                text = json.dumps(body, ensure_ascii=False)
            elif isinstance(body, str):
                # body may already be JSON text
                text = body
            new_content.append({"type": "text", "text": text})
        else:
            new_content.append(block)
    out = dict(result)
    out["content"] = new_content
    return out

def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                reply(mid, {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tencent-meeting-official", "version": SKILL_VERSION},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                remote = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
                reply(mid, remote.get("result", remote))
            elif method == "tools/call":
                remote = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
                result = unwrap_content(remote.get("result", remote))
                reply(mid, result)
            elif method == "ping":
                reply(mid, {})
            else:
                reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception:
            reply(mid, error={"code": -32000, "message": "tencent meeting bridge error"})

if __name__ == "__main__":
    main()
