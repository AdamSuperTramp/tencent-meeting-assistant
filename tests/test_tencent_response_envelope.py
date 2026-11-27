"""腾讯响应兼容回归：合成数据、模拟网络，不访问生产材料。"""
import bootstrap
import json
import unittest
from unittest import mock
from pathlib import Path
import tempfile

import business_export as export
import owner_only_bridge as owner
import post_meeting_poller as poller
import tencent_meeting_mcp_bridge as bridge
from tencent_client import PollerQuotaError


def response(payload, *, wrapped=False, encoded=True, status=200):
    envelope = {'status_code': status,
                'body': json.dumps(payload, ensure_ascii=False) if encoded else payload}
    if wrapped:
        envelope = {'_meta': {'hints': ['合成版本更新提示']}, 'data': envelope}
    return {'content': [{'type': 'text', 'text': json.dumps(envelope, ensure_ascii=False)}]}


class TencentResponseEnvelopeTests(unittest.TestCase):
    def call_export(self, result):
        with mock.patch.object(export, 'tencent_rpc', return_value={'result': result}), \
             mock.patch.object(export, 'mcp_token', return_value='synthetic-token'):
            return export.mcp_call('get_meeting', {'meeting_id': '999000111222'})

    def test_ended_meeting_reaches_poller_and_bridge_in_both_formats(self):
        payload = {'meeting_info_list': [{'meeting_id': '999000111222',
                    'subject': '合成会议', 'status': '已结束仍可入会'}]}
        for wrapped in (False, True):
            for encoded in (False, True):
                with self.subTest(wrapped=wrapped, encoded=encoded):
                    result = response(payload, wrapped=wrapped, encoded=encoded)
                    parsed = self.call_export(result)
                    self.assertEqual(parsed, payload)
                    self.assertTrue(poller.explicit_meeting_ended(parsed['meeting_info_list'][0])[0])
                    promoted = bridge.unwrap_content(result)
                    self.assertEqual(json.loads(promoted['content'][0]['text']), payload)

    def test_business_errors_are_preserved(self):
        payload = {'error_info': {'error_code': 100, 'message': '合成错误'}}
        self.assertEqual(self.call_export(response(payload, wrapped=True, status=403)), payload)

    def test_quota_inside_notice_still_raises(self):
        with self.assertRaises(PollerQuotaError):
            self.call_export(response({'error_info': {'error_code': 500246}}, wrapped=True))

    def test_empty_http_error_still_raises(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 503'):
            self.call_export(response('', wrapped=True, encoded=False, status=503))

    def test_ordinary_data_field_is_not_unwrapped(self):
        payload = {'_meta': {}, 'data': {'items': ['合成业务数据']}}
        result = {'content': [{'type': 'text', 'text': json.dumps(payload)}]}
        self.assertEqual(self.call_export(result), payload)
        self.assertEqual(bridge.unwrap_content(result), result)

    def test_bridge_preserves_other_content_and_error_marker(self):
        result = response({'error_info': '合成错误'}, wrapped=True)
        result['isError'] = True
        result['content'].append({'type': 'image', 'data': 'synthetic'})
        promoted = bridge.unwrap_content(result)
        self.assertTrue(promoted['isError'])
        self.assertEqual(promoted['content'][1], result['content'][1])


class OwnerResponseEnvelopeTests(unittest.TestCase):
    def test_created_meeting_is_registered_and_eligible_in_both_formats(self):
        mid = '999000111222'
        start = '2026-11-27T21:00:00+08:00'
        payload = {'meeting_info_list': [{'meeting_id': mid, 'subject': '合成会议',
                                         'start_time': start}]}
        params = {'name': 'schedule_meeting', 'arguments': {'start_time': start},
                  '_meta': {'openclaw/feishu-request': {
                      'sessionKey': 'agent:main:feishu:group:oc_fixture'}}}
        for wrapped in (False, True):
            for encoded in (False, True):
                with self.subTest(wrapped=wrapped, encoded=encoded), tempfile.TemporaryDirectory() as folder:
                    base = Path(folder)
                    (base / 'registry.json').write_text(json.dumps({'version': 1, 'meetings': {}}))
                    reply = {'result': response(payload, wrapped=wrapped, encoded=encoded)}
                    with mock.patch.object(owner.e, 'BASE', base), \
                         mock.patch.object(owner.e, 'FEISHU_CREATED_PROVENANCE', base / 'created.json'), \
                         mock.patch.object(owner, 'guard'), mock.patch.object(owner, 'audit'), \
                         mock.patch.object(owner, 'guarded_rpc', return_value=reply) as remote:
                        self.assertEqual(owner.routed_rpc({'method': 'tools/call', 'params': params}), reply)
                        remote.assert_called_once()
                        provenance = owner.e.load_feishu_created_provenance()['meetings']
                        registry = json.loads((base / 'registry.json').read_text())['meetings']
                        self.assertIn(mid, provenance)
                        self.assertEqual(registry[mid]['mark'], 'company')
                        self.assertEqual(provenance[mid]['created_from_chat_id'], 'oc_fixture')
                        self.assertEqual(poller.local_start_time(mid, provenance[mid], registry[mid]).isoformat(), start)
                        self.assertTrue(poller.eligible_for_auto_send(mid, provenance[mid],
                                        {'target_chat_id': 'oc_fixture'})[0])
                        self.assertIn(mid, poller.collect_known_company_mids(provenance, registry, {}))

    def test_wrapped_update_uses_confirmed_response_schedule(self):
        reply = {'result': response({'meeting_info_list': [{'meeting_id': '999000111222',
                                  'start_time': '2026-11-27T22:00:00+08:00'}]}, wrapped=True)}
        with mock.patch.object(owner.e, 'sync_local_meeting_schedule', return_value={'ok': True}) as sync:
            self.assertTrue(owner._sync_update_meeting_schedule(
                {'arguments': {'meeting_id': '999000111222', 'start_time': '2026-11-27T21:00:00+08:00'}}, reply))
            self.assertEqual(sync.call_args.kwargs['start_time'], '2026-11-27T22:00:00+08:00')

    def test_wrapped_failures_never_register_or_update(self):
        for status, payload in ((403, {}), (200, {'error_info': {'code': 42}}),
                                (503, {'meeting_info_list': [{'meeting_id': '999000111222'}]})):
            with self.subTest(status=status, payload=payload), \
                 mock.patch.object(owner.e, 'record_feishu_assistant_created') as create, \
                 mock.patch.object(owner.e, 'sync_local_meeting_schedule') as update:
                reply = {'result': response(payload, wrapped=True, status=status)}
                params = {'arguments': {'meeting_id': '999000111222', 'start_time': '2026-11-27T21:00:00+08:00'}}
                self.assertIsNone(owner._tool_call_payload(reply))
                self.assertFalse(owner._record_schedule_meeting_company(params, reply))
                self.assertFalse(owner._sync_update_meeting_schedule(params, reply))
                create.assert_not_called()
                update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
