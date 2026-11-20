"""企业大脑上传契约的离线回归：只传 file，chat 用 attachment_ids。"""
import bootstrap  # noqa: F401  # 隔离到合成目录后再加载生产模块
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brain_delivery as brain

NAME = '20261120205703-合成周会-逐字稿文本.md'
CONTENT = '# 合成逐字稿\n\n[00:00] 测试说话人：这是合成文本。\n'.encode()


class BrainDeliveryContract(unittest.TestCase):
    def test_material_message_describes_kind_and_date(self):
        self.assertEqual(
            brain.material_message(NAME),
            '这是一份腾讯会议逐字稿：2026-11-20 20:57 合成周会，请按原文处理。')

    def test_upload_sends_only_file_field(self):
        calls = []

        def fake_post(path, body, content_type, token):
            calls.append((path, body))
            return {'attachment_id': 'att_fixture_1'}

        with mock.patch.object(brain, 'post', side_effect=fake_post):
            attachment = brain.upload(NAME, CONTENT, 'token-fixture')
        self.assertEqual(attachment, 'att_fixture_1')
        path, body = calls[0]
        self.assertEqual(path, '/mcp/upload')
        self.assertNotIn(b'name="kind"', body)
        self.assertIn(b'name="file"', body)
        self.assertIn(CONTENT, body)

    def test_new_chat_uses_attachment_ids(self):
        calls = []

        def fake_post(path, body, content_type, token):
            calls.append(json.loads(body))
            return {'id': 1, 'result': {'structuredContent': {'conversation_id': 'cnv_fixture'}}}

        with mock.patch.object(brain, 'post', side_effect=fake_post):
            conv = brain.new_chat('att_fixture_1', 'token-fixture', '合成材料说明')
        self.assertEqual(conv, 'cnv_fixture')
        args = calls[0]['params']['arguments']
        self.assertEqual(args['attachment_ids'], ['att_fixture_1'])
        self.assertEqual(args['message'], '合成材料说明')
        self.assertFalse(args['wait'])
        self.assertNotIn('source_ids', args)

    def test_deliver_uploads_once_then_opens_chat(self):
        uploads = []

        def fake_post(path, body, content_type, token):
            if path == '/mcp/upload':
                uploads.append(body)
                return {'attachment_id': 'att_fixture_1'}
            return {'id': 1, 'result': {'structuredContent': {'conversation_id': 'cnv_fixture'}}}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            exports = root / 'exports'
            (data / 'exports').mkdir(parents=True)
            (exports / ('a' * 24)).mkdir(parents=True)
            (exports / ('a' * 24) / NAME).write_bytes(CONTENT)
            (data / 'exports' / ('a' * 24 + '.json')).write_text(json.dumps({
                'export_id': 'a' * 24, 'meeting_id': '991001', 'kind': 'transcript',
                'format': 'md', 'file_name': NAME,
                'sha256': hashlib.sha256(CONTENT).hexdigest(),
            }))
            secrets = root / 'secrets.json'
            secrets.write_text(json.dumps({'BRAIN_TOKEN': 'token-fixture'}))
            job = {}
            with mock.patch.object(brain, 'post', side_effect=fake_post), \
                 mock.patch.object(brain, 'DATA_DIR', data), \
                 mock.patch.object(brain, 'EXPORT_DIR', exports), \
                 mock.patch.object(brain, 'SECRETS_FILE', secrets):
                brain.deliver('991001', 'a' * 24, job, lambda: None)
        self.assertEqual(job['status'], 'accepted')
        self.assertEqual(job['attachment_id'], 'att_fixture_1')
        self.assertEqual(job['conversation_id'], 'cnv_fixture')
        self.assertEqual(len(uploads), 1)

    def test_deliver_reuses_attachment_without_reupload(self):
        def fake_post(path, body, content_type, token):
            if path == '/mcp/upload':
                raise AssertionError('must not upload again')
            return {'id': 1, 'result': {'structuredContent': {'conversation_id': 'cnv_fixture'}}}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data'
            (data / 'exports').mkdir(parents=True)
            (data / 'exports' / ('b' * 24 + '.json')).write_text(json.dumps({
                'export_id': 'b' * 24, 'meeting_id': '991002', 'kind': 'transcript',
                'format': 'md', 'file_name': NAME, 'sha256': 'unused',
            }))
            secrets = root / 'secrets.json'
            secrets.write_text(json.dumps({'BRAIN_TOKEN': 'token-fixture'}))
            job = {'attachment_id': 'att_fixture_2'}
            with mock.patch.object(brain, 'post', side_effect=fake_post), \
                 mock.patch.object(brain, 'DATA_DIR', data), \
                 mock.patch.object(brain, 'SECRETS_FILE', secrets):
                brain.deliver('991002', 'b' * 24, job, lambda: None)
        self.assertEqual(job['status'], 'accepted')
        self.assertEqual(job['attachment_id'], 'att_fixture_2')


if __name__ == '__main__':
    unittest.main()
