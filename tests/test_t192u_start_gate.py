#!/usr/bin/env python3
"""t192u mock tests: start+20m gate before any MCP; no external I/O."""
from __future__ import annotations
import datetime
import json
import os
import sys
import tempfile
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / 'src'
sys.path.insert(0, str(BASE))
import bootstrap

import post_meeting_poller as pol


TZ = datetime.timezone(datetime.timedelta(hours=8))


class StartGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = pathlib.Path(self.tmp.name)
        self.state_path = self.td / 'state.json'
        self.cfg = {
            'enabled': False,
            'send_enabled': False,
            'mode': 'dry-run',
            'judgment_a_start_delay_minutes': 20,
            'global_interval_seconds': 600,
            'lookback_hours': 12,
            'timeout_hours_after_end': 12,
            'backoff_seconds': [60, 300, 900],
            'target_chat_id': 'oc_test',
            'allow_null_created_from_chat': True,
        }
        self.prov = {}
        self.registry = {}
        self.calls = []

        def mcp(name, args):
            self.calls.append((name, dict(args)))
            raise AssertionError('unexpected mcp ' + name)

        self.mcp = mcp

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *, now, prov=None, registry=None, state=None, mcp=None, force=None):
        st = state or {'version': 1, 'candidates': {}}
        with mock.patch.object(pol, 'now_sh', return_value=now), \
             mock.patch.object(pol, 'load_config', return_value=(self.cfg, pathlib.Path('cfg'))), \
             mock.patch.object(pol, 'load_state', return_value=st), \
             mock.patch.object(pol, 'atomic_state_write', lambda data: self.state_path.write_text(json.dumps(data))), \
             mock.patch.object(pol, 'load_registry_meetings', return_value=registry if registry is not None else self.registry), \
             mock.patch.object(pol.e, 'load_feishu_created_provenance', return_value={'meetings': prov if prov is not None else self.prov}), \
             mock.patch.object(pol.e, 'mcp_call', mcp or self.mcp), \
             mock.patch.object(pol, 'log', lambda *a, **k: None):
            return pol.run_once(force_meeting_id=force)

    def test_no_meetings_zero_mcp(self):
        now = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=TZ)
        out = self._run(now=now, prov={}, registry={})
        self.assertEqual(self.calls, [])
        self.assertEqual(out['gate']['mcp_calls_a'], 0)

    def test_future_start_zero_mcp(self):
        now = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=TZ)
        mid = '111'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant', 'start_time': '2026-11-09T15:00:00+08:00', 'subject': 'future'}}
        out = self._run(now=now, prov=prov)
        self.assertEqual(self.calls, [])
        self.assertEqual(out['gate']['mcp_calls_a'], 0)
        self.assertTrue(any(x['meeting_id'] == mid for x in out['gate']['too_early']))

    def test_within_20m_zero_mcp(self):
        now = datetime.datetime(2026, 11, 9, 15, 10, tzinfo=TZ)  # start 15:00 +10m <20
        mid = '222'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant', 'start_time': '2026-11-09T15:00:00+08:00'}}
        out = self._run(now=now, prov=prov)
        self.assertEqual(out['gate']['mcp_calls_a'], 0)
        self.assertEqual(self.calls, [])

    def test_missing_start_blocks_no_mcp(self):
        now = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=TZ)
        mid = '333'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant', 'subject': 'no-start'}}
        out = self._run(now=now, prov=prov)
        self.assertIn(mid, out['gate']['blockers_no_start'])
        self.assertEqual(self.calls, [])

    def test_after_20m_calls_get_meeting_only(self):
        now = datetime.datetime(2026, 11, 9, 15, 25, tzinfo=TZ)  # start+25m
        mid = '444'
        prov = {mid: {'mark': 'company', 'reason': 'created_via_feishu_assistant', 'start_time': '2026-11-09T15:00:00+08:00', 'subject': 'due'}}
        def mcp(name, args):
            self.calls.append((name, dict(args)))
            self.assertEqual(name, 'get_meeting')
            self.assertEqual(args['meeting_id'], mid)
            return {'meeting_info_list': [{'meeting_id': mid, 'live_status': '进行中', 'subject': 'due', 'start_time': '2026-11-09T15:00:00+08:00', 'end_time': '2026-11-09T16:00:00+08:00'}]}
        out = self._run(now=now, prov=prov, mcp=mcp)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'get_meeting')
        self.assertTrue(any(r.get('action') == 'a_not_ended' for r in out['results']))

    def test_scheduled_end_time_alone_not_ended(self):
        meta = {'meeting_id': '1', 'end_time': '2026-11-09T16:00:00+08:00', 'start_time': '2026-11-09T15:00:00+08:00'}
        ok, why = pol.explicit_meeting_ended(meta)
        self.assertFalse(ok)
        self.assertEqual(why, 'no_explicit_ended_status')

    def test_explicit_ended_status(self):
        ok, why = pol.explicit_meeting_ended({'live_status': '已结束', 'end_time': '2026-11-09T16:00:00+08:00'})
        self.assertTrue(ok)


if __name__ == '__main__':
    unittest.main()
