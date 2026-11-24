"""腾讯响应兼容回归：合成数据、模拟网络，不访问生产材料。"""
import bootstrap
import json
import unittest
from unittest import mock

import business_export as export
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


if __name__ == '__main__':
    unittest.main()
