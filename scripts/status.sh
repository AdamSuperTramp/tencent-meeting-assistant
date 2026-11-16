#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1])
local=json.loads((root/'config/runtime.local.json').read_text())
openclaw=json.loads(pathlib.Path(local['openclaw_config']).expanduser().read_text())
auto=pathlib.Path.home()/'.openclaw/automations/post-meeting-export.json'
cfg=json.loads(auto.read_text()) if auto.exists() else {}
f=openclaw.get('channels',{}).get('feishu',{})
print('项目:',root)
print('OpenClaw Feishu:', 'enabled' if f.get('enabled') else 'disabled', '| mode:', f.get('connectionMode'))
print('DM policy:',f.get('dmPolicy'),'| group policy:',f.get('groupPolicy'),'| allowlisted groups:',len(f.get('groupAllowFrom') or []))
print('会后自动化:',cfg.get('mode','not configured'),'| enabled:',cfg.get('enabled',False),'| send_enabled:',cfg.get('send_enabled',False))
print('轮询器暂停标记:',(pathlib.Path.home()/'.openclaw/automations/post-meeting-poller.PAUSED').exists())
print('数据目录存在:',pathlib.Path(local['data_dir']).expanduser().is_dir())
PY
launchctl list ai.openclaw.gateway 2>/dev/null || true
launchctl list ai.openclaw.post-meeting-poller 2>/dev/null || true
