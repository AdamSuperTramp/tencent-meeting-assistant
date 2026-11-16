#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/config/runtime.local.json"
[[ -f "$LOCAL" ]] || { echo "缺少本机配置" >&2; exit 2; }
OPENCLAW_CONFIG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["openclaw_config"])' "$LOCAL")"
POLL_PLIST="$HOME/Library/LaunchAgents/ai.openclaw.post-meeting-poller.plist"
PROMPT_TARGET="$HOME/.openclaw/workspace/AGENTS.md"
PAUSE_FLAG="$HOME/.openclaw/automations/post-meeting-poller.PAUSED"
SUFFIX="${1:-}"
[[ -n "$SUFFIX" ]] || { echo "用法: $0 YYYYMMDDHHMMSS" >&2; exit 2; }
[[ -f "$OPENCLAW_CONFIG.pre-tma-$SUFFIX" ]] || { echo "找不到 OpenClaw 配置备份" >&2; exit 2; }
[[ -f "$POLL_PLIST.pre-tma-$SUFFIX" ]] || { echo "找不到 LaunchAgent 备份" >&2; exit 2; }
[[ -f "$PROMPT_TARGET.pre-tma-$SUFFIX" ]] || { echo "找不到助手规则备份" >&2; exit 2; }
cp -p "$OPENCLAW_CONFIG" "$OPENCLAW_CONFIG.pre-rollback-$(date +%Y%m%d%H%M%S)"
cp -p "$POLL_PLIST" "$POLL_PLIST.pre-rollback-$(date +%Y%m%d%H%M%S)"
WAS_PAUSED=0
if [[ -f "$PAUSE_FLAG" ]]; then WAS_PAUSED=1; else umask 077; : > "$PAUSE_FLAG"; fi
cp -p "$OPENCLAW_CONFIG.pre-tma-$SUFFIX" "$OPENCLAW_CONFIG"
cp -p "$POLL_PLIST.pre-tma-$SUFFIX" "$POLL_PLIST"
cp -p "$PROMPT_TARGET.pre-tma-$SUFFIX" "$PROMPT_TARGET"
UID_NOW="$(id -u)"
launchctl kickstart -k "gui/$UID_NOW/ai.openclaw.gateway"
launchctl bootout "gui/$UID_NOW" "$POLL_PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NOW" "$POLL_PLIST"
if [[ "$WAS_PAUSED" -eq 0 ]]; then rm -f "$PAUSE_FLAG"; fi
echo "已回滚至 pre-tma-$SUFFIX"
