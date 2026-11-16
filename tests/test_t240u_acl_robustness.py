#!/usr/bin/env python3
"""t240u offline ACL robustness; synthetic IDs only, no SDK/network/CLI calls."""
import contextlib
import json
import os
import pathlib
import sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import bootstrap
import socket
import stat
import tempfile
import unittest
from unittest import mock
import record_acl_grant as acl
import business_export as be

NAMES = ['fixture-user-1', 'fixture-user-2', 'fixture-user-3', 'fixture-user-4', 'fixture-user-5']
WANTED = [{'display_name': n, 'open_id': 'fixture_' + str(i), 'permission': 0}
          for i, n in enumerate(NAMES)]

def rows(people):
    return [{'open_id': w['open_id'], 'permission': '可查看'} for w in people]

def members(people, **kw):
    return dict(access_members=rows(people), has_more=False, total_count=len(people), **kw)

def sharing(download=True, minutes=True):
    return {'current': {'download_enable': download, 'view_minutes_enable': minutes,
                        'permission_point_id': '所有人需申请权限查看', 'switch_state': True}}

def write_result(fail=(), config_success=True, nested=True):
    body = {'fail_access_members': [{'to_operator_id': WANTED[i]['open_id'], 'to_operator_id_type': 2}
                                   for i in fail]}
    value = {'code': 0, 'results': {
        'add_members': {'executed': True, 'success': True,
                        'response': {'status_code': 200, 'body': json.dumps(body)}},
        'modify_config': {'executed': True, 'success': config_success,
                          'response': {'status_code': 200, 'body': '{}'}},
        'remove_members': {'executed': False}}}
    return {'result': {'content': [{'type': 'text', 'text': json.dumps(value)}]}} if nested else value

class Fake:
    def __init__(self, fail=(), initial=None, bad_total=False, bad_minutes=False, nested=False, config_success=True):
        self.fail, self.initial = tuple(fail), list(initial if initial is not None else [WANTED[2]])
        self.people = list(self.initial)
        self.bad_total, self.bad_minutes, self.nested = bad_total, bad_minutes, nested
        self.config_success = config_success
        self.calls, self.writes = [], []
        self.download = False
    def __call__(self, name, args):
        self.calls.append((name, args))
        if name == 'record_access' and args['action'] == 'members':
            result = {'access_members': [], 'has_more': False, 'total_count': 5} if self.bad_total else members(self.people)
            return {'response': {'body': json.dumps({'data': result})}} if self.nested else result
        if name == 'record_access' and args['action'] == 'sharing':
            v = sharing(self.download, not self.bad_minutes)
            return {'result': {'data': v}} if self.nested else v
        if name == 'record_share_set':
            self.writes.append(args)
            assert set(args) <= {'meeting_record_id', 'add_members', 'sharing_config'}
            assert set(args.get('sharing_config', {})) <= {'download_enable', 'view_minutes_enable'}
            for add in args.get('add_members', []):
                assert set(add) == {'open_id', 'permission'}
                i = next(i for i,w in enumerate(WANTED) if w['open_id'] == add['open_id'])
                if i not in self.fail and not any(w['open_id'] == add['open_id'] for w in self.people):
                    self.people.append(WANTED[i])
            self.download = True
            return write_result(self.fail, self.config_success)
        raise AssertionError('unexpected mock tool ' + name)

class Robustness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.stack.enter_context(mock.patch.object(acl, 'BASE', self.base))
        self.stack.enter_context(mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
    def business(self, fake, cache=None):
        with mock.patch.object(be, 'load_record_acl_wanted', return_value=(WANTED, {
                'permission': 0, 'enable_download': True, 'ensure_view_minutes': True,
                'preserve_sharing_mode': True})), mock.patch.object(be, 'mcp_call', side_effect=fake):
            return be.ensure_and_verify_recording_acl('fixture_meeting', ['fixture_record'], acl_cache=cache)
    def helper(self, fake):
        return acl.ensure_one_record(fake, 'fixture_record', WANTED,
            enable_download=True, ensure_view_minutes=True, preserve_sharing_mode=True,
            default_permission=0, dry_run=False)
    def test_four_fail_one_already_present_business(self):
        fake, cache = Fake(fail=(0,1,3,4)), {}
        out = self.business(fake, cache)
        self.assertFalse(out['ok'])
        self.assertEqual(set(out['records'][0]['failed_names']), set(NAMES)-{'fixture-user-3'})
        self.assertEqual(cache['verified_record_ids'], [])
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(len(fake.calls), 2)  # members + write; no calls after failure
        self.assertEqual({m['open_id'] for m in fake.writes[0]['add_members']},
                         {w['open_id'] for w in WANTED if w['display_name'] != 'fixture-user-3'})
        self.assertEqual(fake.writes[0]['sharing_config'], {'download_enable': True, 'view_minutes_enable': True})
    def test_four_fail_one_already_present_helper(self):
        fake = Fake(fail=(0,1,3,4))
        out = self.helper(fake)
        self.assertFalse(out['verified'])
        self.assertEqual(out['action'], 'write_failed')
        self.assertEqual(set(out['failed_names']), set(NAMES)-{'fixture-user-3'})
        self.assertEqual(len(fake.calls), 3)  # baseline members/sharing + write
    def test_partial_success_lists_only_failed_people(self):
        fake = Fake(fail=(1,4))
        out = self.business(fake)
        self.assertFalse(out['ok'])
        self.assertEqual(out['records'][0]['write_status']['failed_count'], 2)
        self.assertEqual(set(out['records'][0]['failed_names']), {'fixture-user-2','fixture-user-5'})
        self.assertEqual(len(fake.writes), 1)
    def test_nested_results_business_failure(self):
        request = {'add_members': [{'open_id': WANTED[0]['open_id']}], 'sharing_config': {'download_enable': True}}
        report = acl.inspect_share_result({'results': {'inner': {'response': {'body': json.dumps(write_result((0,), nested=False))}}}}, request, WANTED)
        self.assertFalse(report['ok'])
        self.assertEqual(report['failed_names'], ['fixture-user-1'])
    def test_top_level_success_not_enough(self):
        report = acl.inspect_share_result(write_result((0,1,3,4)), {'add_members': [1]}, WANTED)
        self.assertFalse(report['ok'])
        self.assertIn('fail_access_members', report['errors'])
    def test_nested_config_failure_stops(self):
        fake = Fake(config_success=False)
        out = self.business(fake)
        self.assertFalse(out['ok'])
        self.assertEqual(len(fake.calls), 2)
    def test_not_executed_requested_child_stops(self):
        r = {'code': 0, 'results': {'add_members': {'executed': False}, 'remove_members': {'executed': False}}}
        out = acl.inspect_share_result(r, {'add_members': [1]}, WANTED)
        self.assertFalse(out['ok'])
    def test_total_five_empty_rows_no_write_business(self):
        fake = Fake(bad_total=True)
        out = self.business(fake)
        self.assertFalse(out['ok'])
        self.assertEqual(fake.writes, [])
    def test_total_five_empty_rows_no_write_helper(self):
        fake = Fake(bad_total=True)
        out = self.helper(fake)
        self.assertFalse(out['verified'])
        self.assertEqual(fake.writes, [])
    def test_full_five_and_switches_business(self):
        fake, cache = Fake(nested=True), {}
        out = self.business(fake, cache)
        self.assertTrue(out['ok'])
        self.assertEqual(cache['verified_record_ids'], ['fixture_record'])
        self.assertEqual(out['records'][0]['permission_values'], ['可查看'])
    def test_full_five_and_switches_helper(self):
        out = self.helper(Fake(nested=True))
        self.assertTrue(out['verified'])
    def test_missing_switch_not_verified(self):
        out = self.business(Fake(bad_minutes=True))
        self.assertFalse(out['ok'])
    def test_unknown_permission_not_verified(self):
        m = members(WANTED)
        del m['access_members'][0]['permission']
        self.assertFalse(acl.members_evidence_complete(m))
    def test_userid_not_reinterpreted_as_open_id(self):
        m = {'members': [{'userid': 'fixture_0', 'permission': 0}], 'has_more': False}
        self.assertFalse(acl.members_evidence_complete(m))
        self.assertEqual(acl.members_index(m), {})
    def test_members_business_error_not_empty_acl(self):
        def fake(*a): return {'result': {'body': json.dumps({'error_info': {'code': 123}})}}
        m = acl.fetch_all_members(fake, 'fixture_record')
        self.assertFalse(m['_acl_complete'])
        self.assertFalse(acl.members_evidence_complete(m))
    def test_pagination_nested_and_complete(self):
        calls = []
        def fake(n,a):
            calls.append(a)
            if len(calls)==1:
                return {'data': {'access_members': rows(WANTED[:2])}, 'has_more': True, 'next_page_token': 'p2', 'total_count': 5}
            return {'data': {'access_members': rows(WANTED[2:]), 'has_more': False, 'total_count': 5}}
        m = acl.fetch_all_members(fake, 'fixture_record')
        self.assertTrue(acl.members_evidence_complete(m))
        self.assertEqual(len(acl.members_index(m)),5)
        self.assertEqual(calls[1]['page_token'],'p2')
    def test_missing_page_token_incomplete(self):
        m = acl.fetch_all_members(lambda *a: {'access_members': rows(WANTED), 'has_more': True}, 'r')
        self.assertFalse(acl.members_evidence_complete(m))
    def test_repeated_page_token_incomplete(self):
        calls=[]
        def fake(*a):
            calls.append(1)
            return {'access_members': rows(WANTED), 'has_more': True, 'next_page_token': 'same'}
        m=acl.fetch_all_members(fake,'r')
        self.assertFalse(acl.members_evidence_complete(m))
        self.assertEqual(len(calls),2)
    def test_pagination_cap_no_false_complete(self):
        calls=[]
        def fake(*a):
            calls.append(1)
            return {'access_members': [], 'has_more': True, 'next_page_token': str(len(calls))}
        m=acl.fetch_all_members(fake,'r')
        self.assertFalse(acl.members_evidence_complete(m))
        self.assertEqual(len(calls),20)
    def test_existing_non_target_preserved(self):
        extra={'open_id':'fixture_non_target','display_name':'other','permission':1}
        fake=Fake(initial=[WANTED[2],extra])
        self.assertTrue(self.business(fake)['ok'])
        self.assertTrue(any(w['open_id']==extra['open_id'] for w in fake.people))
        self.assertNotIn('remove_members',fake.writes[0])
    def test_five_active_targets_independent_of_attendance(self):
        doc={'members':[dict(w,status='active') for w in WANTED], 'participants':['fixture-user-3']}
        self.assertEqual({w['display_name'] for w in acl.active_member_targets(doc)},set(NAMES))
    def test_active_duplicate_or_missing_id_fails_closed(self):
        for targets in ([dict(WANTED[0],status='active'), dict(WANTED[0],status='active')],
                        [{'display_name':'x','status':'active'}]):
            with self.assertRaises(ValueError): acl.active_member_targets({'members':targets})
    def test_missing_requested_child_result_fails(self):
        r={'code':0,'results':{'modify_config':{'success':True}}}
        out=acl.inspect_share_result(r,{'add_members':[1],'sharing_config':{'download_enable':True}},WANTED)
        self.assertFalse(out['ok'])
        self.assertIn('add_members:missing_result',out['errors'])
    def test_helper_member_error_stops_before_sharing(self):
        calls=[]
        def fake(n,a):
            calls.append(a)
            return {'error_info':{'code':500246}}
        out=self.helper(fake)
        self.assertFalse(out['verified'])
        self.assertEqual(len(calls),1)
    def test_local_audit_modes_and_credentials_redacted(self):
        acl.audited_mcp(lambda *a: {'results': {'body': json.dumps({'headers': {'Authorization':'fake-sensitive','X-Tencent-Meeting-Token':'fake-sensitive'}, 'pwd':'fake-sensitive'})}},
                        'record_access', {'action':'sharing','meeting_record_id':'fixture_record'})
        directory=self.base/'private-record-acl-audit'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
        for p in directory.glob('*.json'):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
            self.assertNotIn('fake-sensitive',p.read_text())
    def test_unstructured_auth_error_redacted(self):
        for text in ('TENCENT_MEETING_TOKEN=fake-sensitive', 'Authorization: Basic fake-sensitive',
                     'password: fake-sensitive', 'api_key=fake-sensitive'):
            self.assertNotIn('fake-sensitive',acl._audit_safe(text))
    def test_audit_failure_prevents_mcp(self):
        (self.base/'private-record-acl-audit').write_text('not a directory')
        fn=mock.Mock()
        with self.assertRaises(OSError): acl.audited_mcp(fn,'record_share_set',{})
        fn.assert_not_called()
    def test_write_error_no_retry_or_followup(self):
        fake=Fake()
        base=fake.__call__
        def call(n,a):
            if n=='record_share_set':
                fake.calls.append((n,a)); return {'error_info': {'code':500246}}
            return base(n,a)
        out=self.business(call)
        self.assertFalse(out['ok'])
        self.assertEqual(len(fake.calls),2)

if __name__=='__main__':
    unittest.main(verbosity=2)
