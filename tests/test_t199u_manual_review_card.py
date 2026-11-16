#!/usr/bin/env python3
"""Offline mock: t199u shared auto+manual 3-line review card. Stubs all network/MCP/Feishu sends."""
import datetime
import json
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / 'src'
sys.path.insert(0, str(BASE))
import bootstrap

import business_export as e


TZ = datetime.timezone(datetime.timedelta(hours=8))


class FakeProc:
    def __init__(self, returncode=0, stdout='{}', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_pick_mcp(subject, record_start, record_end, urls, meeting_start=None):
    """urls: list of view_address strings returned by address()."""
    def mcp_call(name, args):
        if name == 'get_meeting':
            return {
                'meeting_info_list': [{
                    'subject': subject,
                    'start_time': meeting_start,
                    'records': [{'meeting_record_id': '111', 'type': '云录制', 'media_start_time': record_start, 'duration': None}],
                }]
            }
        if name == 'search_records':
            return {
                'records': [{
                    'meeting_record_id': '111',
                    'record_file_id': '222',
                    'file_type': 'video',
                    'record_start_time': record_start,
                    'record_end_time': record_end,
                    'duration_seconds': None,
                }]
            }
        if name == 'record_access' and args.get('action') == 'address':
            return {'record_files': [{'view_address': u} for u in urls]}
        raise AssertionError('unexpected mcp_call %s %s' % (name, args))
    return mcp_call


class T199UManualReviewCard(unittest.TestCase):
    def test_format_helper_three_lines(self):
        dt = datetime.datetime(2026, 11, 9, 22, 29, 44, tzinfo=TZ)
        card = e.format_manual_review_card('测试演示会议', dt, 'https://meeting.tencent.com/crm/FixtureReview01')
        self.assertEqual(
            card,
            '回顾: 测试演示会议\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crm/FixtureReview01',
        )
        self.assertEqual(card.count('\n'), 2)

    def test_url_accepts_crm_and_crw_no_rewrite(self):
        self.assertTrue(e.is_stable_recording_share_url('https://meeting.tencent.com/crm/FixtureReview01'))
        self.assertTrue(e.is_stable_recording_share_url('https://meeting.tencent.com/crw/AbCdEf123'))
        self.assertFalse(e.is_crm_review_share_url('https://meeting.tencent.com/crw/AbCdEf123'))
        # must not treat rewritten fantasy as automatic
        self.assertTrue(e.is_stable_recording_share_url('https://meeting.tencent.com/crw/AbCdEf123'))

    def test_pick_prefers_real_crm_and_manual_card(self):
        start = '2026-11-09T22:29:44+08:00'
        end = '2026-11-09T23:00:00+08:00'
        mcp = make_pick_mcp(
            '测试演示会议', start, end,
            urls=[
                'https://meeting.tencent.com/crw/OnlyCrwXxx',
                'https://meeting.tencent.com/crm/FixtureReview01',
            ],
        )
        with mock.patch.object(e, 'mcp_call', mcp):
            picked = e.pick_cloud_recording_for_share('123')
        self.assertTrue(picked['ok'])
        self.assertEqual(picked['url_kind'], 'crm')
        self.assertEqual(picked['view_address'], 'https://meeting.tencent.com/crm/FixtureReview01')
        self.assertEqual(picked['date_source'], 'record_start_time')
        self.assertEqual(picked['manual_date'], '2026-11-09 22:29:44')
        expected = (
            '回顾: 测试演示会议\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crm/FixtureReview01'
        )
        self.assertEqual(picked['manual_card_text'], expected)
        self.assertEqual(picked['card_text'], expected)
        self.assertNotIn('录播链接', picked['card_text'])
        self.assertNotIn('时长', picked['card_text'])

    def test_pick_crw_only_boundary_keeps_crw(self):
        start = '2026-11-09T22:29:44+08:00'
        end = '2026-11-09T23:00:00+08:00'
        mcp = make_pick_mcp('Demo', start, end, urls=['https://meeting.tencent.com/crw/OnlyCrwXxx'])
        with mock.patch.object(e, 'mcp_call', mcp):
            picked = e.pick_cloud_recording_for_share('123')
        self.assertEqual(picked['url_kind'], 'crw')
        self.assertEqual(picked['view_address'], 'https://meeting.tencent.com/crw/OnlyCrwXxx')
        self.assertIn('https://meeting.tencent.com/crw/OnlyCrwXxx', picked['manual_card_text'])
        self.assertNotIn('/crm/', picked['manual_card_text'])

    def test_missing_date_fails(self):
        mcp = make_pick_mcp('Demo', None, None, urls=['https://meeting.tencent.com/crm/Abc123xyz'], meeting_start=None)
        with mock.patch.object(e, 'mcp_call', mcp):
            picked = e.pick_cloud_recording_for_share('123')
        self.assertFalse(picked.get('ok'))
        self.assertEqual(picked.get('error'), 'missing_actual_record_time_window')

    def test_meeting_start_fallback_date_source(self):
        mcp = make_pick_mcp(
            'Demo', None, None,
            urls=['https://meeting.tencent.com/crm/Abc123xyz'],
            meeting_start='2026-11-09T22:29:44+08:00',
        )
        with mock.patch.object(e, 'mcp_call', mcp):
            picked = e.pick_cloud_recording_for_share('123')
        self.assertTrue(picked['ok'])
        self.assertEqual(picked['date_source'], 'meeting.start_time')
        self.assertEqual(picked['manual_date'], '2026-11-09 22:29:44')
        # shared template: meeting.start_time is enough for both auto+manual cards
        expected = (
            '回顾: Demo\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crm/Abc123xyz'
        )
        self.assertEqual(picked['card_text'], expected)
        self.assertEqual(picked['manual_card_text'], expected)
        self.assertIsNone(picked.get('auto_card_blocked'))

    def test_deliver_manual_sends_three_lines_and_noreply(self):
        start = '2026-11-09T22:29:44+08:00'
        end = '2026-11-09T23:00:00+08:00'
        mcp = make_pick_mcp('测试演示会议', start, end, urls=['https://meeting.tencent.com/crm/FixtureReview01'])
        sent = {}

        def fake_run(cmd, **kwargs):
            cmd = list(cmd)
            if '--message' in cmd:
                i = cmd.index('--message')
                sent['card'] = cmd[i+1]
                sent['target'] = cmd[cmd.index('--target')+1] if '--target' in cmd else None
                return FakeProc(0, json.dumps({'message_id': 'om_testmanual001'}))
            # readback node script
            return FakeProc(0, json.dumps({
                'ok': True,
                'msg_type': 'text',
                'text_preview': sent.get('card', ''),
                'chat_id': 'oc_grouptest',
            }))

        token = e.REQUEST_CONTEXT.set({'messageId': 'om_inbound_unique_t199u_unify_%s' % __import__('time').time_ns()})
        try:
            with mock.patch.object(e, 'mcp_call', mcp), \
                 mock.patch.object(e, 'resolve_delivery_target', return_value={'kind': 'group', 'chat_id': 'oc_grouptest', 'cli_target': 'chat:oc_grouptest'}), \
                 mock.patch.object(e, 'permitted', return_value={'mark': 'company'}), \
                 mock.patch('subprocess.run', side_effect=fake_run):
                out = e.deliver_recording_link({'meeting_id': '991199001', 'target_chat_id': 'oc_grouptest'})
        finally:
            e.REQUEST_CONTEXT.reset(token)
        self.assertTrue(out['verified'])
        self.assertFalse(out.get('idempotent_replay'))
        self.assertEqual(out.get('reply_instruction'), 'NO_REPLY')
        self.assertIn('card', sent)
        lines = sent['card'].split('\n')
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], '回顾: 测试演示会议')
        self.assertEqual(lines[1], '日期: 2026-11-09 22:29:44')
        self.assertEqual(lines[2], '回顾文件：https://meeting.tencent.com/crm/FixtureReview01')
        self.assertNotIn('时长', sent['card'])
        self.assertNotIn('录播链接', sent['card'])

    def test_auto_and_manual_cards_identical_three_lines(self):
        start = '2026-11-09T22:29:44+08:00'
        end = '2026-11-09T23:00:00+08:00'
        mcp = make_pick_mcp('AutoKeep', start, end, urls=['https://meeting.tencent.com/crw/AutoCrw1'])
        with mock.patch.object(e, 'mcp_call', mcp):
            picked = e.pick_cloud_recording_for_share('999')
        expected = (
            '回顾: AutoKeep\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crw/AutoCrw1'
        )
        self.assertEqual(picked['card_text'], expected)
        self.assertEqual(picked['manual_card_text'], expected)
        self.assertNotIn('录播链接', picked['card_text'])
        self.assertNotIn('时长', picked['card_text'])

    def test_auto_ready_uses_shared_card_then_may_fail_acl(self):
        """With meeting.start_time only, card_text is ready; readiness may still fail later (ACL/state)."""
        mcp = make_pick_mcp(
            'Demo', None, None,
            urls=['https://meeting.tencent.com/crm/Abc123xyz'],
            meeting_start='2026-11-09T22:29:44+08:00',
        )
        with mock.patch.object(e, 'mcp_call', mcp), \
             mock.patch.object(e, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(e, 'permitted', return_value={'mark': 'company'}), \
             mock.patch.object(e, 'ensure_and_verify_recording_acl', return_value={'ok': True, 'verified': True}):
            ready = e.recording_share_ready('123')
        self.assertTrue(ready.get('ok'))
        self.assertTrue(ready['card_text'].startswith('回顾: Demo'))
        self.assertEqual(ready['card_text'], ready['manual_card_text'])
        self.assertNotIn('录播链接', ready['card_text'])

    def test_auto_dry_run_would_send_three_line_card(self):
        start = '2026-11-09T22:29:44+08:00'
        end = '2026-11-09T23:00:00+08:00'
        expected = (
            '回顾: AutoKeep\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crm/AutoCrm1'
        )
        readiness = {
            'both_ready': True,
            'transcript': {'ok': True, 'file_name': '20261109222944-AutoKeep-逐字稿文本.md', 'bytes': 12},
            'recording': {
                'ok': True,
                'card_text': expected,
                'manual_card_text': expected,
                'view_address': 'https://meeting.tencent.com/crm/AutoCrm1',
                'meeting_record_id': '111',
            },
            'minutes': {'ok': True, 'skipped': True},
        }
        prepared = {'exports': [{'file_name': readiness['transcript']['file_name'], 'export_id': 'exp1'}]}
        with mock.patch.object(e, 'resolve_poller_target', return_value={'cli_target': 'chat:oc_ai', 'chat_id': 'oc_ai', 'kind': 'group'}), \
             mock.patch.object(e, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(e, 'load_auto_send_ledger', return_value=(None, 'k', pathlib.Path('/tmp/x.json'))), \
             mock.patch.object(e, 'send_ready', return_value=readiness), \
             mock.patch.object(e, 'prepare', return_value=prepared), \
             mock.patch.object(e, 'poller_send_prepared', return_value={'status': 'dry-run-would-send', 'dry_run': True}), \
             mock.patch.object(e, 'poller_send_text', return_value={'status': 'dry-run-would-send-text', 'dry_run': True, 'text_preview': expected}) as send_text:
            out = e.poller_deliver_transcript_and_recording_link('991199002', 'oc_ai', dry_run=True)
        self.assertTrue(out['dry_run'])
        self.assertEqual(out['recording_card'], expected)
        self.assertEqual(out['view_address'], 'https://meeting.tencent.com/crm/AutoCrm1')
        # ensure the text passed to send is the three-line card
        args, kwargs = send_text.call_args
        self.assertEqual(args[2], expected)
        self.assertNotIn('录播链接', args[2])
        self.assertNotIn('时长', args[2])
        # no minutes auto-send in this path
        kinds = [w.get('kind') for w in out.get('would_send') or []]
        self.assertIn('transcript', kinds)
        self.assertIn('recording_link', kinds)
        self.assertNotIn('minutes', kinds)

    def test_pause_state_untouched(self):
        cfg = json.loads((pathlib.Path.home()/'.openclaw/automations/post-meeting-export.json').read_text())
        self.assertFalse(cfg.get('enabled'))
        self.assertFalse(cfg.get('send_enabled'))
        self.assertFalse((cfg.get('record_acl') or {}).get('enabled'))
        self.assertTrue((pathlib.Path.home()/'.openclaw/automations/post-meeting-poller.PAUSED').exists())


if __name__ == '__main__':
    # Ensure no real token path is required
    os.environ.pop('TENCENT_MEETING_TOKEN', None)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T199UManualReviewCard)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
