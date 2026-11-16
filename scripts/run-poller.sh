#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export TMA_PROJECT_ROOT="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$(python3 -c 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); print(json.loads(p.read_text()).get("node_bin",""))' "$ROOT/config/runtime.local.json"):/opt/homebrew/bin:/usr/bin:/bin"
SECRETS="$(python3 -c 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); print(json.loads(p.read_text()).get("secrets_file",""))' "$ROOT/config/runtime.local.json")"
if [[ -f "$SECRETS" ]]; then
  export TENCENT_MEETING_TOKEN="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("TENCENT_MEETING_TOKEN", ""))' "$SECRETS")"
fi
if [[ -f "$HOME/.openclaw/service-env/ai.openclaw.gateway.env" ]]; then
  set +u
  # shellcheck disable=SC1090
  source "$HOME/.openclaw/service-env/ai.openclaw.gateway.env" || true
  set -u
fi
PYTHON="$(python3 -c 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); print(json.loads(p.read_text()).get("python","/usr/bin/python3"))' "$ROOT/config/runtime.local.json")"
exec "$PYTHON" "$ROOT/src/post_meeting_poller.py" --once
