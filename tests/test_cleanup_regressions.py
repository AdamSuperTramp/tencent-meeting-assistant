"""关键边界的离线回归，使用合成会议和模拟接口。"""
import bootstrap
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import business_export as e
import owner_only_bridge as bridge
import post_meeting_poller as poller


class CleanupRegressions(unittest.TestCase):
    def test_all_group_metadata_forms_deny_private_export(self):
        for key, field in (('openclaw/feishu-request', 'sessionKey'),
                           ('feishu-request', 'sessionKey'),
                           ('openclaw/feishu-request', 'session_key')):
            with self.subTest(key=key, field=field), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                (base / 'registry.json').write_text(json.dumps({
                    'version': 1, 'meetings': {'991001': {'mark': 'private', 'record_file_ids': []}}
                }))
                with mock.patch.object(bridge.e, 'BASE', base), \
                     mock.patch.object(bridge.e, 'is_feishu_assistant_created', return_value=False):
                    result = bridge.routed_rpc({'method': 'tools/call', 'params': {
                        'name': 'prepare_meeting_export',
                        'arguments': {'meeting_id': '991001', 'kind': 'minutes', 'format': 'md'},
                        '_meta': {key: {field: 'agent:main:feishu:group:oc_fixture'}},
                    }})
                self.assertTrue(result['result']['isError'])

    def test_manual_recording_reconciles_without_resending(self):
        url = 'https://meeting.tencent.com/crm/Fixture123'
        picked = {'ok': True, 'view_address': url, 'manual_card_text': '回顾文件：' + url}
        dest = {'kind': 'group', 'chat_id': 'oc_fixture', 'cli_target': 'chat:oc_fixture'}
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(e, 'AUTO_SEND_LEDGER', Path(folder)), \
             mock.patch.object(e, 'resolve_delivery_target', return_value=dest), \
             mock.patch.object(e, 'permitted', return_value={'mark': 'company'}), \
             mock.patch.object(e, 'pick_cloud_recording_for_share', return_value=picked), \
             mock.patch.object(e, 'read_feishu_message', side_effect=[
                 {'ok': False}, {'ok': True, 'msg_type': 'text', 'chat_id': 'oc_fixture', 'text_preview': url}
             ]), \
             mock.patch.object(e.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stdout='{"message_id":"om_fixture"}')) as send:
            token = e.REQUEST_CONTEXT.set({'sessionKey': 'agent:main:feishu:group:oc_fixture',
                                           'messageId': 'om_request'})
            try:
                with self.assertRaises(RuntimeError):
                    e.deliver_recording_link({'meeting_id': '991001'})
                result = e.deliver_recording_link({'meeting_id': '991001'})
            finally:
                e.REQUEST_CONTEXT.reset(token)
        self.assertTrue(result['idempotent_replay'])
        self.assertEqual(send.call_count, 1)

    def test_legacy_dual_delivery_is_not_resent(self):
        ledger = {'result': {'status': 'file-message-readback-verified'}}
        state = {'candidates': {'991001': {'status': 'partial'}}}
        with mock.patch.object(poller.e, 'load_auto_send_ledger', return_value=(ledger, '', None)), \
             mock.patch.object(poller, 'atomic_state_write'), \
             mock.patch.object(poller.e, 'poller_deliver_component') as send:
            result = poller.process_candidate('991001', {}, {}, {'target_chat_id': 'oc_fixture'}, state)
        self.assertEqual(result['action'], 'legacy_auto_sent')
        send.assert_not_called()

    def test_brain_checkpoint_merges_concurrent_poller_state(self):
        import fcntl
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            state_path = base / 'poller-state' / 'post-meeting-poller-state.json'
            state_path.parent.mkdir()
            state = {'candidates': {'991001': {
                'a_ended_confirmed': True,
                'b_components': {'transcript': {'ready_payload': {'export_id': 'a' * 24}}},
            }}}
            state_path.write_text(json.dumps(state))
            cfg = {'enabled': True, 'send_enabled': True, 'mode': 'live',
                   'target_chat_id': 'oc_fixture', 'brain_delivery_since': '2020-01-01T00:00:00+08:00'}
            provenance = {'991001': {'mark': 'company', 'created_at': '2026-01-01T00:00:00+08:00',
                                     'created_from_chat_id': 'oc_fixture'}}
            registry = {'991001': {'mark': 'company'}}

            def deliver(mid, export_id, job, save):
                with (state_path.parent / 'poller.run.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    current = json.loads(state_path.read_text())
                    current['candidates'][mid]['concurrent_change'] = 'preserved'
                    state_path.write_text(json.dumps(current))
                job['status'] = 'accepted'
                save()

            with mock.patch.object(poller, 'BASE', base), \
                 mock.patch.object(poller, 'STATE_PATH', state_path), \
                 mock.patch.object(poller, 'CONFIG_PATH', base / 'config.json'), \
                 mock.patch.object(poller, 'DEFAULT_TARGET', 'oc_fixture'), \
                 mock.patch.object(poller.e, 'allowed_group_chat_ids', return_value={'oc_fixture'}), \
                 mock.patch.object(poller.brain_delivery, 'deliver', side_effect=deliver), \
                 mock.patch.object(poller, 'log'):
                poller.deliver_ready_brain_transcripts(cfg, state, provenance, registry)
            candidate = json.loads(state_path.read_text())['candidates']['991001']
            self.assertEqual(candidate['concurrent_change'], 'preserved')
            self.assertEqual(candidate['brain_delivery']['status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
