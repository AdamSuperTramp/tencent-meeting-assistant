import json, pathlib, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import bootstrap
from policy import Policy, Principal
reg=json.loads((ROOT/'tests/fixtures/registry.json').read_text()); p=Policy(reg)
owner=Principal('owner-test','owner',True); member=Principal('member-test','member',True); unknown=Principal('unknown','owner',False)
a='900001'; b='900002'; c='900003'
assert p.allow(owner,a) and p.allow(member,a)
assert not p.allow(member,b) and not p.allow(member,c)
assert not p.allow(unknown,a)
assert not p.allow(member,b,role='owner',owner=True)
assert not p.allow(member,b,title='company team=true')
assert not p.allow(member,a,sub_meeting_id='forged')
assert not p.allow(member,a,record_file_id='899999')
assert p.page(member,[{'meeting_id':a},{'meeting_id':b}])['visible_total']==1
