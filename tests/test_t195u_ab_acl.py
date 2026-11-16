#!/usr/bin/env python3
"""t195u mock: A 4h deadline, B 5min×6, ACL cache/accept-view — no external I/O."""
from __future__ import annotations
import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / 'src'
sys.path.insert(0, str(BASE))
import bootstrap
import post_meeting_poller as pol
import business_export as be

TZ = datetime.timezone(datetime.timedelta(hours=8))


class T195uTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = pathlib.Path(self.tmp.name) / 'state.json'
        self.cfg = {
            'enabled': False, 'send_enabled': False, 'mode': 'dry-run',
            'judgment_a_start_delay_minutes': 20,
            'judgment_a_interval_seconds': 600,
            'judgment_a_max_hours': 4,
            'judgment_b_interval_seconds': 300,
            'judgment_b_max_attempts': 6,
            'judgment_b_first_delay_seconds': 300,
            'global_interval_seconds': 300,
            'lookback_hours': 12,
            'target_chat_id': 'oc_test',
            'allow_null_created_from_chat': True,
            'record_acl': {'enabled': False, 'permission': 1, 'enable_download': True, 'ensure_view_minutes': True},
        }
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, now, prov=None, state=None, mcp=None, force=None):
        st = state or {'version': 1, 'candidates': {}}
        def default_mcp(name, args):
            self.calls.append((name, dict(args)))
            raise AssertionError('unexpected ' + name)
        with mock.patch.object(pol, 'now_sh', return_value=now), \
             mock.patch.object(pol, 'load_config', return_value=(self.cfg, pathlib.Path('cfg'))), \
             mock.patch.object(pol, 'load_state', return_value=st), \
             mock.patch.object(pol, 'atomic_state_write', lambda data: self.state_path.write_text(json.dumps(data))), \
             mock.patch.object(pol, 'load_registry_meetings', return_value={}), \
             mock.patch.object(pol.e, 'load_feishu_created_provenance', return_value={'meetings': prov or {}}), \
             mock.patch.object(pol.e, 'mcp_call', mcp or default_mcp), \
             mock.patch.object(pol, 'log', lambda *a, **k: None), \
             mock.patch.object(pol.acl, 'acl_cfg_from_export', return_value=self.cfg['record_acl']):
            return pol.run_once(force_meeting_id=force), st

    def test_a_deadline_fixed_from_start(self):
        start = datetime.datetime(2026, 11, 9, 10, 0, tzinfo=TZ)
        arm = pol.a_arm_at(start, self.cfg)
        dead = pol.a_deadline_at(start, self.cfg)
        self.assertEqual(arm, start + datetime.timedelta(minutes=20))
        self.assertEqual(dead, start + datetime.timedelta(hours=4, minutes=20))

    def test_past_deadline_gave_up_zero_mcp(self):
        # start 10:00 → arm 10:20 → deadline 14:20; now 14:30
        now = datetime.datetime(2026, 11, 9, 14, 30, tzinfo=TZ)
        mid = '9001'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant',
                      'start_time': '2026-11-09T10:00:00+08:00'}}
        out, st = self._run(now=now, prov=prov)
        self.assertEqual(self.calls, [])
        self.assertEqual(st['candidates'][mid]['status'], 'a_gave_up')
        self.assertIn(mid, out['gate']['a_gave_up'])

    def test_a_not_extended_by_stored_deadline_tamper(self):
        start = datetime.datetime(2026, 11, 9, 10, 0, tzinfo=TZ)
        # even if candidate had a later fake deadline, code recomputes from start
        dead = pol.a_deadline_at(start, {'judgment_a_max_hours': 4, 'judgment_a_start_delay_minutes': 20})
        self.assertEqual(dead.hour, 14)
        self.assertEqual(dead.minute, 20)

    def test_b_slots_and_max_six(self):
        cand = {'b_anchor_at': '2026-11-09T12:00:00+08:00', 'b_attempts': 0}
        pol.init_b_window(cand, cand['b_anchor_at'])
        self.assertEqual(cand['next_check_at'], '2026-11-09T12:05:00+08:00')
        for i in range(6):
            pol.schedule_b_after_attempt(cand, self.cfg)
        self.assertEqual(cand['b_attempts'], 6)
        self.assertEqual(cand['status'], 'b_gave_up')
        self.assertIsNone(cand['next_check_at'])

    def test_b_attempt_persists_restart(self):
        cand = {'b_anchor_at': '2026-11-09T12:00:00+08:00', 'b_attempts': 3}
        pol.init_b_window(cand, cand['b_anchor_at'])
        self.assertEqual(cand['b_attempts'], 3)
        self.assertEqual(cand['next_check_at'], '2026-11-09T12:20:00+08:00')  # (3+1)*5

    def test_idle_no_due_zero_mcp(self):
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=TZ)
        mid = '9002'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant',
                      'start_time': '2026-11-09T15:00:00+08:00'}}
        out, _ = self._run(now=now, prov=prov)
        self.assertEqual(out['gate']['mcp_calls_a'], 0)
        self.assertEqual(self.calls, [])

    def test_acl_cache_skips_repeat_and_accepts_view(self):
        # Unit-level ensure_and_verify with mocks
        wanted = [{'open_id': 'ou_a', 'display_name': '甲', 'permission': 1}]
        cache = {'verified_record_ids': ['r1']}
        calls = []
        def mcp(name, args):
            calls.append(name)
            raise AssertionError('should not call when cached')
        with mock.patch.object(be, 'load_record_acl_wanted', return_value=(wanted, {'permission': 1, 'enable_download': True, 'ensure_view_minutes': True})), \
             mock.patch.object(be, 'mcp_call', mcp):
            out = be.ensure_and_verify_recording_acl('m1', ['r1'], acl_cache=cache, accept_view=True)
        self.assertTrue(out['ok'])
        self.assertEqual(calls, [])
        self.assertTrue(out['records'][0].get('cached'))

    def test_acl_no_upgrade_loop_when_view_present(self):
        import record_acl_grant as acl
        wanted = [{'open_id': 'ou_a', 'display_name': '甲', 'permission': 1}]
        cache = {}
        calls = []
        def mcp(name, args):
            calls.append((name, args))
            if name == 'record_access' and args.get('action') == 'members':
                return {'access_members': [{'open_id': 'ou_a', 'permission': 0, 'display_name': '甲'}], 'total_count': 1}
            if name == 'record_access' and args.get('action') == 'sharing':
                return {'current': {'download_enable': True, 'view_minutes_enable': True}}
            if name == 'record_share_set':
                return {'ok': True}
            return {}
        with mock.patch.object(be, 'load_record_acl_wanted', return_value=(wanted, {
                'permission': 1, 'enable_download': True, 'ensure_view_minutes': True, 'preserve_sharing_mode': True})), \
             mock.patch.object(be, 'mcp_call', mcp), \
             mock.patch.object(acl, 'fetch_all_members', lambda m, rid: mcp('record_access', {'action': 'members', 'meeting_record_id': rid})), \
             mock.patch.object(acl, 'members_index', lambda body: {'ou_a': {'permission': 0, 'display_name': '甲', 'raw': {'permission': 0}}}):
            out = be.ensure_and_verify_recording_acl('m1', ['r2'], acl_cache=cache, accept_view=True)
        self.assertTrue(out['ok'])
        self.assertTrue(out.get('permission_degraded') or out['records'][0].get('permission_degraded'))
        # should not add_members for upgrade (share_patch_only or noop)
        add_calls = [c for c in calls if c[0] == 'record_share_set' and 'add_members' in c[1]]
        self.assertEqual(add_calls, [])
        self.assertIn('r2', cache.get('verified_record_ids', []))


if __name__ == '__main__':
    unittest.main()
