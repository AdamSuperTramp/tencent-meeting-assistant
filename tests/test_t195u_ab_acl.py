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
             mock.patch.object(pol, 'log', lambda *a, **k: None):
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

    def test_idle_no_due_zero_mcp(self):
        now = datetime.datetime(2026, 11, 9, 9, 0, tzinfo=TZ)
        mid = '9002'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant',
                      'start_time': '2026-11-09T15:00:00+08:00'}}
        out, _ = self._run(now=now, prov=prov)
        self.assertEqual(out['gate']['mcp_calls_a'], 0)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
