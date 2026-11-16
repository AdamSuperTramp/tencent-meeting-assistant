"""Redirect every test process to an isolated synthetic host directory."""
from pathlib import Path
import json
import os
import tempfile
import atexit
import shutil

TEST_ROOT = Path(tempfile.mkdtemp(prefix='tencent-meeting-assistant-tests-'))
TEST_HOME = TEST_ROOT / 'home'
(TEST_HOME / '.openclaw/automations').mkdir(parents=True)
(TEST_HOME / 'Library/Logs/openclaw').mkdir(parents=True)
(TEST_HOME / '.config').mkdir(parents=True)
TEST_DATA = TEST_ROOT / 'data'
TEST_DATA.mkdir()
TEST_ARCHIVE = TEST_ROOT / 'archive'
TEST_ARCHIVE.mkdir()
TEST_EXPORTS = TEST_ROOT / 'exports'
TEST_EXPORTS.mkdir()
(TEST_HOME / '.openclaw/openclaw.json').write_text(json.dumps({
    'channels': {'feishu': {
        'enabled': True, 'dmPolicy': 'allowlist', 'allowFrom': ['ou_fixture'],
        'groupPolicy': 'allowlist', 'groupAllowFrom': ['oc_fixture'], 'requireMention': True,
    }},
    'agents': {'entries': {'main': {'tools': {
        'allow': ['bundle-mcp', 'feishu_app_scopes'],
        'deny': ['group:runtime', 'group:fs', 'message', 'sessions_spawn', 'sessions_send', 'gateway', 'nodes', 'browser', 'cron'],
    }}}},
}))
(TEST_HOME / '.openclaw/automations/post-meeting-export.json').write_text(json.dumps({
    'enabled': False, 'send_enabled': False, 'mode': 'dry-run', 'target_chat_id': 'oc_fixture',
    'record_acl': {'enabled': False},
}))
(TEST_HOME / '.openclaw/automations/post-meeting-poller.PAUSED').touch()
(TEST_HOME / '.config/secrets.json').write_text('{}')

for key, value in {
    'TMA_DATA_DIR': TEST_DATA,
    'TMA_SOURCE_ARCHIVE_DIR': TEST_ARCHIVE,
    'TMA_EXPORT_DIR': TEST_EXPORTS,
    'TMA_OPENCLAW_CONFIG': TEST_HOME / '.openclaw/openclaw.json',
    'TMA_OPENCLAW_AUTOMATION_CONFIG': TEST_HOME / '.openclaw/automations/post-meeting-export.json',
    'TMA_OPENCLAW_LOG_DIR': TEST_HOME / 'Library/Logs/openclaw',
    'TMA_OPENCLAW_CLI': '/usr/bin/false',
    'TMA_NODE_BIN': '/usr/bin',
    'TMA_PYTHON': '/usr/bin/python3',
    'TMA_OWNER_OPEN_ID': 'ou_fixture',
    'TMA_TRUSTED_GROUP_IDS': '["oc_fixture"]',
    'TMA_AUTO_SEND_GROUP_ID': 'oc_fixture',
    'TMA_FEISHU_NODE_SDK_ENTRY': '',
    'TMA_SECRETS_FILE': TEST_HOME / '.config/secrets.json',
}.items():
    os.environ[key] = str(value)

# Tests that use Path.home() are redirected as well, never to a real user profile.
Path.home = classmethod(lambda cls: TEST_HOME)

atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)
