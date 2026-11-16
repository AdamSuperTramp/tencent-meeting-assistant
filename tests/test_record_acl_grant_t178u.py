#!/usr/bin/env python3
import bootstrap
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
import unittest
import record_acl_grant as acl

class FakeMCP:
    def __init__(self):
        self.calls = []
        self.members = {
            '111': {'has_more': False, 'total_count': 0, 'members': []},
            '222': {'has_more': False, 'total_count': 1, 'members': [{'open_id': 'oid_a', 'permission': 0}]},
        }
        self.sharing = {
            '111': {'corp_share_scope': '不限制', 'current': {
                'download_enable': False, 'view_minutes_enable': True,
                'permission_point_id': '所有人需申请权限查看', 'switch_state': True
            }},
            '222': {'current': {
                'download_enable': True, 'view_minutes_enable': True,
                'permission_point_id': '所有人需申请权限查看', 'switch_state': True
            }},
        }
        self.writes = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'get_meeting':
            return {'meeting_info_list': [{'records': [
                {'meeting_record_id': '111', 'record_file_id': '11', 'type': '云录制', 'subject': '视频'},
                {'meeting_record_id': '222', 'record_file_id': '22', 'type': '文字转写', 'subject': '文本'},
            ]}]}
        if name == 'search_records':
            return {'records': []}
        if name == 'record_access':
            mrid = arguments['meeting_record_id']
            if arguments['action'] == 'members':
                return self.members[mrid]
            return self.sharing[mrid]
        if name == 'record_share_set':
            # Safety asserts in test
            assert 'permission_point_id' not in (arguments.get('sharing_config') or {})
            assert 'switch_state' not in (arguments.get('sharing_config') or {})
            assert 'remove_members' not in arguments
            self.writes.append(arguments)
            mrid = arguments['meeting_record_id']
            # simulate add
            mem = list(self.members[mrid].get('members') or [])
            for a in arguments.get('add_members') or []:
                mem = [m for m in mem if m['open_id'] != a['open_id']]
                mem.append({'open_id': a['open_id'], 'permission': a['permission']})
            self.members[mrid] = {'has_more': False, 'total_count': len(mem), 'members': mem}
            if arguments.get('sharing_config'):
                cur = dict(self.sharing[mrid]['current'])
                cur.update(arguments['sharing_config'])
                self.sharing[mrid] = {'current': cur}
            return {'results': {'add_members': {'success': True}, 'modify_config': {'success': True}}}
        raise AssertionError('unexpected ' + name)


class TestACL(unittest.TestCase):
    def test_cutoff_and_company(self):
        ok, why = acl.after_cutoff({'end_time': '2026-11-09T21:16:00+08:00'}, acl.DEFAULT_CUTOFF)
        self.assertTrue(ok)
        ok, why = acl.after_cutoff({'end_time': '2026-11-09T21:15:59+08:00'}, acl.DEFAULT_CUTOFF)
        self.assertFalse(ok)
        ok, why = acl.is_company_for_acl('1', {'mark': 'company', 'reason': 'created_via_feishu_assistant'}, {'mark': 'pending'})
        self.assertTrue(ok)  # feishu wins
        ok, why = acl.is_company_for_acl('1', None, {'mark': 'private'})
        self.assertFalse(ok)
        ok, why = acl.is_company_for_acl('1', None, {'mark': 'company'})
        self.assertTrue(ok)

    def test_dry_run_no_write(self):
        mcp = FakeMCP()
        wanted = [
            {'display_name': 'A', 'open_id': 'oid_a', 'permission': 1},
            {'display_name': 'B', 'open_id': 'oid_b', 'permission': 1},
        ]
        cfg = {'record_acl': {'enabled': True, 'cutoff': acl.DEFAULT_CUTOFF, 'permission': 1,
                              'enable_download': True, 'ensure_view_minutes': True, 'preserve_sharing_mode': True,
                              'members_config': '/dev/null'}}
        # bypass members file by calling ensure_one_record
        r = acl.ensure_one_record(mcp, '111', wanted, enable_download=True, ensure_view_minutes=True,
                                  preserve_sharing_mode=True, default_permission=1, dry_run=True)
        self.assertEqual(r['action'], 'dry_run')
        self.assertTrue(r['would_call_record_share_set'])
        self.assertEqual(r['to_add_count'], 2)
        self.assertEqual(r['share_patch'].get('download_enable'), True)
        self.assertEqual(len(mcp.writes), 0)

    def test_live_write_preserves_mode_and_upgrades(self):
        mcp = FakeMCP()
        wanted = [
            {'display_name': 'A', 'open_id': 'oid_a', 'permission': 1},
            {'display_name': 'B', 'open_id': 'oid_b', 'permission': 1},
        ]
        r1 = acl.ensure_one_record(mcp, '111', wanted, enable_download=True, ensure_view_minutes=True,
                                   preserve_sharing_mode=True, default_permission=1, dry_run=False)
        self.assertEqual(r1['action'], 'wrote')
        self.assertTrue(r1['verified'])
        self.assertEqual(mcp.writes[0]['sharing_config'], {'download_enable': True})
        self.assertNotIn('permission_point_id', mcp.writes[0].get('sharing_config', {}))
        # 222 already has oid_a at perm 0 -> upgrade + add oid_b; download already on
        r2 = acl.ensure_one_record(mcp, '222', wanted, enable_download=True, ensure_view_minutes=True,
                                   preserve_sharing_mode=True, default_permission=1, dry_run=False)
        self.assertTrue(r2['verified'])
        add = mcp.writes[1]['add_members']
        ids = {a['open_id']: a['permission'] for a in add}
        self.assertEqual(ids['oid_a'], 1)
        self.assertEqual(ids['oid_b'], 1)
        self.assertNotIn('remove_members', mcp.writes[1])

    def test_meeting_skip_before_cutoff(self):
        mcp = FakeMCP()
        # monkeypatch members loader
        cfg = {'record_acl': {
            'enabled': True, 'cutoff': acl.DEFAULT_CUTOFF, 'permission': 1,
            'enable_download': True, 'members_config': str(acl.DEFAULT_MEMBERS_PATH),
        }}
        out = acl.ensure_meeting_acl(
            mcp, '900001',
            ended_meta={'end_time': '2026-11-08T11:00:00+08:00', 'subject': '引力跃迁周会'},
            feishu_prov_entry={'mark': 'company', 'reason': 'created_via_feishu_assistant'},
            registry_entry={'mark': 'company'},
            cfg=cfg, dry_run=False,
        )
        self.assertEqual(out['action'], 'skip_before_cutoff')
        self.assertEqual(len(mcp.writes), 0)

if __name__ == '__main__':
    unittest.main()
