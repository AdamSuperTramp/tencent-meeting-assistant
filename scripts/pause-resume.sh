#!/bin/bash
set -euo pipefail
FLAG="$HOME/.openclaw/automations/post-meeting-poller.PAUSED"
mkdir -p "$(dirname "$FLAG")"
case "${1:-}" in
  pause)
    umask 077
    : > "$FLAG"
    echo "会后自动化已暂停；LaunchAgent 下次唤醒时会退出。"
    ;;
  resume)
    rm -f "$FLAG"
    launchctl kickstart "gui/$(id -u)/ai.openclaw.post-meeting-poller" 2>/dev/null || true
    echo "已移除暂停标记；会后自动化将在下一轮运行。"
    ;;
  *) echo "用法: $0 pause|resume" >&2; exit 2 ;;
esac
