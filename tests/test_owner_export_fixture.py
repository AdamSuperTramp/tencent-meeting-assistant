import pathlib,tempfile,json,importlib.util,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
SRC=ROOT/'src'
FIX=ROOT/'tests'/'fixtures'
sys.path.insert(0, str(SRC))
import bootstrap
spec=importlib.util.spec_from_file_location('export_test',SRC/'business_export.py');e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
# This test uses only synthetic meetings and does not invoke production admission.
e.owner_context=lambda:'fixed-owner-only-fixture'
passed={}
with tempfile.TemporaryDirectory(prefix='owner-export-fixture-') as temp:
 t=pathlib.Path(temp);e.BASE=t;e.SOURCE=t/'sources';e.SOURCE.mkdir();(e.SOURCE/'texts').mkdir();e.OUT=t/'exports-generated'
 e.owner_context=lambda:'fixed-owner-only-fixture'
 registry={'version':1,'meetings':{}}
 inventory=[]
 for i,mark in enumerate(['company','private','pending'],1):
  mid=str(900000+i);fid=str(800000+i)
  registry['meetings'][mid]={'mark':mark,'record_file_ids':[fid],'sub_meeting_ids':[]}
  inventory.append({'meeting_id':mid,'record_file_ids':[fid],'title':'SYNTHETIC '+mark,'date_beijing':'2026-11-07T21:19:00+08:00'})
  (e.SOURCE/'texts'/f'record-{fid}-minutes.json').write_text(json.dumps({'meeting_minute':{'minute':'SYNTHETIC FIXTURE ONLY - no real private text'}}))
 (t/'registry.json').write_text(json.dumps(registry));(e.SOURCE/'inventory.json').write_text(json.dumps(inventory));(t/'file-delivery-enabled.json').write_text('{"enabled":false}')
 (e.SOURCE/'meetings-raw.json').write_text(json.dumps({str(900000+i):[{'source':'search_records','record_file_id':str(800000+i),'minutes_file_name':'20261107211900-SYNTHETIC '+mark+'-纪要文本.md'}] for i,mark in enumerate(['company','private','pending'],1)}))
 for i,mark in enumerate(['company','private','pending'],1):
  result=e.prepare({'meeting_id':str(900000+i),'kind':'minutes','format':'md'});assert len(result['exports'])==1 and result['exports'][0]['format']=='md' and result['exports'][0]['bytes']>0;passed['owner_'+mark+'_export']=True
  try:e.send({'export_id':result['exports'][0]['export_id']});raise AssertionError('Unexpected send')
  except PermissionError:passed[mark+'_send_latch_denied']=True
 try:e.prepare({'meeting_id':'900002','kind':'minutes','format':'md','role':'owner'});raise AssertionError('Actor argument accepted')
 except ValueError:passed['model_role_argument_rejected']=True
 e.live_search_records=lambda mid: []
 try:e.prepare({'meeting_id':'999999','kind':'minutes','format':'md'});raise AssertionError('Unknown source accepted')
 except PermissionError:passed['unknown_source_denied']=True
 def deny():raise PermissionError('Exclusive-owner boundary not established')
 e.owner_context=deny
 try:e.prepare({'meeting_id':'900002','kind':'minutes','format':'md'});raise AssertionError('Guard bypass')
 except PermissionError:passed['guard_false_private_export_denied']=True
