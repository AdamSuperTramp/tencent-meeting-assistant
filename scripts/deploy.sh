#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/config/runtime.local.json"
if [[ ! -f "$LOCAL" ]]; then echo "缺少本机配置: $LOCAL" >&2; exit 2; fi
OPENCLAW_CONFIG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["openclaw_config"])' "$LOCAL")"
POLL_PLIST="$HOME/Library/LaunchAgents/ai.openclaw.post-meeting-poller.plist"
PROMPT_TARGET="$HOME/.openclaw/workspace/AGENTS.md"
PAUSE_FLAG="$HOME/.openclaw/automations/post-meeting-poller.PAUSED"
NOW="$(date +%Y%m%d%H%M%S)"
cp -p "$OPENCLAW_CONFIG" "$OPENCLAW_CONFIG.pre-tma-$NOW"
if [[ -f "$POLL_PLIST" ]]; then cp -p "$POLL_PLIST" "$POLL_PLIST.pre-tma-$NOW"; fi
if [[ -f "$PROMPT_TARGET" ]]; then cp -p "$PROMPT_TARGET" "$PROMPT_TARGET.pre-tma-$NOW"; fi
WAS_PAUSED=0
if [[ -f "$PAUSE_FLAG" ]]; then WAS_PAUSED=1; else umask 077; : > "$PAUSE_FLAG"; fi
python3 - "$ROOT" "$OPENCLAW_CONFIG" "$POLL_PLIST" <<'PY'
import json, os, pathlib, plistlib, sys, tempfile
root=pathlib.Path(sys.argv[1]); cfg_path=pathlib.Path(sys.argv[2]); plist_path=pathlib.Path(sys.argv[3])
cfg=json.loads(cfg_path.read_text())
server=cfg.get('mcp',{}).get('servers',{}).get('tencent-meeting')
if not isinstance(server,dict): raise SystemExit('OpenClaw MCP server tencent-meeting is missing')
server['command']=json.loads((root/'config/runtime.local.json').read_text())['python']
server['args']=[str(root/'src/owner_only_bridge.py')]
tmp=cfg_path.with_name(cfg_path.name+'.tmp')
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:
    f.write(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
    f.flush(); os.fsync(f.fileno())
os.chmod(tmp,0o600); os.replace(tmp,cfg_path)
plist={}
if plist_path.exists():
    with plist_path.open('rb') as f: plist=plistlib.load(f)
plist.update({
 'Label':'ai.openclaw.post-meeting-poller',
 'ProgramArguments':['/bin/bash',str(root/'scripts/run-poller.sh')],
 'WorkingDirectory':str(root),
 'RunAtLoad':True,
 'StartInterval':60,
 'StandardOutPath':str(pathlib.Path.home()/'Library/Logs/openclaw/post-meeting-poller.launchd.out.log'),
 'StandardErrorPath':str(pathlib.Path.home()/'Library/Logs/openclaw/post-meeting-poller.launchd.err.log'),
})
plist_path.parent.mkdir(parents=True,exist_ok=True)
with plist_path.open('wb') as f: plistlib.dump(plist,f,sort_keys=False)
os.chmod(plist_path,0o600)
PY
cp "$ROOT/prompts/assistant-rules.md" "$PROMPT_TARGET"
chmod 600 "$PROMPT_TARGET"
UID_NOW="$(id -u)"
launchctl kickstart -k "gui/$UID_NOW/ai.openclaw.gateway"
launchctl bootout "gui/$UID_NOW" "$POLL_PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NOW" "$POLL_PLIST"
if [[ "$WAS_PAUSED" -eq 0 ]]; then rm -f "$PAUSE_FLAG"; fi
echo "已部署。配置备份后缀: pre-tma-$NOW"
