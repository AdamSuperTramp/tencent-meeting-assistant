#!/usr/bin/env python3
"""t279u offline: ACL failure must not block send once transcript + recording share are ready."""
from __future__ import annotations
import datetime
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
VIEW = "https://meeting.tencent.com/crm/Abc123XYZ"
CARD = "回顾: Demo\n日期: 2026-11-13 21:00:00\n回顾文件：https://meeting.tencent.com/crm/Abc123XYZ"


def _picked():
    return {
        "ok": True,
        "subject": "Demo",
        "meeting_id": "123",
        "meeting_record_id": "r1",
        "view_address": VIEW,
        "card_text": CARD,
        "manual_card_text": CARD,
        "all_record_ids_for_acl": ["r1"],
        "url_kind": "crm",
        "date_source": "meeting.start_time",
        "time_line": None,
        "duration_text": None,
    }


def _meeting_info(state="已完成"):
    return {"meeting_info_list": [{"records": [{"meeting_record_id": "r1", "state": state}]}]}


class T279UAclNotBlockSend(unittest.TestCase):
    def test_recording_share_ready_ok_when_acl_fails(self):
        with mock.patch.object(e, "is_feishu_assistant_created", return_value=True), \
             mock.patch.object(e, "permitted", return_value={"mark": "company"}), \
             mock.patch.object(e, "pick_cloud_recording_for_share", return_value=_picked()), \
             mock.patch.object(e, "mcp_call", return_value=_meeting_info()), \
             mock.patch.object(e, "ensure_and_verify_recording_acl", return_value={"ok": False, "error": "members_incomplete", "records": []}):
            ready = e.recording_share_ready("123", acl_cache={"verified_record_ids": []})
        self.assertTrue(ready.get("ok"))
        self.assertTrue(ready.get("acl_not_verified"))
        self.assertTrue(ready.get("acl_degraded"))
        self.assertEqual(ready["view_address"], VIEW)
        self.assertEqual(ready["card_text"], CARD)
        self.assertIsNone(ready.get("error"))

    def test_still_blocks_when_records_transcoding(self):
        with mock.patch.object(e, "is_feishu_assistant_created", return_value=True), \
             mock.patch.object(e, "permitted", return_value={"mark": "company"}), \
             mock.patch.object(e, "pick_cloud_recording_for_share", return_value=_picked()), \
             mock.patch.object(e, "mcp_call", return_value=_meeting_info("转码中")), \
             mock.patch.object(e, "ensure_and_verify_recording_acl") as grant:
            ready = e.recording_share_ready("123")
        self.assertFalse(ready.get("ok"))
        self.assertEqual(ready.get("error"), "records_not_ready")
        grant.assert_not_called()

    def test_send_ready_both_ready_when_acl_fails(self):
        rec = dict(_picked())
        rec.update({"ok": True, "acl_not_verified": True, "acl_degraded": True, "acl": {"ok": False}})
        with mock.patch.object(e, "material_ready", return_value={"ok": True, "file_name": "20261113205612-Demo-逐字稿文本.md", "error": None}), \
             mock.patch.object(e, "recording_share_ready", return_value=rec):
            out = e.send_ready("123", acl_cache={"verified_record_ids": []})
        self.assertTrue(out["transcript"]["ok"])
        self.assertTrue(out["recording"]["ok"])
        self.assertTrue(out["both_ready"])

    def test_process_candidate_sends_when_acl_unverified(self):
        readiness = {
            "transcript": {"ok": True, "file_name": "20261113205612-Demo-逐字稿文本.md", "bytes": 12, "error": None},
            "recording": {
                "ok": True,
                "view_address": VIEW,
                "card_text": CARD,
                "meeting_record_id": "r1",
                "acl_not_verified": True,
                "acl_degraded": True,
                "acl": {"ok": False},
            },
            "both_ready": True,
            "minutes": {"ok": True, "kind": "minutes", "skipped": True, "error": None},
        }
        cfg = {
            "enabled": True,
            "send_enabled": True,
            "mode": "live",
            "target_chat_id": "oc_ai",
            "judgment_b_max_attempts": 6,
            "judgment_b_interval_seconds": 300,
            "record_acl": {"enabled": True},
        }
        state = {"candidates": {}}
        mid = "991279001"
        cand = pol.candidate_record(state, mid)
        cand["status"] = "waiting_materials"
        cand["a_ended_confirmed"] = True
        cand["a_confirmed_at"] = "2026-11-13T21:52:12+08:00"
        cand["b_anchor_at"] = "2026-11-13T21:52:12+08:00"
        cand["b_attempts"] = 4
        cand["next_check_at"] = "2026-11-13T22:17:12+08:00"
        now = datetime.datetime(2026, 11, 13, 22, 17, 13, tzinfo=TZ)
        deliver_kwargs = {}

        def fake_deliver(*a, **k):
            deliver_kwargs.update(k)
            return {
                "sent": False, "dry_run": True, "reason": "ok",
                "file_names": [readiness["transcript"]["file_name"]],
                "would_send": [{"kind": "transcript"}, {"kind": "recording_link"}],
            }

        with mock.patch.object(pol, "now_sh", return_value=now), \
             mock.patch.object(pol, "log", lambda *a, **k: None), \
             mock.patch.object(pol.e, "send_ready", return_value=readiness), \
             mock.patch.object(pol.e, "poller_deliver_transcript_and_recording_link", side_effect=fake_deliver):
            out = pol.process_candidate(
                mid,
                {"end_time": "2026-11-13T22:00:00+08:00", "subject": "测试项目周会"},
                {"mark": "company", "reason": "created_via_feishu_assistant"},
                cfg, state, force=False,
            )
        self.assertEqual(out.get("action"), "dry_run_would_send")
        self.assertIs(deliver_kwargs.get("readiness"), readiness)
        self.assertTrue(state["candidates"][mid]["last_readiness"]["acl_degraded"])
        self.assertTrue(state["candidates"][mid]["last_readiness"]["recording_ok"])


if __name__ == "__main__":
    unittest.main()
