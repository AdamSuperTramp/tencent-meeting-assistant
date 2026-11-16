#!/usr/bin/env python3
"""t219u offline: Judgment B deliver must reuse readiness — no second send_ready."""
from __future__ import annotations
import datetime
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / 'src'
sys.path.insert(0, str(BASE))
import bootstrap

import business_export as e
import post_meeting_poller as pol

TZ = datetime.timezone(datetime.timedelta(hours=8))


def _readiness(card='回顾: 测\n日期: 2026-11-10 12:00:00\n回顾文件：https://meeting.tencent.com/crm/Abc123XYZ'):
    return {
        'transcript': {'ok': True, 'file_name': '20261110120000-测-逐字稿文本.md', 'bytes': 12, 'error': None},
        'recording': {
            'ok': True,
            'view_address': 'https://meeting.tencent.com/crm/Abc123XYZ',
            'card_text': card,
            'meeting_record_id': 'r1',
            'time_line': None,
            'duration_text': None,
            'acl': {'ok': True, 'permission_degraded': False},
        },
        'both_ready': True,
        'minutes': {'ok': True, 'kind': 'minutes', 'skipped': True, 'error': None},
    }


class T219UDedupeSendReady(unittest.TestCase):
    def test_deliver_with_readiness_skips_send_ready(self):
        readiness = _readiness()
        prepared = {
            'exports': [{
                'export_id': 'a' * 24,
                'file_name': readiness['transcript']['file_name'],
                'meeting_id': '991219001',
                'kind': 'transcript',
            }]
        }
        calls = {'send_ready': 0}

        def boom_send_ready(*a, **k):
            calls['send_ready'] += 1
            raise AssertionError('send_ready must not be called when readiness provided')

        with mock.patch.object(e, 'resolve_poller_target', return_value={'cli_target': 'chat:oc_ai', 'chat_id': 'oc_ai', 'kind': 'group'}), \
             mock.patch.object(e, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(e, 'load_auto_send_ledger', return_value=(None, 'k', pathlib.Path('/tmp/x.json'))), \
             mock.patch.object(e, 'send_ready', side_effect=boom_send_ready), \
             mock.patch.object(e, 'prepare', return_value=prepared), \
             mock.patch.object(e, 'poller_send_prepared', return_value={'status': 'dry-run-would-send', 'dry_run': True}), \
             mock.patch.object(e, 'poller_send_text', return_value={'status': 'dry-run-would-send-text', 'dry_run': True, 'text_preview': readiness['recording']['card_text']}) as send_text:
            out = e.poller_deliver_transcript_and_recording_link(
                '991219001', 'oc_ai', dry_run=True, readiness=readiness, acl_cache={'verified_record_ids': ['r1']}
            )
        self.assertEqual(calls['send_ready'], 0)
        self.assertTrue(out['dry_run'])
        self.assertEqual(out['reason'], 'ok')
        self.assertEqual(out['recording_card'], readiness['recording']['card_text'])
        self.assertEqual(out['view_address'], readiness['recording']['view_address'])
        args, kwargs = send_text.call_args
        self.assertEqual(args[2], readiness['recording']['card_text'])
        self.assertIn('回顾:', args[2])
        self.assertIn('日期:', args[2])
        self.assertIn('回顾文件：', args[2])
        self.assertNotIn('录播链接', args[2])
        kinds = [w.get('kind') for w in out.get('would_send') or []]
        self.assertEqual(kinds, ['transcript', 'recording_link'])

    def test_deliver_without_readiness_still_calls_send_ready(self):
        readiness = _readiness()
        prepared = {
            'exports': [{
                'export_id': 'b' * 24,
                'file_name': readiness['transcript']['file_name'],
                'meeting_id': '991219002',
                'kind': 'transcript',
            }]
        }
        with mock.patch.object(e, 'resolve_poller_target', return_value={'cli_target': 'chat:oc_ai', 'chat_id': 'oc_ai', 'kind': 'group'}), \
             mock.patch.object(e, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(e, 'load_auto_send_ledger', return_value=(None, 'k', pathlib.Path('/tmp/y.json'))), \
             mock.patch.object(e, 'send_ready', return_value=readiness) as sr, \
             mock.patch.object(e, 'prepare', return_value=prepared), \
             mock.patch.object(e, 'poller_send_prepared', return_value={'status': 'dry-run-would-send', 'dry_run': True}), \
             mock.patch.object(e, 'poller_send_text', return_value={'status': 'dry-run-would-send-text', 'dry_run': True}):
            out = e.poller_deliver_transcript_and_recording_link('991219002', 'oc_ai', dry_run=True)
        self.assertEqual(sr.call_count, 1)
        self.assertEqual(out['reason'], 'ok')

    def test_process_candidate_passes_readiness_no_second_send_ready(self):
        readiness = _readiness()
        cfg = {
            'enabled': False, 'send_enabled': False, 'mode': 'dry-run',
            'judgment_b_interval_seconds': 300, 'judgment_b_max_attempts': 6,
            'judgment_b_first_delay_seconds': 300, 'target_chat_id': 'oc_ai',
            'record_acl': {'enabled': False},
        }
        state = {'version': 1, 'candidates': {}}
        mid = '991219003'
        cand = pol.candidate_record(state, mid)
        pol.init_b_window(cand, '2026-11-10T12:00:00+08:00')
        # Force due: set next_check_at in the past
        cand['next_check_at'] = '2026-11-10T12:00:00+08:00'
        now = datetime.datetime(2026, 11, 10, 12, 6, tzinfo=TZ)
        send_ready_calls = []
        deliver_kwargs = {}

        def fake_send_ready(m, acl_cache=None):
            send_ready_calls.append({'meeting_id': m, 'acl_cache': acl_cache})
            if isinstance(acl_cache, dict):
                acl_cache.setdefault('verified_record_ids', [])
                if 'r1' not in acl_cache['verified_record_ids']:
                    acl_cache['verified_record_ids'].append('r1')
            return readiness

        def fake_deliver(m, chat, dry_run=False, readiness=None, acl_cache=None):
            deliver_kwargs.update({
                'meeting_id': m, 'chat': chat, 'dry_run': dry_run,
                'readiness_is': readiness, 'acl_cache': acl_cache,
            })
            # Prove deliver path would not need another send_ready when readiness passed
            self.assertIs(readiness, readiness if readiness is not None else None)
            self.assertIsNotNone(readiness)
            self.assertTrue(readiness.get('both_ready'))
            return {
                'sent': False, 'dry_run': True, 'reason': 'ok',
                'file_names': [readiness['transcript']['file_name']],
                'would_send': [{'kind': 'transcript'}, {'kind': 'recording_link'}],
                'recording_card': readiness['recording']['card_text'],
                'view_address': readiness['recording']['view_address'],
            }

        with mock.patch.object(pol, 'now_sh', return_value=now), \
             mock.patch.object(pol, 'log', lambda *a, **k: None), \
             mock.patch.object(pol.e, 'send_ready', side_effect=fake_send_ready), \
             mock.patch.object(pol.e, 'poller_deliver_transcript_and_recording_link', side_effect=fake_deliver):
            out = pol.process_candidate(mid, {'end_time': '2026-11-10T12:00:00+08:00', 'subject': '测'},
                                        {'mark': 'company', 'reason': 'created_via_feishu_assistant'},
                                        cfg, state, force=False)
        self.assertEqual(len(send_ready_calls), 1)
        self.assertEqual(out.get('action'), 'dry_run_would_send')
        self.assertIs(deliver_kwargs['readiness_is'], readiness)
        self.assertIn('r1', (deliver_kwargs.get('acl_cache') or {}).get('verified_record_ids') or [])

    def test_three_line_card_unchanged_via_readiness(self):
        card = '回顾: 测试演示\n日期: 2026-11-09 22:29:44\n回顾文件：https://meeting.tencent.com/crm/FixtureReview01'
        readiness = _readiness(card)
        prepared = {'exports': [{'export_id': 'c' * 24, 'file_name': 'n.md', 'meeting_id': '1', 'kind': 'transcript'}]}
        with mock.patch.object(e, 'resolve_poller_target', return_value={'cli_target': 'chat:oc_ai', 'chat_id': 'oc_ai', 'kind': 'group'}), \
             mock.patch.object(e, 'is_feishu_assistant_created', return_value=True), \
             mock.patch.object(e, 'load_auto_send_ledger', return_value=(None, 'k', pathlib.Path('/tmp/z.json'))), \
             mock.patch.object(e, 'send_ready', side_effect=AssertionError('no send_ready')), \
             mock.patch.object(e, 'prepare', return_value=prepared), \
             mock.patch.object(e, 'poller_send_prepared', return_value={'status': 'dry-run-would-send', 'dry_run': True}), \
             mock.patch.object(e, 'poller_send_text', return_value={'status': 'dry-run-would-send-text', 'dry_run': True}) as st:
            out = e.poller_deliver_transcript_and_recording_link('1', 'oc_ai', dry_run=True, readiness=readiness)
        self.assertEqual(out['recording_card'], card)
        self.assertEqual(st.call_args.args[2], card)

    def test_pause_flags_still_off(self):
        cfg = json.loads((pathlib.Path.home() / '.openclaw/automations/post-meeting-export.json').read_text())
        self.assertFalse(cfg.get('enabled'))
        self.assertFalse(cfg.get('send_enabled'))
        self.assertFalse((cfg.get('record_acl') or {}).get('enabled'))
        self.assertTrue((pathlib.Path.home() / '.openclaw/automations/post-meeting-poller.PAUSED').exists())


if __name__ == '__main__':
    os.environ.pop('TENCENT_MEETING_TOKEN', None)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T219UDedupeSendReady)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
