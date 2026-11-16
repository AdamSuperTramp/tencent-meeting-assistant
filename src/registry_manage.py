"""Owner-local maintenance only. Not exposed as an agent tool or network API.
Same-adam-UID security limitation applies. Does not grant Feishu access.
"""
import pathlib,json,os,argparse,datetime
from settings import DATA_DIR
R=DATA_DIR
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('meeting_id');p.add_argument('mark',choices=['company','private','pending']);p.add_argument('--reason',required=True);a=p.parse_args()
 path=R/'registry.json';data=json.loads(path.read_text());entry=data['meetings'].setdefault(a.meeting_id,{'record_file_ids':[],'sub_meeting_ids':[]})
 entry.update(mark=a.mark,reason=a.reason,version=data['version']+1);data['version']+=1
 tmp=R/'registry.new';fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2)
 os.replace(tmp,path)
 # Only this component's cache. Existing OpenClaw summaries remain owner-only and must never migrate to team agents.
 cache=R/'cache'
 if cache.exists():
  for f in cache.iterdir():
   if f.is_file():f.unlink()
 event={'time':datetime.datetime.now().isoformat(),'meeting_id':a.meeting_id,'mark':a.mark,'version':data['version'],'actor':'owner-local-cli','action':'registry-update-cache-invalidated'}
 fd=os.open(R/'registry-audit.jsonl',os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
 with os.fdopen(fd,'a') as f:f.write(json.dumps(event)+'\n')
 print('Updated registry version',data['version'],'(does not enable member transport)')
