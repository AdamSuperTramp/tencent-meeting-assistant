"""权限、改期、分项补查的合成数据回归测试。"""
import bootstrap
import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import business_export as service
import group_access
import meeting_delivery
import owner_only_bridge as bridge
import post_meeting_poller as poller
from test_t192u_start_gate import StartGateTests as GateBase


class ScheduleTests(GateBase):
    def test_reschedule_reopens_only_end_watch(self):
        state = {'version': 1, 'candidates': {'991001': {
            'status': 'a_gave_up', 'scheduled_start': '2026-11-09T08:00:00+08:00',
        }}}
        provenance = {'991001': {'mark': 'company', 'start_time': '2026-11-09T15:00:00+08:00'}}
        out = self._run(now=datetime.datetime(2026, 11, 9, 14, tzinfo=poller.TZ),
                        state=state, prov=provenance)
        self.assertEqual(out['gate']['mcp_calls_a'], 0)
        cand = json.loads(self.state_path.read_text())['candidates']['991001']
        self.assertEqual(cand['status'], 'watching')
        self.assertEqual(cand['a_deadline_at'], '2026-11-09T19:20:00+08:00')

    def test_failed_update_does_not_sync(self):
        response = {'result': {'content': [{'type': 'text', 'text': '{"error_info":{"code":42}}'}]}}
        with mock.patch.object(bridge.e, 'sync_local_meeting_schedule') as sync:
            self.assertFalse(bridge._sync_update_meeting_schedule(
                {'arguments': {'meeting_id': '991001', 'start_time': '2026-11-09T15:00:00+08:00'}},
                response))
            sync.assert_not_called()

    def test_successful_update_syncs(self):
        with mock.patch.object(bridge.e, 'sync_local_meeting_schedule', return_value={'ok': True}) as sync:
            self.assertTrue(bridge._sync_update_meeting_schedule(
                {'arguments': {'meeting_id': '991001', 'start_time': '2026-11-09T15:00:00+08:00'}},
                {'result': {'content': [{'type': 'text', 'text': '{}'}]}}))
            self.assertEqual(sync.call_args.kwargs['start_time'], '2026-11-09T15:00:00+08:00')


class AccessTests(unittest.TestCase):
    def test_added_group_rejected(self):
        cfg = json.loads(bridge.CONFIG.read_text())
        self.assertTrue(bridge.exclusive_owner(cfg))
        cfg['channels']['feishu']['groupAllowFrom'].append('oc_other')
        self.assertFalse(bridge.exclusive_owner(cfg))

    def test_missing_session_rejected_before_network(self):
        with mock.patch.object(bridge, 'guarded_rpc') as remote:
            with self.assertRaises(PermissionError):
                bridge.routed_rpc({'method': 'tools/call', 'params': {'name': 'get_meeting'}})
            remote.assert_not_called()

    def test_group_search_filters_private_and_totals(self):
        api = SimpleNamespace(BASE=bootstrap.TEST_DATA,
                              load_feishu_created_provenance=lambda: {'meetings': {}})
        payload = {'records': [{'meeting_id': '991001'}, {'meeting_id': '991002'}], 'total': 999}
        api._tool_call_payload = lambda _: payload
        with mock.patch.object(group_access, 'company_ids', return_value={'991001'}):
            result = group_access.route(api, 'search_records', {}, lambda: {})
        visible = json.loads(result['result']['content'][0]['text'])
        self.assertEqual(visible['records'], [{'meeting_id': '991001'}])
        self.assertNotIn('total', visible)

    def test_private_detail_rejected(self):
        with mock.patch.object(group_access, 'company_ids', return_value={'991001'}):
            with self.assertRaises(PermissionError):
                group_access.route(None, 'get_meeting', {'meeting_id': '991002'}, mock.Mock())

    def test_unknown_tool_rejected(self):
        with mock.patch.object(group_access, 'company_ids', return_value=set()):
            with self.assertRaises(PermissionError):
                group_access.route(None, 'get_record_transcript', {}, mock.Mock())


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cfg = self.base / 'automation.json'
        self.cfg.write_text(json.dumps({
            'enabled': True, 'send_enabled': True, 'mode': 'live', 'target_chat_id': 'oc_fixture',
        }))
        self.path = self.base / 'poller-state/post-meeting-poller-state.json'
        self.path.parent.mkdir()
        self.candidate = {'status': 'b_gave_up', 'a_ended_confirmed': True, 'b_components': {
            'transcript': {'status': 'verified'}, 'recording_link': {'status': 'not_ready'},
        }}
        self.write_state()
        self.api = SimpleNamespace(
            BASE=self.base, AI_GROUP_CHAT_ID='oc_fixture', OPENCLAW_AUTOMATION_CONFIG=self.cfg,
            resolve_delivery_target=lambda _: {'kind': 'group', 'chat_id': 'oc_fixture'},
            permitted=lambda _: {'mark': 'company'},
            load_feishu_created_provenance=lambda: {'meetings': {'991001': {
                'mark': 'company', 'created_from_chat_id': 'oc_fixture',
            }}},
            load_auto_send_ledger=lambda *args: (None, 'key', self.base/'ledger.json'),
            _atomic_json_write=service._atomic_json_write,
            mcp_call=mock.Mock(side_effect=AssertionError('unexpected network')),
            poller_deliver_component=mock.Mock(side_effect=self.deliver),
        )

    def write_state(self):
        self.path.write_text(json.dumps({'version': 1, 'candidates': {'991001': self.candidate}}))

    def deliver(self, mid, kind, target, component, checkpoint):
        component['status'] = 'verified'
        checkpoint()

    def test_status_is_read_only_and_separate(self):
        result = meeting_delivery.status(self.api, {'meeting_id': '991001'})
        self.assertEqual(result['components']['transcript']['label'], '已送达')
        self.assertEqual(result['components']['recording_link']['status'], 'not_ready')
        self.api.poller_deliver_component.assert_not_called()
        self.api.mcp_call.assert_not_called()

    def test_retry_only_missing_and_repeated_request_skips(self):
        args = {'meeting_id': '991001', 'kind': 'missing'}
        result = meeting_delivery.retry(self.api, args)
        self.assertEqual(result['tracking_status'], 'auto_sent')
        self.assertEqual(self.api.poller_deliver_component.call_count, 1)
        self.assertEqual(self.api.poller_deliver_component.call_args.args[1], 'recording_link')
        meeting_delivery.retry(self.api, args)
        self.assertEqual(self.api.poller_deliver_component.call_count, 1)

    def test_paused_rejects_retry(self):
        self.cfg.with_name('post-meeting-poller.PAUSED').touch()
        with self.assertRaises(PermissionError):
            meeting_delivery.retry(self.api, {'meeting_id': '991001', 'kind': 'missing'})
        self.api.poller_deliver_component.assert_not_called()

    def test_send_exception_preserves_uncertain_ledger(self):
        self.api.poller_deliver_component.side_effect = RuntimeError('unverified')
        self.api.load_auto_send_ledger = lambda *args: ({'result': {'message_id': 'om_fixture'}}, '', None)
        result = meeting_delivery.retry(self.api, {'meeting_id': '991001', 'kind': 'recording_link'})
        self.assertEqual(result['components']['recording_link']['status'], 'uncertain')

    def test_legacy_status_is_unknown(self):
        self.candidate.pop('b_components')
        self.write_state()
        result = meeting_delivery.status(self.api, {'meeting_id': '991001'})
        self.assertEqual(result['components']['transcript']['status'], 'unknown_legacy')

    def test_reserved_send_never_prepares_again(self):
        component = {}
        with mock.patch.object(service, 'load_auto_send_ledger', return_value=({'result': {}}, '', None)), \
             mock.patch.object(service, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(service, 'resolve_poller_target', return_value={'chat_id': 'oc_fixture'}), \
             mock.patch.object(service, 'prepare') as prepare:
            service.poller_deliver_component('991001', 'transcript', 'oc_fixture', component)
        self.assertEqual(component['status'], 'uncertain')
        prepare.assert_not_called()


class EntryTests(unittest.TestCase):
    def test_group_status_through_mcp_route(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with mock.patch.object(bridge.e, 'BASE', base), \
                 mock.patch.object(bridge.e, 'permitted', return_value={'mark': 'company'}), \
                 mock.patch.object(bridge.e, 'load_auto_send_ledger', return_value=(None, '', None)), \
                 mock.patch.object(bridge, 'guarded_rpc') as upstream:
                result = bridge.routed_rpc({
                    'method': 'tools/call',
                    'params': {
                        'name': 'get_meeting_delivery_status',
                        'arguments': {'meeting_id': '991001', 'target_chat_id': 'oc_fixture'},
                        '_meta': {'openclaw/feishu-request': {
                            'sessionKey': 'agent:main:feishu:group:oc_fixture',
                            'messageId': 'om_fixture',
                        }},
                    },
                })
                body = json.loads(result['result']['content'][0]['text'])
                self.assertEqual(body['components']['transcript']['status'], 'pending')
                upstream.assert_not_called()

    def test_pause_stops_shell_before_credentials_or_network(self):
        import os
        import subprocess
        flag = bootstrap.TEST_HOME / '.openclaw/automations/post-meeting-poller.PAUSED'
        flag.touch()
        runner = Path(__file__).resolve().parents[1] / 'scripts/run-poller.sh'
        result = subprocess.run(['/bin/bash', str(runner)],
                                env={**os.environ, 'HOME': str(bootstrap.TEST_HOME)},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_title_update_keeps_confirmed_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'provenance.json'
            path.write_text(json.dumps({'meetings': {'991001': {
                'start_time': '2026-11-09T15:00:00+08:00', 'subject': '合成会议',
            }}}))
            with mock.patch.object(service, 'FEISHU_CREATED_PROVENANCE', path), \
                 mock.patch.object(service, 'upsert_company_from_feishu_provenance'):
                service.record_feishu_assistant_created('991001', subject='合成会议改名')
            self.assertEqual(json.loads(path.read_text())['meetings']['991001']['start_time'],
                             '2026-11-09T15:00:00+08:00')


if __name__ == '__main__':
    unittest.main()
