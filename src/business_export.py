"""Fixed-source/fixed-recipient export tools for exclusive-owner gateway only.
Not a team identity adapter; no arbitrary paths, receivers or actor claims accepted.
"""
import pathlib,json,os,re,hashlib,zipfile,html,subprocess,datetime,tempfile
from settings import CODE_DIR, DATA_DIR, SOURCE_ARCHIVE_DIR, EXPORT_DIR, OWNER_OPEN_ID, OPENCLAW_CLI, NODE_BIN, OPENCLAW_CONFIG, OPENCLAW_AUTOMATION_CONFIG, SECRETS_FILE, AUTO_SEND_GROUP_ID
BASE=DATA_DIR
SOURCE=SOURCE_ARCHIVE_DIR
OUT=EXPORT_DIR
OWNER=OWNER_OPEN_ID
CLI=OPENCLAW_CLI
NODE=NODE_BIN
CONFIG=OPENCLAW_CONFIG
from tencent_client import rpc as tencent_rpc, check_quota, PollerQuotaError
import feishu_delivery
from contextvars import ContextVar
REQUEST_CONTEXT=ContextVar('feishu_request_context',default=None)
TOOLS=[{'name':'prepare_meeting_export','description':'Generate real owner-accessible registered meeting minutes/transcript as Markdown from archived known source or live MCP for newly ended company meetings. Word/DOCX disabled. Exclusive-owner channel only; no sending. Returns opaque export IDs.','inputSchema':{'type':'object','additionalProperties':False,'properties':{'meeting_id':{'type':'string','pattern':'^[0-9]+$'},'kind':{'type':'string','enum':['minutes','transcript','both']},'format':{'type':'string','enum':['md'],'description':'Markdown only; Word/DOCX disabled by owner preference'}},'required':['meeting_id','kind','format']}},{'name':'send_prepared_meeting_export','description':'Send an already generated verified export through official Feishu channel to the single fixed owner. No arbitrary file path/recipient. Owner-only delivery latch is configured server-side; call this after prepare_meeting_export, not the unavailable generic message tool.','inputSchema':{'type':'object','additionalProperties':False,'properties':{'export_id':{'type':'string','pattern':'^[a-f0-9]{24}$'}},'required':['export_id']}}]
def load(path):return json.loads(path.read_text())
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def owner_context():
 # Fixed server-side admission context, NEVER a tool argument. No authenticated
 # per-turn member identity exists in this stdio server. Fail closed unless the
 # entire gateway remains exclusive-owner with locked tools.
 spec=__import__('importlib.util',fromlist=['']).spec_from_file_location('export_admission',CODE_DIR/'owner_only_bridge.py')
 module=__import__('importlib.util',fromlist=['']).module_from_spec(spec)
 spec.loader.exec_module(module)
 module.guard()
 return 'fixed-owner-only'

def allowed_group_chat_ids():
 # Server-side allowlist only; never trust arbitrary tool-supplied recipients.
 f=json.loads(CONFIG.read_text()).get('channels',{}).get('feishu',{})
 ids=f.get('groupAllowFrom') or []
 return {x for x in ids if isinstance(x,str) and re.fullmatch(r'oc_[a-zA-Z0-9]+',x)}

def normalize_group_chat_id(raw):
 if not isinstance(raw,str):return None
 s=raw.strip()
 for prefix in ('feishu:','lark:','chat:','group:','channel:'):
  if s.lower().startswith(prefix):s=s[len(prefix):].strip()
 return s if re.fullmatch(r'oc_[a-zA-Z0-9]+',s) else None

def resolve_delivery_target(explicit=None):
 # Current runtime-owned MCP context only. No global session lookup or owner fallback.
 ctx=REQUEST_CONTEXT.get() or {}
 sk=ctx.get('sessionKey','')
 match=re.fullmatch(r'agent:main:feishu:group:(oc_[a-zA-Z0-9]+)',sk)
 if match:
  oc=match.group(1)
  if oc not in allowed_group_chat_ids():raise PermissionError('Inbound group not allowlisted')
  if explicit and normalize_group_chat_id(explicit)!=oc:raise PermissionError('Target differs from current inbound group')
  return {'cli_target':'chat:'+oc,'chat_id':oc,'kind':'group','source':'bound-mcp-session'}
 if sk=='agent:main:main' or sk=='agent:main:feishu:dm:'+OWNER:
  if explicit:raise PermissionError('DM request cannot redirect to group')
  return {'cli_target':OWNER,'chat_id':OWNER,'kind':'direct','source':'bound-mcp-session'}
 raise PermissionError('Missing reliable current Feishu request context; no upload, no DM fallback')

LIVE_CACHE=BASE/'live-cache'
FEISHU_CREATED_PROVENANCE=BASE/'feishu-assistant-created.json'
FEISHU_CREATED_REASON='created_via_feishu_assistant'

def _atomic_json_write(path, data):
 path=pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.', dir=str(path.parent))
 try:
  with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
  os.chmod(tmp,0o600); os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def load_feishu_created_provenance():
 p=FEISHU_CREATED_PROVENANCE
 if not p.exists():return {'version':1,'meetings':{}}
 data=load(p)
 if not isinstance(data,dict):return {'version':1,'meetings':{}}
 data.setdefault('version',1); data.setdefault('meetings',{})
 return data

def is_feishu_assistant_created(mid):
 mid=str(mid)
 return mid in (load_feishu_created_provenance().get('meetings') or {})

def record_feishu_assistant_created(meeting_id, subject=None, created_from_chat_id=None, session_key=None, extra=None):
 """Durable provenance: any meeting created via Feishu「腾讯会议助手」is company/team."""
 mid=str(meeting_id)
 if not re.fullmatch(r'[0-9]+', mid):raise ValueError('Invalid meeting_id for feishu provenance')
 now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
 data=load_feishu_created_provenance()
 prev=(data.get('meetings') or {}).get(mid) or {}
 entry=dict(prev)
 entry.update({
  'meeting_id':mid,
  'subject':(subject if subject is not None else prev.get('subject')),
  'created_from_chat_id':(created_from_chat_id if created_from_chat_id is not None else prev.get('created_from_chat_id')),
  'session_key':(session_key if session_key is not None else prev.get('session_key')),
  'reason':FEISHU_CREATED_REASON,
  'mark':'company',
  'created_at':prev.get('created_at') or now,
  'updated_at':now,
 })
 if isinstance(extra,dict):
  # Durable schedule fields may be refreshed on create/update; other extras stay fill-once.
  refreshable={'start_time','end_time','time_zone','meeting_code','join_url','cancelled','cancelled_at','cancel_reason'}
  for k,v in extra.items():
   if v is None: continue
   if k in refreshable or k not in entry: entry[k]=v
 data.setdefault('meetings',{})[mid]=entry
 data['version']=int(data.get('version') or 1)+1
 data['updated_at']=now
 _atomic_json_write(FEISHU_CREATED_PROVENANCE, data)
 # Force company in privacy registry. Feishu-assistant-created overrides pending and
 # even a prior private mark (Adam standing rule: never personal if created via this bot).
 return upsert_company_from_feishu_provenance(mid, entry)

def upsert_company_from_feishu_provenance(mid, prov=None):
 prov=prov or (load_feishu_created_provenance().get('meetings') or {}).get(str(mid)) or {}
 subject=(prov.get('subject') or '')
 reason=FEISHU_CREATED_REASON
 if subject:reason=reason+': '+subject
 return atomic_registry_upsert(str(mid),'company',reason,[], allow_private_upgrade=True,
                               subject=subject or None,
                               provenance={'source':'feishu-assistant-created','created_from_chat_id':prov.get('created_from_chat_id'),'session_key':prov.get('session_key'),'created_at':prov.get('created_at')},
                               start_time=prov.get('start_time'), end_time=prov.get('end_time'),
                               cancelled=prov.get('cancelled'))

def sync_local_meeting_schedule(meeting_id, *, start_time=None, end_time=None, time_zone=None, subject=None, cancelled=None, cancel_reason=None, meeting_code=None, join_url=None, source='local-schedule-sync'):
 """Persist schedule fields for start+20m poll gate. Never invent start_time. No MCP."""
 mid=str(meeting_id)
 if not re.fullmatch(r'[0-9]+', mid):raise ValueError('Invalid meeting_id')
 extra={}
 if start_time is not None: extra['start_time']=start_time
 if end_time is not None: extra['end_time']=end_time
 if time_zone is not None: extra['time_zone']=time_zone
 if meeting_code is not None: extra['meeting_code']=meeting_code
 if join_url is not None: extra['join_url']=join_url
 if cancelled is not None:
  extra['cancelled']=bool(cancelled)
  if cancelled:
   extra['cancelled_at']=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
   if cancel_reason: extra['cancel_reason']=cancel_reason
 if not extra and subject is None:
  return {'ok':False,'why':'nothing_to_sync'}
 # Prefer updating feishu provenance when known company; else registry-only schedule stamp.
 if is_feishu_assistant_created(mid) or extra.get('cancelled') is not None or start_time is not None:
  # Ensure provenance row exists for company creates; for registry-only company keep registry path below.
  if is_feishu_assistant_created(mid):
   record_feishu_assistant_created(mid, subject=subject, extra=extra)
  else:
   # Registry company / watchlist may store schedule without feishu row.
   path=BASE/'registry.json'
   data=load(path) if path.exists() else {'version':1,'meetings':{}}
   entry=(data.get('meetings') or {}).get(mid)
   if isinstance(entry, dict) and entry.get('mark')=='company':
    atomic_registry_upsert(mid, 'company', entry.get('reason') or 'company', entry.get('record_file_ids') or [],
                           sub_meeting_ids=entry.get('sub_meeting_ids'), subject=subject or entry.get('subject'),
                           provenance=entry.get('provenance'), start_time=start_time if start_time is not None else entry.get('start_time'),
                           end_time=end_time if end_time is not None else entry.get('end_time'),
                           cancelled=cancelled if cancelled is not None else entry.get('cancelled'))
   elif start_time is not None:
    # Cannot invent company mark; stash under feishu file only if already there — otherwise report gap.
    return {'ok':False,'why':'no_local_company_row_for_schedule','meeting_id':mid}
 return {'ok':True,'meeting_id':mid,'fields':sorted(extra.keys()),'source':source}


def record_file_ids_from_live_rows(rows):
 fids=[]
 for r in rows or []:
  fid=str(r.get('record_file_id') or '')
  if not re.fullmatch(r'[0-9]+',fid):continue
  if r.get('file_type')=='transcript' or r.get('has_transcript_content') is True:
   fids.append(fid)
 if not fids:
  fids=[str(r.get('record_file_id')) for r in (rows or []) if re.fullmatch(r'[0-9]+',str(r.get('record_file_id') or ''))]
 return list(dict.fromkeys(fids))

MCP_ENDPOINT='https://mcp.meeting.tencent.com/mcp/wemeet-open/v1'
MCP_SKILL='v1.1.0'
CLIENT_INFO={'_client_info':{'os':'macOS','agent':'OpenClaw','model':'GrokBot'}}

def mcp_token():
 t=os.environ.get('TENCENT_MEETING_TOKEN','').strip()
 if t:return t
 secrets=SECRETS_FILE
 return load(secrets)['TENCENT_MEETING_TOKEN']

def mcp_call(name,arguments):
 args=dict(arguments or {});args.update(CLIENT_INFO)
 body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}
 raw=tencent_rpc(body,timeout=120,access_token=mcp_token())
 check_quota(raw)
 if isinstance(raw,dict) and raw.get('error'):
  raise RuntimeError('MCP '+name+' error: '+str(raw['error']))
 result=raw.get('result',raw)
 if isinstance(result,dict) and result.get('isError'):
  raise RuntimeError('MCP '+name+' tool error')
 content=result.get('content') if isinstance(result,dict) else None
 if not (isinstance(content,list) and content):
  return result
 text=content[0].get('text','')
 try:env=json.loads(text)
 except Exception:return {'raw_text':text}
 if isinstance(env,dict) and 'body' in env and 'status_code' in env:
  body=env.get('body')
  if isinstance(body,str):
   try:body=json.loads(body)
   except Exception:pass
  if env.get('status_code') not in (None,200) and not body:
   raise RuntimeError('MCP '+name+' HTTP '+str(env.get('status_code')))
  return body
 return env

def known_company_titles():
 titles=set()
 reg=load(BASE/'registry.json')
 inv_path=SOURCE/'inventory.json'
 inv={r['meeting_id']:r for r in (load(inv_path) if inv_path.exists() else [])}
 for mid,entry in reg.get('meetings',{}).items():
  if entry.get('mark')!='company':continue
  row=inv.get(mid) or {}
  title=(row.get('title') or row.get('subject') or '').strip()
  if title:titles.add(title)
 # Hardened recurring company titles already proven in inventory history.
 titles.update({'引力跃迁周会','引力跃迁会议'})
 return titles

def atomic_registry_upsert(mid,mark,reason,record_file_ids,sub_meeting_ids=None,allow_private_upgrade=False,subject=None,provenance=None,start_time=None,end_time=None,cancelled=None):
 path=BASE/'registry.json'
 data=load(path)
 entry=data['meetings'].setdefault(mid,{'record_file_ids':[],'sub_meeting_ids':[]})
 # Never silently upgrade private -> company unless Feishu-assistant provenance / explicit allow.
 if entry.get('mark')=='private' and mark=='company' and not allow_private_upgrade:
  raise PermissionError('Refusing to auto-reclassify private meeting as company')
 if entry.get('mark') in ('company','private') and mark=='pending':
  mark=entry['mark'];reason=entry.get('reason') or reason
 ids=[str(x) for x in (record_file_ids or []) if re.fullmatch(r'[0-9]+',str(x))]
 # Prefer keeping existing ids and union with newly discovered.
 merged=list(dict.fromkeys(list(entry.get('record_file_ids') or [])+ids))
 entry.update(mark=mark,reason=reason,version=data['version']+1,record_file_ids=merged,sub_meeting_ids=list(sub_meeting_ids or entry.get('sub_meeting_ids') or []))
 if subject:entry['subject']=subject
 if provenance:entry['provenance']=provenance
 if start_time is not None:entry['start_time']=start_time
 if end_time is not None:entry['end_time']=end_time
 if cancelled is not None:entry['cancelled']=bool(cancelled)
 data['version']+=1
 fd,tmp=tempfile.mkstemp(prefix='.registry-',dir=str(path.parent))
 try:
  with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
 return entry

def live_search_records(mid):
 out=mcp_call('search_records',{'meeting_id':mid,'page_size':50})
 rows=out.get('records') if isinstance(out,dict) else None
 if not isinstance(rows,list):
  raise PermissionError('Live MCP discovery failed for meeting')
 return rows

def infer_company_from_live(mid,rows):
 titles=known_company_titles()
 subjects=sorted({(r.get('subject') or '').strip() for r in rows if (r.get('subject') or '').strip()})
 if not subjects:return None
 # Identifiable company only when subject exactly matches known company titles.
 if not any(s in titles for s in subjects):
  return None
 # Prefer transcript record file ids; fall back to any with transcript content.
 fids=[]
 for r in rows:
  fid=str(r.get('record_file_id') or '')
  if not re.fullmatch(r'[0-9]+',fid):continue
  if r.get('file_type')=='transcript' or r.get('has_transcript_content') is True:
   fids.append(fid)
 if not fids:
  fids=[str(r.get('record_file_id')) for r in rows if re.fullmatch(r'[0-9]+',str(r.get('record_file_id') or ''))]
 if not fids:return None
 subject=next(s for s in subjects if s in titles)
 return subject,list(dict.fromkeys(fids))

def ensure_registered(mid):
 owner_context()
 mid=str(mid)
 # Standing rule: Feishu「腾讯会议助手」created meetings are always company/team.
 if is_feishu_assistant_created(mid):
  return upsert_company_from_feishu_provenance(mid)
 data=load(BASE/'registry.json')
 entry=data['meetings'].get(mid)
 if entry:
  # Already company via create-path reason also counts even if provenance file lagging.
  if entry.get('mark')=='company' and FEISHU_CREATED_REASON in str(entry.get('reason') or ''):
   return entry
  return entry
 # Archive miss / never inventoried: discover live, auto-mark only identifiable company meetings.
 rows=live_search_records(mid)
 inferred=infer_company_from_live(mid,rows)
 if not inferred:
  raise PermissionError('Unattributed/unknown meeting source denied')
 subject,fids=inferred
 return atomic_registry_upsert(mid,'company','live MCP subject matches known company title: '+subject,fids)

def permitted(mid):
 if not re.fullmatch(r'[0-9]+',mid):raise ValueError('Invalid meeting ID')
 ctx=REQUEST_CONTEXT.get() or {}
 if ':feishu:group:' in ctx.get('sessionKey',''):
  registry_path=BASE/'registry.json'
  registry=load(registry_path).get('meetings',{}) if registry_path.exists() else {}
  if registry.get(mid,{}).get('mark')!='company' and not is_feishu_assistant_created(mid):
   raise PermissionError('Group access requires registered company meeting')
 entry=ensure_registered(mid)
 if ':feishu:group:' in ctx.get('sessionKey','') and entry.get('mark')!='company':raise PermissionError('Private/pending meeting denied in group context')
 # Owner retains access to registered company/private/pending. This module has
 # no member route; future members must use a separate authenticated ACL adapter.
 return entry

def text_from_record_payload(kind,data):
 if data.get('error_info') or data.get('error') or data.get('failure'):return ''
 if kind=='minutes':
  return (data.get('meeting_minute') or {}).get('minute','') or ''
 if data.get('more') or data.get('has_more'):raise ValueError('Incomplete transcript source')
 paragraphs=(data.get('minutes') or {}).get('paragraphs',[]);parts=[]
 for paragraph in paragraphs:
  text=''.join(str(w.get('text','')) for s in paragraph.get('sentences',[]) for w in s.get('words',[]))
  if text:
   sec=int(paragraph.get('start_time',0))/1000
   parts.append(f'[{int(sec//60):02d}:{int(sec%60):02d}] '+(paragraph.get('speaker') or {}).get('user_name','未标注说话人')+'：'+text)
 return '\n\n'.join(parts)

def archive_source_text(mid,kind,entry):
 rows=load(SOURCE/'inventory.json') if (SOURCE/'inventory.json').exists() else []
 row=next((r for r in rows if r['meeting_id']==mid),None)
 if not row:return None
 pieces=[];sources=[]
 for fid in entry['record_file_ids']:
  if fid not in row['record_file_ids'] or not re.fullmatch(r'[0-9]+',fid):raise PermissionError('Recording attribution mismatch')
  p=SOURCE/'texts'/f'record-{fid}-{kind}.json'
  if not p.exists() or p.is_symlink():continue
  data=load(p)
  text=text_from_record_payload(kind,data)
  if text:pieces.append('## 录制分段 '+fid+'\n\n'+text);sources.append({'record_file_id':fid,'source_sha256':digest(p),'source_origin':'archive'})
 if not pieces:return None
 header='# '+row['title']+' — '+('纪要' if kind=='minutes' else '逐字稿')+'\n\n会议ID：'+mid+'\n\n会议日期（北京时间）：'+row['date_beijing']+'\n\n来源：已归档腾讯会议录制文本；非新会议、非重新生成纪要。\n\n'
 return header+'\n\n'.join(pieces),sources

def live_source_text(mid,kind,entry):
 LIVE_CACHE.mkdir(parents=True,exist_ok=True,mode=0o700)
 rows=live_search_records(mid)
 # Refresh ids if registry empty/stale but live has readable files.
 if not entry.get('record_file_ids'):
  inferred=infer_company_from_live(mid,rows)
  if inferred:
   _,fids=inferred
   entry=atomic_registry_upsert(mid,entry.get('mark') or 'company',entry.get('reason') or 'live MCP refresh record ids',fids)
  elif entry.get('mark')=='company' or is_feishu_assistant_created(mid):
   fids=record_file_ids_from_live_rows(rows)
   if fids:
    entry=atomic_registry_upsert(mid,entry.get('mark') or 'company',entry.get('reason') or 'live MCP refresh record ids for feishu/company',fids, allow_private_upgrade=is_feishu_assistant_created(mid))
 subject=next(((r.get('subject') or '').strip() for r in rows if (r.get('subject') or '').strip()),None)
 start=next((r.get('meeting_start_time') for r in rows if r.get('meeting_start_time')),None)
 if not subject or not start:raise PermissionError('Live MCP metadata incomplete')
 pieces=[];sources=[]
 action='minutes' if kind=='minutes' else 'transcript'
 for fid in entry['record_file_ids']:
  if not re.fullmatch(r'[0-9]+',str(fid)):continue
  data=mcp_call('record_content',{'action':action,'record_file_id':str(fid),'meeting_id':mid})
  if not isinstance(data,dict):continue
  text=text_from_record_payload(kind,data)
  if not text:continue
  p=LIVE_CACHE/f'record-{fid}-{kind}.json'
  if p.is_symlink():raise PermissionError('Symlink live cache forbidden')
  atomic_json(p,data);p.chmod(0o600)
  pieces.append('## 录制分段 '+str(fid)+'\n\n'+text)
  sources.append({'record_file_id':str(fid),'source_sha256':digest(p),'source_origin':'live-mcp'})
 if not pieces:raise ValueError('No complete readable live source text for requested material')
 header='# '+subject+' — '+('纪要' if kind=='minutes' else '逐字稿')+'\n\n会议ID：'+mid+'\n\n会议日期（北京时间）：'+start+'\n\n来源：腾讯会议 live MCP 录制文本；按需拉取，非本地归档快照。\n\n'
 return header+'\n\n'.join(pieces),sources

def source_text(mid,kind):
 entry=permitted(mid)
 archived=archive_source_text(mid,kind,entry)
 if archived:return archived
 return live_source_text(mid,kind,entry)

def source_blob_path(fid,kind,origin=None):
 live=LIVE_CACHE/f'record-{fid}-{kind}.json'
 arch=SOURCE/'texts'/f'record-{fid}-{kind}.json'
 if origin=='live-mcp' or (live.exists() and not live.is_symlink()):
  if live.exists() and not live.is_symlink():return live
 if arch.exists() and not arch.is_symlink():return arch
 raise PermissionError('Source blob missing')

def docx_write(path,text):
 with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
  z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
  body=''.join('<w:p><w:r><w:t xml:space="preserve">'+html.escape(t)+'</w:t></w:r></w:p>' for t in text.splitlines())
  z.writestr('word/document.xml','<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+body+'<w:sectPr/></w:body></w:document>')

def safe_filename(name):
 if not isinstance(name,str):raise ValueError('Missing API filename')
 name=re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', '_', name).strip().strip('.')
 if not name or len(name.encode('utf-8'))>240:raise ValueError('Unsafe/oversize API filename')
 return name

def native_name(mid,kind,sources):
 # Prefer archived API metadata; fall back to live search_records. Never use export clock alone.
 # Owner naming (forward-only, t145): transcript …-逐字稿文本.md ; minutes …-纪要文本.md
 permitted(mid)
 fids={s['record_file_id'] for s in sources}
 raw=[]
 raw_path=SOURCE/'meetings-raw.json'
 if raw_path.exists():
  raw=load(raw_path).get(mid,[])
 rows=[x for x in raw if x.get('source')=='search_records' and str(x.get('record_file_id')) in fids]
 if not rows:
  live_rows=live_search_records(mid)
  rows=[]
  for x in live_rows:
   if str(x.get('record_file_id')) in fids:
    y=dict(x);y['source']='search_records';rows.append(y)
 names=set(); evidence=[]
 for row in rows:
  original=row.get(kind+'_file_name') or row.get('original_'+kind+'_file_name')
  if not original and re.fullmatch(r'\d{14}-.+\.md',row.get('file_name','')):
   candidate=row['file_name']
   if kind=='transcript' and candidate.endswith('-逐字稿文本.md'):original=candidate
   elif kind=='minutes' and candidate.endswith('-纪要文本.md'):original=candidate
  if original:
   name=safe_filename(original)
   if not name.endswith('.md'):raise ValueError('API original is not Markdown')
   if kind=='transcript' and not name.endswith('-逐字稿文本.md'):raise ValueError('Unexpected transcript suffix')
   if kind=='minutes' and not name.endswith('-纪要文本.md'):raise ValueError('Unexpected minutes suffix')
   provenance={'field':'API original filename','value':original}
  else:
   stamp=row.get('meeting_start_time','')
   dt=datetime.datetime.fromisoformat(stamp)
   if dt.tzinfo is None:raise ValueError('API timestamp timezone unavailable')
   dt=dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
   title=row.get('subject')
   if not title:raise ValueError('API title unavailable')
   suffix='-逐字稿文本' if kind=='transcript' else '-纪要文本'
   name=safe_filename(dt.strftime('%Y%m%d%H%M%S')+'-'+title+suffix+'.md')
   provenance={'field':'search_records.meeting_start_time','value':stamp,'timezone':'Asia/Shanghai','title_field':'subject','mode':'owner-confirmed-naming-t145','native_minutes_suffix_verified':True if kind=='minutes' else None,'suffix':suffix}
  names.add(name);evidence.append(provenance)
 if len(names)!=1:raise ValueError('Missing or ambiguous native filename metadata')
 return names.pop(),evidence

def prepare(args):
 if set(args)!={'meeting_id','kind','format'}:raise ValueError('Only meeting_id/kind/format accepted')
 mid=args['meeting_id'];kind=args['kind'];fmt=args['format']
 if kind not in ['minutes','transcript','both'] or fmt!='md':raise ValueError('Unsupported kind/format; Markdown only')
 os.umask(0o077);OUT.mkdir(parents=True,exist_ok=True,mode=0o700)
 manifests=BASE/'exports';manifests.mkdir(exist_ok=True,mode=0o700);results=[]
 for k in ['minutes','transcript'] if kind=='both' else [kind]:
  text,sources=source_text(mid,k)
  for f in [fmt]:
   eid=hashlib.sha256((mid+k+f+text).encode()).hexdigest()[:24]
   name,naming=native_name(mid,k,sources)
   folder=OUT/eid
   if folder.is_symlink():raise PermissionError('Symlink folder forbidden')
   folder.mkdir(exist_ok=True,mode=0o700);p=folder/name
   if p.is_symlink():raise PermissionError('Symlink export forbidden')
   if f=='md':atomic_text(p,text)
   else:docx_write(p,text)
   p.chmod(0o600)
   m={'export_id':eid,'meeting_id':mid,'kind':k,'format':f,'file_name':p.name,'naming':naming,'naming_version':2,'sha256':digest(p),'source':sources,'recipient':OWNER,'registry_version':load(BASE/'registry.json')['version']}
   atomic_json(manifests/(eid+'.json'),m);results.append({'export_id':eid,'kind':k,'format':f,'file_name':p.name,'bytes':p.stat().st_size,'sent':False})
 return {'exports':results,'recipient':'fixed-owner-only','prepared_not_sent':True}

def atomic_text(path,text):
 import tempfile
 fd,tmp=tempfile.mkstemp(prefix='.export-',dir=str(path.parent))
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(text);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def atomic_json(path,data):
 import tempfile
 fd,tmp=tempfile.mkstemp(prefix='.'+path.name,dir=str(path.parent))
 try:
  with os.fdopen(fd,'w') as f:
   json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
  dfd=os.open(path.parent,os.O_RDONLY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)

def delivery_request_identity():
 ctx=REQUEST_CONTEXT.get() or {}
 msg=ctx.get('messageId')
 if not isinstance(msg,str) or not re.fullmatch(r'om_[a-zA-Z0-9_-]+',msg):
  raise PermissionError('Missing trusted inbound message ID; refusing send without request idempotency')
 return ctx.get('sessionKey'),msg

def read_feishu_message(message_id):
 return feishu_delivery.read_message(message_id,node_bin=NODE,script=CODE_DIR/'feishu_message_get.mjs')

def reconcile_delivery_result(result,dest):
 return feishu_delivery.reconcile_file(result,dest,readback=read_feishu_message)

def send(args):
 import fcntl
 dest=resolve_delivery_target(args.get('target_chat_id'))
 sk,msg=delivery_request_identity()
 eid=args.get('export_id','')
 if not re.fullmatch('[a-f0-9]{24}',eid):raise ValueError('Invalid export_id')
 manifest=load(BASE/'exports'/(eid+'.json'))
 # Per real inbound message / meeting / kind, not per file; next user message resends.
 key=hashlib.sha256(json.dumps([sk,msg,dest['cli_target'],manifest['meeting_id'],manifest['kind']],separators=(',',':')).encode()).hexdigest()
 folder=BASE/'request-deliveries';folder.mkdir(exist_ok=True,mode=0o700)
 ledger=folder/(key+'.json')
 with (folder/(key+'.lock')).open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  if ledger.exists():
   old=load(ledger);result=old.get('result') or {}
   if result.get('status')!='file-message-readback-verified':
    result=reconcile_delivery_result(result,dest)
    atomic_json(ledger,dict(old,phase='verified',result=result))
   return dict(result,meeting_id=manifest['meeting_id'],kind=manifest['kind'],idempotent_replay=True,inbound_message_id=msg,request_delivery_key=key)
  identity={'inbound_message_id':msg,'session_key':sk,'meeting_id':manifest['meeting_id'],'kind':manifest['kind'],'target':dest['cli_target']}
  # Durable reservation BEFORE any external side effect. Survives transcript-write errors/crash.
  atomic_json(ledger,dict(identity,phase='reserved',result={}))
  def progress(result):atomic_json(ledger,dict(identity,phase=result.get('status'),result=result))
  try:result=_send_once(args,progress)
  except Exception:
   # Reservation/progress remains. A repeat must reconcile, never resend blindly.
   raise
  progress(result)
  return dict(result,idempotent_replay=False,inbound_message_id=msg,request_delivery_key=key)

def _send_once(args,progress):
 # export_id required; optional target_chat_id only from deliver_meeting_markdown (allowlisted oc_*).
 keys=set(args)
 if 'export_id' not in keys or not keys.issubset({'export_id','target_chat_id'}) or not re.fullmatch('[a-f0-9]{24}',args['export_id']):raise ValueError('Only a valid export_id (and optional allowlisted target_chat_id) is accepted')
 # Explicit operator latch; no model tool can modify this file after fs/runtime tool denial.
 latch=BASE/'file-delivery-enabled.json'
 if not latch.exists() or load(latch).get('enabled') is not True:raise PermissionError('Sending disabled pending permission acceptance; no upload attempted')
 m=load(BASE/'exports'/(args['export_id']+'.json'));permitted(m['meeting_id'])
 if m['recipient']!=OWNER or m['export_id']!=args['export_id'] or m['format']!='md':raise PermissionError('Manifest attribution mismatch')
 name,naming=native_name(m['meeting_id'],m['kind'],m['source'])
 # Legacy hashed manifests cannot reach upload; reprepare is explicit and local.
 if m.get('naming_version')!=2 or m['file_name']!=name or m.get('naming')!=naming:raise PermissionError('Legacy/stale filename; prepare again before sending')
 folder=OUT/args['export_id'];p=folder/name
 if folder.is_symlink() or p.is_symlink() or p.resolve().parent!=folder.resolve() or digest(p)!=m['sha256']:raise PermissionError('Export path/hash mismatch')
 for item in m['source']:
  if item['record_file_id'] not in permitted(m['meeting_id'])['record_file_ids']:raise PermissionError('Source changed or unowned')
  sp=source_blob_path(item['record_file_id'],m['kind'],item.get('source_origin'))
  if digest(sp)!=item['source_sha256']:raise PermissionError('Source changed or unowned')
 receipt=BASE/'exports'/(args['export_id']+'.receipt.json')
 # Request ledgers own delivery history. Legacy last-export receipt is not a send gate.
 env=dict(os.environ);env['PATH']=NODE+':/opt/homebrew/bin:/usr/bin:/bin'
 dest=resolve_delivery_target(args.get('target_chat_id'))
 # Group asks must stay on chat_id; never silently rewrite an allowlisted group target to OWNER DM.
 if dest['kind']=='group' and (not str(dest['chat_id']).startswith('oc_') or dest['cli_target']==OWNER):
  raise PermissionError('Refusing to deliver group-originated export to owner DM')
 proc=subprocess.run([CLI,'message','send','--channel','feishu','--target',dest['cli_target'],'--media',str(p),'--json'],capture_output=True,text=True,env=env,timeout=90)
 # Retain only sanitized receipt fields, never raw SDK exception/request headers.
 def objects(text):
  found=[];dec=json.JSONDecoder()
  for i,char in enumerate(text):
   if char=='{':
    try:found.append(dec.raw_decode(text[i:])[0])
    except Exception:pass
  return found
 def values(o,keys):
  out=[]
  if isinstance(o,dict):
   for k,v in o.items():
    if k in keys and isinstance(v,str):out.append(v)
    elif isinstance(v,(dict,list)):out+=values(v,keys)
  elif isinstance(o,list):
   for v in o:out+=values(v,keys)
  return out
 ids=values(objects(proc.stdout),{'messageId','message_id'})
 mids=[v for v in ids if re.fullmatch(r'om_[a-zA-Z0-9_-]+',v)]
 result={'export_id':args['export_id'],'meeting_id':m['meeting_id'],'kind':m['kind'],'official_delivery_exit_code':proc.returncode,'recipient':dest['cli_target'],'receive_id_type':'chat_id' if dest['kind']=='group' else 'open_id','chat_id':dest['chat_id'] if dest['kind']=='group' else None,'delivery_kind':dest['kind'],'delivery_source':dest['source'],'message_id':mids[0] if mids else None,'file_key':None,'file_name':name,'sha256':m['sha256'],'status':'sent-unverified' if proc.returncode==0 else 'failed-no-automatic-retry'}
 progress(result)  # Persist candidate message ID before readback/internal transcript handling.
 if result['message_id']:
  # Official im.message.get via installed Lark SDK. CLI message read with open_id target is invalid.
  helper=str(CODE_DIR/'feishu_message_get.mjs')
  read=subprocess.run([NODE+'/node',helper,result['message_id']],capture_output=True,text=True,env=env,timeout=60)
  try:rb=json.loads(read.stdout.strip() or '{}')
  except Exception:rb={'ok':False,'error':'invalid readback json','stderr':read.stderr[-500:]}
  result['file_key']=rb.get('file_key') if isinstance(rb.get('file_key'),str) and re.fullmatch(r'file_[a-zA-Z0-9_-]+',rb.get('file_key') or '') else None
  result['msg_type']=rb.get('msg_type')
  result['readback_file_name']=rb.get('file_name')
  result['readback_chat_id']=rb.get('chat_id')
  result['readback_api']='im.message.get'
  result['readback_api_code']=rb.get('api_code')
  ok=bool(result['file_key']) and result.get('msg_type')=='file' and result.get('readback_file_name')==name and rb.get('ok') is True and (dest['kind']!='group' or rb.get('chat_id')==dest['chat_id'])
  result['readback_ok']=ok
  result['status']='file-message-readback-verified' if ok else 'sent-readback-unverified'
 progress(result)  # Verified external outcome is durable before returning to MCP.
 atomic_json(receipt,result);receipt.chmod(0o600)
 if proc.returncode:raise RuntimeError('Official Feishu delivery failed; sanitized receipt retained; no automatic retry')
 if result['status']!='file-message-readback-verified':
  raise RuntimeError('Feishu send exited 0 but file message was not verified; refusing false success. status='+result['status'])
 return result

TOOLS.append({'name':'deliver_meeting_markdown','description':'每条新入站消息重新交付一次；同条消息内部重试不得重复发送。逐字稿只选kind=transcript，纪要只选kind=minutes，只有用户明确同时要两者才选both，禁止混发。群聊入站必须把 Conversation info 的 chat_id 传入 target_chat_id（chat:oc_…/oc_…），文件发回同一群，禁止发到私聊/open_id。私聊可省略 target_chat_id。新消息不论此前是否发过都重新上传，同消息重复调用复用服务器请求级收据。成功后助手必须只输出 NO_REPLY（禁止任何配文/英文tool-finished/已发送/往上翻）。MD-only。','inputSchema':{'type':'object','additionalProperties':False,'properties':{'meeting_id':{'type':'string','pattern':'^[0-9]+$'},'kind':{'type':'string','enum':['minutes','transcript','both'],'description':'逐字稿仅transcript；会议纪要仅minutes；仅明确同时要两者才both，禁止混发'},'target_chat_id':{'type':'string','description':'Group ask: required. Pass Conversation info chat_id (chat:oc_… or oc_…). File is delivered to that same group. DM ask: omit. Never pass open_id/ou_.'}},'required':['meeting_id','kind']}})
TOOLS.append({'name':'deliver_meeting_recording_link','description':'用户索取某场会议录播链接时调用。仅向原会话投递一条纯文本卡片（群聊必须传 target_chat_id=本轮 chat_id；私聊省略）。正文严格三行：回顾: {真实会议名}\\n日期: {YYYY-MM-DD HH:mm:ss Asia/Shanghai 可证时间}\\n回顾文件：{接口真实分享URL（优先 crm，若仅有 crw 则用真实 crw，禁止把 crw 改成 crm）}；不附逐字稿/纪要MD、不配文、不加时长说明。成功后助手只输出 NO_REPLY。查无/未就绪/缺可证日期时工具报错，助手用一句中文真实失败，禁止编造链接或时间。','inputSchema':{'type':'object','additionalProperties':False,'properties':{'meeting_id':{'type':'string','pattern':'^[0-9]+$'},'target_chat_id':{'type':'string','description':'Group ask: required Conversation info chat_id (chat:oc_… or oc_…). DM: omit. Never open_id/ou_.'}},'required':['meeting_id']}})


def request_completion_path():
 sk,msg=delivery_request_identity()
 key=hashlib.sha256(json.dumps([sk,msg],separators=(',',':')).encode()).hexdigest()
 folder=BASE/'request-completions';folder.mkdir(exist_ok=True,mode=0o700)
 return folder/(key+'.json')

def record_request_completion(args,delivered):
 sk,msg=delivery_request_identity()
 kinds=['minutes','transcript'] if args['kind']=='both' else [args['kind']]
 atomic_json(request_completion_path(),{'schema':'tencent-file-delivery-v1','session_key':sk,'inbound_message_id':msg,'verified':True,'requested':[{'meeting_id':args['meeting_id'],'kind':k} for k in kinds],'deliveries':delivered})

def deliver(args):
 keys=set(args)
 if 'meeting_id' not in keys or 'kind' not in keys or not keys.issubset({'meeting_id','kind','target_chat_id'}):raise ValueError('Only meeting_id/kind and optional target_chat_id accepted')
 # Resolve before prepare so a bad/non-allowlisted group target fails closed with no upload.
 dest=resolve_delivery_target(args.get('target_chat_id'))
 if dest['kind']=='group' and permitted(args['meeting_id']).get('mark')!='company':raise PermissionError('Private/pending export cannot be delivered to a group')
 prepared=prepare({'meeting_id':args['meeting_id'],'kind':args['kind'],'format':'md'})
 delivered=[]
 for x in prepared['exports']:
  payload={'export_id':x['export_id']}
  if dest['kind']=='group':
   payload['target_chat_id']=dest['chat_id']  # normalized oc_*; send() will prefix chat:
  out=send(payload)
  # Must be a fresh upload this call; already-delivered / re_sent=false is not success.
  verified=(out.get('status')=='file-message-readback-verified' and out.get('file_name')==x['file_name'] and out.get('message_id') and out.get('file_key'))
  if not verified:
   raise RuntimeError('Delivery not verified as current Markdown file attachment: '+json.dumps(out,ensure_ascii=False))
  # Group-originated delivery must not land on owner DM.
  if dest['kind']=='group' and (out.get('receive_id_type')!='chat_id' or out.get('chat_id')!=dest['chat_id'] or out.get('recipient')==OWNER):
   raise RuntimeError('Group delivery target mismatch; file must go to the same group chat_id, not owner DM')
  delivered.append(out)
 record_request_completion(args,delivered)  # Durable evidence consumed by final channel completion.
 return {'delivery':delivered,'file_names':[x['file_name'] for x in prepared['exports']],'verified':True,'force_resent':True,'same_request_deduplicated':all(x.get('idempotent_replay') for x in delivered),'delivery_kind':dest['kind'],'chat_id':dest.get('chat_id') if dest['kind']=='group' else None,'reply_instruction':'Each NEW inbound message requests one delivery per kind; retries of SAME message reuse verified receipt, never upload twice. After verified attachment this turn: output ONLY NO_REPLY (empty). Never say 已发送/已经发过/往上翻, never any caption, never English tool-finished text, never paste paths/body. Failures: one short real error only.'}

def handle(name,args,request_context=None):
 token=REQUEST_CONTEXT.set(request_context)
 try:return _handle(name,args)
 except Exception:
  if name in ('deliver_meeting_markdown','deliver_meeting_recording_link','send_prepared_meeting_export'):
   try:atomic_json(request_completion_path(),{'schema':'tencent-file-delivery-v1','verified':False,'reason':'export-call-failed'})
   except Exception:pass  # Never replace the real error; successful per-kind ledgers remain intact.
  raise
 finally:REQUEST_CONTEXT.reset(token)

def _handle(name,args):
 if name in ('get_meeting_delivery_status','retry_meeting_delivery'):
  import meeting_delivery
  # 本模块也可能由入口桥通过 importlib 加载，因此传递当前服务接口。
  from types import SimpleNamespace
  service=SimpleNamespace(**globals())
  if name=='get_meeting_delivery_status':return meeting_delivery.status(service,args)
  return meeting_delivery.retry(service,args)
 if name=='deliver_meeting_markdown':return deliver(args)
 if name=='deliver_meeting_recording_link':return deliver_recording_link(args)
 if name=='prepare_meeting_export':return prepare(args)
 if name=='send_prepared_meeting_export':
  out=send(args);manifest=load(BASE/'exports'/(args['export_id']+'.json'))
  record_request_completion({'meeting_id':manifest['meeting_id'],'kind':manifest['kind']},[out])
  return out
 raise ValueError('Unknown export tool')

# --- T142 post-meeting poller delivery (auto-send; separate from inbound) ---
AUTO_SEND_LEDGER = BASE / 'auto-send-ledger'
AI_GROUP_CHAT_ID = AUTO_SEND_GROUP_ID
POLLER_IDENTITY = 'post-meeting-poller-v1'


def deliver_recording_link(args):
 """Manual Feishu ask: send only the recording share card text to same chat. No MD attachments."""
 keys=set(args)
 if 'meeting_id' not in keys or not keys.issubset({'meeting_id','target_chat_id'}):
  raise ValueError('Only meeting_id and optional target_chat_id accepted')
 mid=str(args['meeting_id'])
 if not re.fullmatch(r'[0-9]+', mid):
  raise ValueError('Invalid meeting_id')
 dest=resolve_delivery_target(args.get('target_chat_id'))
 # Privacy: group only company; DM owner retains access to registered meetings via permitted()
 entry=permitted(mid)
 if dest['kind']=='group' and entry.get('mark')!='company':
  raise PermissionError('Private/pending recording link cannot be delivered to a group')
 picked=pick_cloud_recording_for_share(mid)
 if not picked.get('ok'):
  err=picked.get('error') or 'recording_unavailable'
  # Short stable errors for model to relay
  mapping={
   'no_cloud_recording':'未找到该会议的云录制',
   'no_stable_crw_view_address':'回顾/录播分享链接尚未就绪',
   'missing_actual_record_time_window':'录播时间尚未就绪',
   'mcp_error':'腾讯会议接口暂时不可用（可能达到调用上限）',
  }
  raise RuntimeError(mapping.get(err, '录播不可用：'+str(err)))
 url=picked.get('view_address')
 card=picked.get('manual_card_text')
 if not card:
  raise RuntimeError('录播分享链接尚未就绪' if not is_stable_recording_share_url(url) else '录播时间尚未就绪')
 if not is_stable_recording_share_url(url) or url not in card:
  raise RuntimeError('录播分享链接尚未就绪')
 # Send text to resolved target (group chat:oc_ or owner DM)
 import fcntl
 kind='manual_recording_link'
 # Manual asks re-send each inbound message; use request messageId in ledger key when present
 ctx=REQUEST_CONTEXT.get() or {}
 req=str(ctx.get('messageId') or 'no-msg')
 # separate ledger namespace under auto-send-ledger with hashed key including request id
 key=hashlib.sha256(json.dumps([POLLER_IDENTITY,'manual-recording-link', mid, dest.get('chat_id') or dest.get('cli_target'), req], separators=(',',':')).encode()).hexdigest()
 AUTO_SEND_LEDGER.mkdir(parents=True, exist_ok=True, mode=0o700)
 ledger=AUTO_SEND_LEDGER/(key+'.json')
 lock_path=AUTO_SEND_LEDGER/(key+'.lock')
 with lock_path.open('a') as lock:
  fcntl.flock(lock, fcntl.LOCK_EX)
  old=load(ledger) if ledger.exists() else None
  if old and (old.get('result') or {}).get('status')=='text-message-readback-verified':
   # same inbound message retry: idempotent replay, still success for NO_REPLY
   return {'verified':True,'idempotent_replay':True,'card_text':card,'view_address':url,'url_kind':picked.get('url_kind'),'date_source':picked.get('date_source'),'manual_date':picked.get('manual_date'),'delivery_kind':dest['kind'],'chat_id':dest.get('chat_id'),'message_id':(old.get('result') or {}).get('message_id'),'status':'text-message-readback-verified','reply_instruction':'NO_REPLY'}
  env=os.environ.copy()
  proc=subprocess.run([CLI,'message','send','--channel','feishu','--target',dest['cli_target'],'--message',card,'--json'],capture_output=True,text=True,env=env,timeout=90)
  if proc.returncode!=0:
   raise RuntimeError('录播链接发送失败')
  try: payload=json.loads(proc.stdout or '{}')
  except Exception: payload={}
  def find_id(o):
   if isinstance(o,dict):
    for k,v in o.items():
     if k in ('messageId','message_id','id') and isinstance(v,str) and v.startswith('om_'): return v
     found=find_id(v)
     if found: return found
   if isinstance(o,list):
    for x in o:
     found=find_id(x)
     if found: return found
   return None
  msg_id=find_id(payload)
  if not msg_id:
   raise RuntimeError('录播链接发送失败：无 message_id')
  rb=read_feishu_message(msg_id)
  ok=_text_readback_ok(rb, url, dest)
  if dest['kind']=='group':
   ok=ok and rb.get('chat_id')==dest['chat_id']
  result={'status':'text-message-readback-verified' if ok else 'sent-text-readback-unverified','message_id':msg_id,'chat_id':rb.get('chat_id'),'msg_type':rb.get('msg_type'),'view_address':url}
  atomic_json(ledger, {'identity':POLLER_IDENTITY,'kind':kind,'meeting_id':mid,'target':dest['cli_target'],'phase':result['status'],'result':result,'card_text':card})
  if not ok:
   raise RuntimeError('录播链接发送未通过回读校验')
  return {'verified':True,'idempotent_replay':False,'card_text':card,'view_address':url,'url_kind':picked.get('url_kind'),'date_source':picked.get('date_source'),'manual_date':picked.get('manual_date'),'delivery_kind':dest['kind'],'chat_id':dest.get('chat_id') or rb.get('chat_id'),'message_id':msg_id,'status':'text-message-readback-verified','reply_instruction':'NO_REPLY'}


def auto_send_ledger_key(meeting_id, kind, target_chat_id):
 mid=str(meeting_id); kind=str(kind); chat=normalize_group_chat_id(target_chat_id)
 if not chat:raise ValueError('Invalid target_chat_id for auto-send ledger')
 if kind not in ('minutes','transcript','recording_link'):raise ValueError('Invalid kind')
 return hashlib.sha256(json.dumps([POLLER_IDENTITY, mid, kind, chat], separators=(',',':')).encode()).hexdigest()

def resolve_poller_target(target_chat_id):
 chat=normalize_group_chat_id(target_chat_id)
 if not chat:raise PermissionError('Poller requires allowlisted oc_* group target')
 if chat not in allowed_group_chat_ids():raise PermissionError('Poller target group not allowlisted')
 return {'cli_target':'chat:'+chat,'chat_id':chat,'kind':'group','source':'post-meeting-poller'}

def material_ready(meeting_id, kind):
 """Return {ok, file_name?, error?, bytes?} without sending. Live MCP preferred."""
 mid=str(meeting_id)
 try:
  if not is_feishu_assistant_created(mid) and FEISHU_CREATED_REASON not in str((load(BASE/'registry.json').get('meetings') or {}).get(mid,{}).get('reason') or ''):
   return {'ok':False,'kind':kind,'error':'not_feishu_assistant_created'}
  entry=ensure_registered(mid)
  if entry.get('mark')!='company':
   return {'ok':False,'kind':kind,'error':'not_company'}
  text,sources=source_text(mid,kind)
  if not text or not text.strip():
   return {'ok':False,'kind':kind,'error':'empty_text'}
  name,naming=native_name(mid,kind,sources)
  return {'ok':True,'kind':kind,'file_name':name,'bytes':len(text.encode('utf-8')),'sources':len(sources)}
 except Exception as ex:
  return {'ok':False,'kind':kind,'error':type(ex).__name__+': '+str(ex)[:400]}

def dual_ready(meeting_id):
 t=material_ready(meeting_id,'transcript')
 m=material_ready(meeting_id,'minutes')
 return {'transcript':t,'minutes':m,'both_ready':bool(t.get('ok') and m.get('ok'))}

def load_auto_send_ledger(meeting_id, kind, target_chat_id):
 AUTO_SEND_LEDGER.mkdir(parents=True, exist_ok=True, mode=0o700)
 key=auto_send_ledger_key(meeting_id, kind, target_chat_id)
 path=AUTO_SEND_LEDGER/(key+'.json')
 if not path.exists():return None, key, path
 return load(path), key, path

def poller_send_prepared(export_id, target_chat_id, dry_run=False):
 """Send one prepared export to allowlisted group using poller ledger (not inbound messageId)."""
 import fcntl
 dest=resolve_poller_target(target_chat_id)
 m=load(BASE/'exports'/(export_id+'.json'))
 mid=m['meeting_id']; kind=m['kind']
 if not is_feishu_assistant_created(mid) and FEISHU_CREATED_REASON not in str((load(BASE/'registry.json').get('meetings') or {}).get(mid,{}).get('reason') or ''):
  raise PermissionError('Auto-send refused: meeting lacks feishu-assistant provenance')
 if permitted(mid).get('mark')!='company':
  raise PermissionError('Auto-send refused: not company')
 old,key,ledger=load_auto_send_ledger(mid, kind, dest['chat_id'])
 lock_path=AUTO_SEND_LEDGER/(key+'.lock')
 with lock_path.open('a') as lock:
  fcntl.flock(lock, fcntl.LOCK_EX)
  old=load(ledger) if ledger.exists() else None
  if old and (old.get('result') or {}).get('status')=='file-message-readback-verified':
   return dict(old['result'], idempotent_replay=True, auto_send_key=key, dry_run=False)
  if dry_run:
   return {'status':'dry-run-would-send','meeting_id':mid,'kind':kind,'file_name':m.get('file_name'),'chat_id':dest['chat_id'],'export_id':export_id,'auto_send_key':key,'dry_run':True}
  # Reconcile prior candidate if any
  if old and (old.get('result') or {}).get('message_id'):
   try:
    result=reconcile_delivery_result(old['result'], dest)
    atomic_json(ledger, dict(old, phase='verified', result=result))
    return dict(result, idempotent_replay=True, auto_send_key=key)
   except Exception:
    # If readback fails but we have a message_id, do not blind resend.
    raise RuntimeError('Auto-send prior candidate unverified; refusing blind resend for '+mid+'/'+kind)
  if old:
   raise RuntimeError("Auto-send outcome uncertain; no message ID; refusing blind resend")
  # Reserve before external side effects.
  identity={'identity':POLLER_IDENTITY,'meeting_id':mid,'kind':kind,'target':dest['cli_target'],'chat_id':dest['chat_id'],'export_id':export_id}
  atomic_json(ledger, dict(identity, phase='reserved', result={}))
  token=REQUEST_CONTEXT.set({'sessionKey':'agent:main:feishu:group:'+dest['chat_id'],'messageId':'om_poller_'+key[:24]})
  try:
   def progress(result):
    atomic_json(ledger, dict(identity, phase=result.get('status'), result=result))
   # _send_once uses resolve_delivery_target(explicit) via args target_chat_id + REQUEST_CONTEXT
   result=_send_once({'export_id':export_id,'target_chat_id':dest['chat_id']}, progress)
   progress(result)
   return dict(result, idempotent_replay=False, auto_send_key=key)
  finally:
   REQUEST_CONTEXT.reset(token)


def format_duration_seconds(sec):
 sec=int(round(float(sec)))
 if sec < 0: sec=0
 h, rem = divmod(sec, 3600)
 m, s = divmod(rem, 60)
 if h and m:
  return f'{h}小时{m}分钟'
 if h and s:
  return f'{h}小时{s}秒'
 if h:
  return f'{h}小时'
 if m and s:
  return f'{m}分钟{s}秒'
 if m:
  return f'{m}分钟'
 return f'{s}秒'


def format_meeting_time_range_shanghai(start_dt, end_dt):
 """Human range in Asia/Shanghai; include year on cross-year, date on both sides when cross-day."""
 TZ=datetime.timezone(datetime.timedelta(hours=8))
 if start_dt.tzinfo is None: start_dt=start_dt.replace(tzinfo=TZ)
 else: start_dt=start_dt.astimezone(TZ)
 if end_dt.tzinfo is None: end_dt=end_dt.replace(tzinfo=TZ)
 else: end_dt=end_dt.astimezone(TZ)
 def md(dt, with_year=False):
  if with_year:
   return f'{dt.year}年{dt.month}月{dt.day}日 {dt.strftime("%H:%M")}'
  return f'{dt.month}月{dt.day}日 {dt.strftime("%H:%M")}'
 cross_year = start_dt.year != end_dt.year
 cross_day = start_dt.date() != end_dt.date()
 if cross_year:
  return f'{md(start_dt, True)} 至 {md(end_dt, True)}'
 if cross_day:
  return f'{md(start_dt)} 至 {md(end_dt)}'
 return f'{md(start_dt)} 至 {end_dt.strftime("%H:%M")}'


def is_stable_recording_share_url(url):
 """Accept real meeting.tencent.com share paths from API: crw or crm. Never rewrite crw→crm."""
 if not isinstance(url, str):
  return False
 u=url.strip().split('?',1)[0]
 return bool(re.fullmatch(r'https://meeting\.tencent\.com/(?:crw|crm)/[A-Za-z0-9]+', u))


def is_crm_review_share_url(url):
 if not isinstance(url, str):
  return False
 u=url.strip().split('?',1)[0]
 return bool(re.fullmatch(r'https://meeting\.tencent\.com/crm/[A-Za-z0-9]+', u))


def format_shanghai_wall_clock(dt):
 """Single Asia/Shanghai wall clock: YYYY-MM-DD HH:mm:ss."""
 TZ=datetime.timezone(datetime.timedelta(hours=8))
 if dt.tzinfo is None:
  dt=dt.replace(tzinfo=TZ)
 else:
  dt=dt.astimezone(TZ)
 return dt.strftime('%Y-%m-%d %H:%M:%S')


def format_manual_review_card(subject, date_dt, view_url):
 """Shared auto+manual review card — exactly three lines, no duration/caption."""
 name=(subject or '').strip() or '会议'
 date_s=format_shanghai_wall_clock(date_dt)
 url=(view_url or '').strip().split('?',1)[0]
 if not is_stable_recording_share_url(url):
  raise ValueError('manual review card requires real crw/crm share url')
 return f'回顾: {name}\n日期: {date_s}\n回顾文件：{url}'


def parse_iso_dt(value):
 if not value: return None
 if isinstance(value,(int,float)):
  return datetime.datetime.fromtimestamp(value, datetime.timezone(datetime.timedelta(hours=8)))
 s=str(value).strip()
 try:
  dt=datetime.datetime.fromisoformat(s)
 except Exception:
  return None
 if dt.tzinfo is None:
  dt=dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
 return dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))


def load_record_acl_wanted():
 import record_acl_grant as acl
 cfg_path=OPENCLAW_AUTOMATION_CONFIG
 cfg=load(cfg_path) if cfg_path.exists() else {}
 ra=cfg.get('record_acl') or {}
 members_path=ra.get('members_config') or str(BASE/'record-acl-members.json')
 doc=acl.load_members_config(members_path)
 wanted=acl.active_member_targets(doc)
 perm=int(ra.get('permission') if ra.get('permission') is not None else 1)
 for w in wanted:
  w['permission']=perm
 return wanted, ra


def verify_record_acl_members(meeting_record_id, wanted=None):
 """Readback via access_members. Success = all wanted present (可查看 accepted; API often ignores permission=1)."""
 import record_acl_grant as acl
 if wanted is None:
  wanted,_=load_record_acl_wanted()
 body=acl.fetch_all_members(lambda name,args: acl.audited_mcp(mcp_call,name,args), str(meeting_record_id))
 idx=acl.members_index(body)
 present=[]; missing=[]
 for w in wanted:
  ex=idx.get(w['open_id'])
  if ex and ex.get('permission') is not None and ex['permission'] >= 0:
   raw=((ex.get('raw') or {}).get('permission') if isinstance(ex.get('raw'),dict) else None)
   present.append({'display_name':w['display_name'],'permission':raw if raw is not None else ex.get('permission')})
  else:
   missing.append(w['display_name'])
 return {
  'ok': len(missing)==0 and len(wanted)>0 and acl.members_evidence_complete(body),
  'members_complete': acl.members_evidence_complete(body),
  'present': present,
  'missing': missing,
  'total_count': body.get('total_count'),
  'meeting_record_id': str(meeting_record_id),
 }


def ensure_and_verify_recording_acl(meeting_id, meeting_record_ids, *, acl_cache=None, accept_view=True):
 """Grant+verify ready records when record_acl.enabled. Disabled t283u (user set meetings world-viewable).
 Not a send gate (t279u). If accept_view: do not re-add for manage-upgrade when already present.
 acl_cache may carry verified_record_ids to skip repeat writes within B window. Mutates cache."""
 import record_acl_grant as acl
 wanted, ra = load_record_acl_wanted()
 if not ra.get('enabled'):
  return {'ok':False,'skipped':'acl_disabled','records':[],'wanted_names':[w.get('display_name') for w in wanted],'acl_cache': acl_cache if isinstance(acl_cache, dict) else {}}
 if not wanted:
  return {'ok':False,'error':'no_active_acl_members','records':[]}
 cache = acl_cache if isinstance(acl_cache, dict) else {}
 verified = set(str(x) for x in (cache.get('verified_record_ids') or []))
 req_perm = int(ra.get('permission') if ra.get('permission') is not None else 1)
 results=[]
 all_ok=True
 degraded_any=False
 for mrid in meeting_record_ids:
  mrid=str(mrid)
  if mrid in verified:
   results.append({'meeting_record_id':mrid,'ok':True,'cached':True,'grant_action':'cache_hit'})
   continue
  # Presence-first: only add missing members; never infinite manage-upgrade writes.
  body=acl.fetch_all_members(lambda name,args: acl.audited_mcp(mcp_call,name,args), mrid)
  if not acl.members_evidence_complete(body):
   all_ok=False
   results.append({'meeting_record_id':mrid,'ok':False,'grant_action':'members_incomplete',
                   'members_ok':False,'missing':[w['display_name'] for w in wanted]})
   continue
  idx=acl.members_index(body)
  to_add=[]
  degraded=[]
  for w in wanted:
   oid=w['open_id']
   ex=idx.get(oid)
   if not ex:
    to_add.append({'open_id':oid,'permission':req_perm,'display_name':w.get('display_name')})
    continue
   have=ex.get('permission')
   if have is not None and req_perm > 0 and have < req_perm:
    degraded.append({'display_name':w.get('display_name'),'have':have,'requested':req_perm})
    degraded_any=True
    # accept_view: do not re-add for upgrade
  grant_action='noop_present'
  write_status=None
  if to_add:
   share_patch={}
   if ra.get('enable_download', True):
    share_patch['download_enable']=True
   if ra.get('ensure_view_minutes', True):
    share_patch['view_minutes_enable']=True
   args={'meeting_record_id':mrid,'add_members':[{'open_id':x['open_id'],'permission':int(x['permission'])} for x in to_add]}
   if share_patch and ra.get('preserve_sharing_mode', True):
    args['sharing_config']=share_patch
   try:
    response=acl.audited_mcp(mcp_call, 'record_share_set', args)
    write_status=acl.inspect_share_result(response,args,wanted)
    grant_action='add_members'
   except Exception as ex:
    grant_action='add_error:'+type(ex).__name__
    write_status={'ok':False,'errors':[type(ex).__name__],'failed_names':[]}
  elif ra.get('enable_download', True) or ra.get('ensure_view_minutes', True):
   # members present; still ensure switches without member churn
   share_patch={}
   if ra.get('enable_download', True): share_patch['download_enable']=True
   if ra.get('ensure_view_minutes', True): share_patch['view_minutes_enable']=True
   if share_patch:
    try:
     args={'meeting_record_id':mrid,'sharing_config':share_patch}
     response=acl.audited_mcp(mcp_call, 'record_share_set', args)
     write_status=acl.inspect_share_result(response,args,wanted)
     grant_action='share_patch_only'
    except Exception as ex:
     grant_action='share_patch_error:'+type(ex).__name__
     write_status={'ok':False,'errors':[type(ex).__name__],'failed_names':[]}
  if write_status is not None and not write_status['ok']:
   all_ok=False
   results.append({'meeting_record_id':mrid,'ok':False,'grant_action':grant_action,
                   'members_ok':False,'missing':[w['display_name'] for w in wanted if w['open_id'] not in idx],
                   'failed_names':write_status.get('failed_names',[]),'write_status':write_status})
   break
  ver=verify_record_acl_members(mrid, wanted)
  if not ver.get('members_complete',False):
   all_ok=False
   results.append({'meeting_record_id':mrid,'ok':False,'grant_action':grant_action,
                   'members_ok':False,'missing':ver['missing'],'write_status':write_status})
   break
  sharing=acl.audited_mcp(mcp_call,'record_access',{'action':'sharing','meeting_record_id':mrid}) or {}
  cur=acl.sharing_current(sharing)
  if ra.get('enable_download', True):
   switches_ok=bool(cur.get('download_enable') in (True,1,'true','True')) and bool(cur.get('view_minutes_enable') in (True,1,'true','True'))
  else:
   switches_ok=bool(cur.get('view_minutes_enable') in (True,1,'true','True') or not ra.get('ensure_view_minutes', True))
  ok=bool(ver['ok'] and switches_ok and not acl.response_errors(sharing))
  if not ok: all_ok=False
  if ok:
   verified.add(mrid)
  results.append({
   'meeting_record_id': mrid,
   'grant_action': grant_action,
   'members_ok': ver['ok'],
   'write_status': write_status,
   'missing': ver['missing'],
   'present_names': [p['display_name'] for p in ver['present']],
   'permission_values': sorted({str(p.get('permission')) for p in ver['present']}),
   'permission_requested': req_perm,
   'permission_degraded': degraded,
   'sharing': {k:cur.get(k) for k in ('download_enable','view_minutes_enable','permission_point_id','switch_state')},
   'ok': ok,
  })
 cache['verified_record_ids']=sorted(verified)
 if degraded_any:
  cache['permission_degraded']=True
 return {'ok': all_ok, 'wanted_names':[w['display_name'] for w in wanted], 'records': results, 'permission_degraded': degraded_any, 'acl_cache': cache}


def pick_cloud_recording_for_share(meeting_id):
 """Prefer cloud video; share URL prefers real crm then real crw (never rewrite). Times from search_records."""
 mid=str(meeting_id)
 info=mcp_call('get_meeting',{'meeting_id':mid,'is_compact':True}) or {}
 if isinstance(info,dict) and info.get('error_info'):
  return {'ok':False,'error':'mcp_error','detail':info.get('error_info')}
 meeting=(info.get('meeting_info_list') or [{}])[0]
 subject=(meeting.get('subject') or '').strip() or '会议'
 sr=mcp_call('search_records',{'meeting_id':mid,'page_size':50,'is_compact':False}) or {}
 if isinstance(sr,dict) and sr.get('error_info'):
  return {'ok':False,'error':'mcp_error','detail':sr.get('error_info')}
 rows=list(sr.get('records') or [])
 # Prefer video / 云录制
 candidates=[]
 for r in rows:
  ft=(r.get('file_type') or '').lower()
  if ft in ('video','') or 'video' in ft:
   candidates.append(r)
 if not candidates:
  # fall back to get_meeting records typed 云录制
  for r in (meeting.get('records') or []):
   if '云录制' in str(r.get('type') or '') or str(r.get('type')).lower()=='video':
    candidates.append({
     'meeting_record_id': r.get('meeting_record_id'),
     'record_file_id': r.get('record_file_id'),
     'file_type': 'video',
     'subject': r.get('subject') or subject,
     'record_start_time': r.get('media_start_time'),
     'record_end_time': None,
     'duration_seconds': None,
     'duration': r.get('duration'),
     'meeting_start_time': meeting.get('start_time'),
    })
 if not candidates:
  return {'ok':False,'error':'no_cloud_recording'}
 # Prefer real crm (回顾) when address() returns it; else real crw. Never rewrite crw→crm.
 crm_view=None; crw_view=None; chosen_crm=None; chosen_crw=None
 for r in candidates:
  mrid=str(r.get('meeting_record_id') or '')
  if not mrid or mrid=='0':
   continue
  address=mcp_call('record_access',{'action':'address','meeting_record_id':mrid}) or {}
  if isinstance(address,dict) and address.get('error_info'):
   continue
  files=address.get('record_files') or []
  for f in files:
   url=(f.get('view_address') or '').split('?',1)[0].strip()
   if is_crm_review_share_url(url) and not crm_view:
    crm_view=url; chosen_crm=r
   elif ('/crw/' in url) and is_stable_recording_share_url(url) and not crw_view:
    crw_view=url; chosen_crw=r
 if crm_view:
  chosen=chosen_crm; view=crm_view; url_kind='crm'
 elif crw_view:
  chosen=chosen_crw; view=crw_view; url_kind='crw'
 else:
  return {'ok':False,'error':'no_stable_crw_view_address','candidates':len(candidates)}
 start=parse_iso_dt(chosen.get('record_start_time'))
 end=parse_iso_dt(chosen.get('record_end_time'))
 dur=chosen.get('duration_seconds')
 if start and end:
  dur=int((end-start).total_seconds())
 elif start and chosen.get('duration'):
  # parse HH:MM:SS
  try:
   hh,mm,ss=[int(x) for x in str(chosen.get('duration')).split(':')]
   dur=hh*3600+mm*60+ss
   end=start+datetime.timedelta(seconds=dur)
  except Exception:
   pass
 # Optional legacy metadata (range/duration) when full record window exists — not used in card text.
 have_window=bool(start and end and dur is not None and dur>=0)
 time_line=format_meeting_time_range_shanghai(start, end) if have_window else None
 dur_text=format_duration_seconds(dur) if have_window else None
 # Shared auto+manual date: prefer record_start_time; else meeting.start_time. Missing => fail.
 date_dt=start
 date_source='record_start_time'
 if date_dt is None:
  date_dt=parse_iso_dt(meeting.get('start_time') or chosen.get('meeting_start_time'))
  date_source='meeting.start_time'
 if date_dt is None:
  return {'ok':False,'error':'missing_actual_record_time_window','meeting_record_id':str(chosen.get('meeting_record_id')),'view_address':view,'url_kind':url_kind,'note':'no provable Shanghai wall-clock for 日期 line'}
 card=format_manual_review_card(subject, date_dt, view)
 out={
  'ok':True,
  'subject': subject,
  'meeting_id': mid,
  'meeting_record_id': str(chosen.get('meeting_record_id')),
  'record_file_id': str(chosen.get('record_file_id') or '') or None,
  'view_address': view,
  'url_kind': url_kind,
  'date_source': date_source,
  'manual_date': format_shanghai_wall_clock(date_dt),
  'manual_card_text': card,
  'card_text': card,
  'all_record_ids_for_acl': sorted({str(x.get('meeting_record_id')) for x in rows if x.get('meeting_record_id')} | {str(chosen.get('meeting_record_id'))}),
  'record_start_time': start.isoformat() if start else None,
  'record_end_time': end.isoformat() if end else None,
  'duration_seconds': int(dur) if dur is not None else None,
  'duration_text': dur_text,
  'time_line': time_line,
 }
 return out


def recording_share_ready(meeting_id, acl_cache=None):
 """Stable recording share URL/card on records already ready (not 录制中/转码中).
 Five-person ACL grant is skipped when record_acl.enabled is false (t283u).
 A failed grant/verify must not block ready/send (t279u). Optional acl_cache for B window."""
 mid=str(meeting_id)
 try:
  if not is_feishu_assistant_created(mid) and FEISHU_CREATED_REASON not in str((load(BASE/'registry.json').get('meetings') or {}).get(mid,{}).get('reason') or ''):
   return {'ok':False,'error':'not_feishu_assistant_created'}
  if permitted(mid).get('mark')!='company':
   return {'ok':False,'error':'not_company'}
  picked=pick_cloud_recording_for_share(mid)
  if not picked.get('ok'):
   return {'ok':False, **picked}
  # Auto+manual share the same three-line card_text (日期 from record_start_time or meeting.start_time).
  if not picked.get('card_text') or not is_stable_recording_share_url(picked.get('view_address')):
   return {'ok':False,'error': picked.get('auto_card_blocked') or 'missing_actual_record_time_window','picked':{k:picked.get(k) for k in ('view_address','meeting_record_id','manual_card_text','card_text','url_kind','date_source') if k in picked}}
  acl_ids=picked.get('all_record_ids_for_acl') or [picked['meeting_record_id']]
  info=mcp_call('get_meeting',{'meeting_id':mid,'is_compact':True}) or {}
  if isinstance(info,dict) and info.get('error_info'):
   return {'ok':False,'error':'mcp_error','detail':info.get('error_info')}
  states={}
  for r in ((info.get('meeting_info_list') or [{}])[0].get('records') or []):
   states[str(r.get('meeting_record_id'))]=str(r.get('state') or '')
  ready_ids=[]; waiting=[]
  for rid in acl_ids:
   st=states.get(rid,'')
   if any(x in st for x in ('录制中','转码中')):
    waiting.append({'meeting_record_id':rid,'state':st})
   else:
    ready_ids.append(rid)
  if not ready_ids:
   return {'ok':False,'error':'records_not_ready','waiting':waiting,'picked':{k:picked.get(k) for k in ('view_address','meeting_record_id')}}
  if picked['meeting_record_id'] not in ready_ids:
   return {'ok':False,'error':'share_target_not_ready','waiting':waiting,'meeting_record_id':picked['meeting_record_id']}
  # t283u: skip five-person grant/verify when ACL is disabled (no MCP writes).
  # t279u: if still enabled, grant is best-effort and must not block auto-send.
  _, ra = load_record_acl_wanted()
  if not ra.get('enabled'):
   return {'ok':True, **picked, 'acl':{'ok':False,'skipped':'acl_disabled'}, 'waiting':waiting, 'acl_skipped':True}
  acl=ensure_and_verify_recording_acl(mid, ready_ids, acl_cache=acl_cache, accept_view=True)
  out={'ok':True, **picked, 'acl':acl, 'waiting':waiting}
  if not acl.get('ok'):
   out['acl_not_verified']=True
   out['acl_degraded']=True
  return out
 except Exception as ex:
  return {'ok':False,'error':type(ex).__name__+': '+str(ex)[:400]}


def send_ready(meeting_id, acl_cache=None):
 t=material_ready(meeting_id,'transcript')
 r=recording_share_ready(meeting_id, acl_cache=acl_cache)
 return {
  'transcript': t,
  'recording': r,
  'both_ready': bool(t.get('ok') and r.get('ok')),
  'minutes': {'ok': True, 'kind':'minutes','skipped':True,'error':None},
 }


def _text_readback_ok(rb, expected_substr, dest):
 return feishu_delivery.text_readback_ok(rb,expected_substr,dest)


def poller_send_text(meeting_id, target_chat_id, text, *, expected_substr, dry_run=False, kind='recording_link'):
 """Send the three-line card to the allowlisted group; ledger by meeting_id+kind.
 Verify the readback is text or post (t281u) and contains expected_substr."""
 import fcntl
 mid=str(meeting_id)
 dest=resolve_poller_target(target_chat_id)
 if not is_feishu_assistant_created(mid) and FEISHU_CREATED_REASON not in str((load(BASE/'registry.json').get('meetings') or {}).get(mid,{}).get('reason') or ''):
  raise PermissionError('Auto-send refused: meeting lacks feishu-assistant provenance')
 if permitted(mid).get('mark')!='company':
  raise PermissionError('Auto-send refused: not company')
 if not isinstance(text,str) or not text.strip():
  raise ValueError('empty text')
 if expected_substr and expected_substr not in text:
  raise ValueError('text missing expected share url')
 old,key,ledger=load_auto_send_ledger(mid, kind, dest['chat_id'])
 lock_path=AUTO_SEND_LEDGER/(key+'.lock')
 with lock_path.open('a') as lock:
  fcntl.flock(lock, fcntl.LOCK_EX)
  old=load(ledger) if ledger.exists() else None
  if old and (old.get('result') or {}).get('status')=='text-message-readback-verified':
   return dict(old['result'], idempotent_replay=True, auto_send_key=key, dry_run=False)
  if dry_run:
   return {'status':'dry-run-would-send-text','meeting_id':mid,'kind':kind,'chat_id':dest['chat_id'],'text_preview':text[:300],'auto_send_key':key,'dry_run':True}
  if old and (old.get('result') or {}).get('message_id'):
   # try reconcile text
   mid_msg=old['result']['message_id']
   rb=read_feishu_message(mid_msg)
   # Reconcile against the URL actually sent (ledger), not a newly picked crm/crw URL.
   expected=(old.get('result') or {}).get('share_url') or expected_substr
   if _text_readback_ok(rb, expected, dest):
    result=dict(old['result'], status='text-message-readback-verified', readback_ok=True,
                 msg_type=rb.get('msg_type') or old['result'].get('msg_type'),
                 text_preview=(rb.get('text_preview') or old['result'].get('text_preview') or '')[:300],
                 share_url=expected)
    atomic_json(ledger, dict(old, phase='verified', result=result))
    return dict(result, idempotent_replay=True, auto_send_key=key)
   raise RuntimeError('Auto-send prior text candidate unverified; refusing blind resend for '+mid+'/'+kind)
  if old:
   raise RuntimeError('Auto-send text outcome uncertain; no message ID; refusing blind resend')
  identity={'identity':POLLER_IDENTITY,'meeting_id':mid,'kind':kind,'target':dest['cli_target'],'chat_id':dest['chat_id']}
  atomic_json(ledger, dict(identity, phase='reserved', result={}))
  env=os.environ.copy()
  proc=subprocess.run([CLI,'message','send','--channel','feishu','--target',dest['cli_target'],'--message',text,'--json'],capture_output=True,text=True,env=env,timeout=90)
  if proc.returncode!=0:
   raise RuntimeError('feishu text send failed: '+(proc.stderr or proc.stdout)[:500])
  # parse message id
  msg_id=None
  try:
   payload=json.loads(proc.stdout or '{}')
  except Exception:
   payload={}
  def find_id(o):
   if isinstance(o,dict):
    for k,v in o.items():
     if k in ('messageId','message_id','id') and isinstance(v,str) and v.startswith('om_'):
      return v
     found=find_id(v)
     if found: return found
   if isinstance(o,list):
    for x in o:
     found=find_id(x);
     if found: return found
   return None
  msg_id=find_id(payload)
  if not msg_id:
   # sometimes nested under result
   raise RuntimeError('feishu text send missing message_id: '+(proc.stdout or '')[:500])
  atomic_json(ledger, dict(identity, phase='sent-unverified', result={'message_id':msg_id,'share_url':expected_substr,'kind':kind,'chat_id':dest['chat_id']}))
  rb=read_feishu_message(msg_id)
  ok=_text_readback_ok(rb, expected_substr, dest)
  result={'status':'text-message-readback-verified' if ok else 'sent-text-readback-unverified','meeting_id':mid,'kind':kind,'chat_id':dest['chat_id'],'message_id':msg_id,'msg_type':rb.get('msg_type'),'text_preview':(rb.get('text_preview') or '')[:300],'share_url':expected_substr}
  atomic_json(ledger, dict(identity, phase=result['status'], result=result))
  if not ok:
   raise RuntimeError('recording link text readback failed: '+json.dumps(result, ensure_ascii=False)[:400])
  return dict(result, auto_send_key=key, dry_run=False)


def poller_deliver_transcript_and_recording_link(meeting_id, target_chat_id, dry_run=False, readiness=None, acl_cache=None):
 """New auto-send: transcript MD file + recording crw share card. Does not auto-send minutes MD.
 Respects old already_auto_sent_both (transcript+minutes) as terminal — no resend/new format backfill.
 Pass readiness from Judgment B send_ready to avoid a second uncached send_ready (t219u).
 If readiness is omitted, falls back to send_ready(mid, acl_cache=acl_cache) for manual/compat callers.
 """
 mid=str(meeting_id)
 dest=resolve_poller_target(target_chat_id)
 if not is_feishu_assistant_created(mid):
  raise PermissionError('Auto-send scope: feishu-assistant-created only')
 # Legacy completion: both transcript+minutes already verified => do not resend under new format
 legacy={}
 for kind in ('transcript','minutes'):
  old,_,_=load_auto_send_ledger(mid, kind, dest['chat_id'])
  legacy[kind]='verified' if old and (old.get('result') or {}).get('status')=='file-message-readback-verified' else 'pending'
 link_old,_,_=load_auto_send_ledger(mid, 'recording_link', dest['chat_id'])
 link_verified=bool(link_old and (link_old.get('result') or {}).get('status')=='text-message-readback-verified')
 if legacy['transcript']=='verified' and legacy['minutes']=='verified':
  return {'sent':False,'reason':'already_auto_sent_both_legacy','statuses':legacy,'chat_id':dest['chat_id'],'dry_run':dry_run,'note':'historical dual MD send complete; not re-sent under new format'}
 if legacy['transcript']=='verified' and link_verified:
  return {'sent':False,'reason':'already_auto_sent_transcript_and_link','statuses':{'transcript':'verified','recording_link':'verified'},'chat_id':dest['chat_id'],'dry_run':dry_run}

 # t219u: reuse B-window readiness (card_text/view_address/transcript + ACL verified_record_ids already applied).
 if readiness is None:
  readiness=send_ready(mid, acl_cache=acl_cache)
 elif not isinstance(readiness, dict) or 'both_ready' not in readiness:
  raise ValueError('readiness must be send_ready() result dict when provided')
 if not readiness.get('both_ready'):
  return {'sent':False,'reason':'not_send_ready','readiness':readiness,'chat_id':dest['chat_id'],'dry_run':dry_run}

 would=[]; delivered=[]
 # 1) transcript file if needed
 if legacy['transcript']!='verified':
  prepared=prepare({'meeting_id':mid,'kind':'transcript','format':'md'})
  x=prepared['exports'][0]
  would.append({'meeting_id':mid,'kind':'transcript','file_name':x['file_name'],'export_id':x['export_id']})
  out=poller_send_prepared(x['export_id'], dest['chat_id'], dry_run=dry_run)
  delivered.append(out)
  if not dry_run and out.get('status')!='file-message-readback-verified':
   raise RuntimeError('Auto-send transcript failed verification: status='+str(out.get('status')))
 else:
  would.append({'meeting_id':mid,'kind':'transcript','already_verified':True})

 # 2) recording link text if needed
 if not link_verified:
  card=readiness['recording']['card_text']
  url=readiness['recording']['view_address']
  if not is_stable_recording_share_url(url):
   raise RuntimeError('refusing to send non-stable share url')
  would.append({'meeting_id':mid,'kind':'recording_link','view_address':url,'card_text':card})
  out=poller_send_text(mid, dest['chat_id'], card, expected_substr=url, dry_run=dry_run, kind='recording_link')
  delivered.append(out)
  if not dry_run and out.get('status')!='text-message-readback-verified':
   raise RuntimeError('Auto-send recording link failed verification')
 else:
  would.append({'meeting_id':mid,'kind':'recording_link','already_verified':True})

 return {
  'sent': not dry_run,
  'dry_run': dry_run,
  'reason': 'ok',
  'chat_id': dest['chat_id'],
  'would_send': would,
  'delivery': delivered,
  'file_names': [readiness['transcript'].get('file_name')],
  'recording_card': readiness['recording'].get('card_text'),
  'view_address': readiness['recording'].get('view_address'),
  'readiness': {
   'transcript': {k:readiness['transcript'].get(k) for k in ('ok','file_name','error','bytes')},
   'recording': {k:readiness['recording'].get(k) for k in ('ok','view_address','time_line','duration_text','meeting_record_id','error') if k in readiness['recording']},
  },
 }


def poller_deliver_dual(meeting_id, target_chat_id, dry_run=False):
 """Only when BOTH transcript and minutes are ready: prepare+send both. Never send one alone when the other is not ready yet.
 If one kind already verified in ledger and the other is not, send only the missing kind (crash recovery)."""
 mid=str(meeting_id)
 dest=resolve_poller_target(target_chat_id)
 if not is_feishu_assistant_created(mid):
  raise PermissionError('Auto-send scope: feishu-assistant-created only')
 # Per-kind ledger status first (crash recovery may complete a missing kind).
 statuses={}
 for kind in ('transcript','minutes'):
  old,_,_=load_auto_send_ledger(mid, kind, dest['chat_id'])
  statuses[kind]='verified' if old and (old.get('result') or {}).get('status')=='file-message-readback-verified' else 'pending'
 readiness=dual_ready(mid)
 if statuses['transcript']=='verified' and statuses['minutes']=='verified':
  return {'sent':False,'reason':'already_auto_sent_both','statuses':statuses,'chat_id':dest['chat_id'],'file_names':[readiness['transcript'].get('file_name'),readiness['minutes'].get('file_name')],'dry_run':dry_run}
 need=[k for k,s in statuses.items() if s!='verified']
 # Fresh dual-send: both must be ready. Recovery: only the missing kind must be ready.
 if len(need)==2 and not readiness['both_ready']:
  return {'sent':False,'reason':'not_dual_ready','readiness':readiness,'statuses':statuses,'chat_id':dest['chat_id'],'dry_run':dry_run}
 if len(need)==1 and not readiness[need[0]].get('ok'):
  return {'sent':False,'reason':'missing_kind_not_ready','readiness':readiness,'statuses':statuses,'chat_id':dest['chat_id'],'dry_run':dry_run}
 prepared=prepare({'meeting_id':mid,'kind':'both','format':'md'})
 by_kind={x['kind']:x for x in prepared['exports']}
 would=[]; delivered=[]
 for kind in ('transcript','minutes'):
  if statuses[kind]=='verified':
   continue
  x=by_kind[kind]
  would.append({'meeting_id':mid,'kind':kind,'file_name':x['file_name'],'export_id':x['export_id']})
  out=poller_send_prepared(x['export_id'], dest['chat_id'], dry_run=dry_run)
  delivered.append(out)
  if not dry_run and out.get('status')!='file-message-readback-verified':
   raise RuntimeError('Auto-send kind failed verification: '+kind+' status='+str(out.get('status')))
 return {'sent':not dry_run,'dry_run':dry_run,'reason':'ok','chat_id':dest['chat_id'],'would_send':would,'delivery':delivered,'file_names':[readiness['transcript']['file_name'],readiness['minutes']['file_name']],'readiness':readiness}


# t296u independent automated delivery. Manual tools and legacy wrappers unchanged.
def poller_deliver_component(meeting_id, kind, target_chat_id, component, *, dry_run=False, checkpoint=lambda:None):
 """Recover ledger first, cache ready material once, deliver only this missing kind."""
 mid=str(meeting_id)
 if kind not in ('transcript','recording_link'):raise ValueError('Unsupported automated kind')
 if target_chat_id!=AI_GROUP_CHAT_ID:raise PermissionError('Auto target must remain AI group')
 dest=resolve_poller_target(target_chat_id)
 if not is_feishu_assistant_created(mid):raise PermissionError('Missing Feishu provenance')
 verified='file-message-readback-verified' if kind=='transcript' else 'text-message-readback-verified'
 old,key,ledger=load_auto_send_ledger(mid,kind,dest['chat_id'])
 stamp=lambda:datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
 if old:
  result=old.get('result') or {}
  if result.get('status')!=verified:
   if not result.get('message_id'):
    component.update(status='uncertain',last_error='reserved_without_message_id_no_blind_resend');checkpoint();return
   if dry_run:
    component.update(status='uncertain',last_error='dry_run_no_readback');return
   if kind=='transcript':
    result=reconcile_delivery_result(result,dest)
   else:
    expected=result.get('share_url')
    if not expected:raise RuntimeError('Missing original share URL; no blind resend')
    rb=read_feishu_message(result['message_id'])
    if not _text_readback_ok(rb,expected,dest):raise RuntimeError('Prior text readback unverified; no blind resend')
    result=dict(result,status=verified,readback_ok=True,msg_type=rb.get('msg_type'))
   atomic_json(ledger,dict(old,phase='verified',result=result))
  component.update(status='verified',sent_verified_at=component.get('sent_verified_at') or stamp(),delivery=result,last_error=None);checkpoint();return
 # A ready payload survives delivery failure/restart, so no repeated generation/query.
 if not component.get('ready_payload'):
  if kind=='transcript':
   payload=prepare({'meeting_id':mid,'kind':'transcript','format':'md'})['exports'][0]
  else:
   payload=recording_share_ready(mid,acl_cache={})
   if not payload.get('ok'):
    error=payload.get('error') or 'recording_not_ready'
    if 'PollerQuotaError' in str(error) or '500246' in json.dumps(payload):raise PollerQuotaError('quota500246')
    component.update(status='not_ready',last_error=error);checkpoint();return
  component.update(ready_payload=payload,ready_at=component.get('ready_at') or stamp(),status='ready',last_error=None);checkpoint()
 payload=component['ready_payload']
 if kind=='transcript':
  result=poller_send_prepared(payload['export_id'],dest['chat_id'],dry_run=dry_run)
 else:
  url=payload['view_address']
  if not is_stable_recording_share_url(url):raise ValueError('Unstable share URL')
  result=poller_send_text(mid,dest['chat_id'],payload['card_text'],expected_substr=url,dry_run=dry_run)
 if dry_run:
  component['status']='dry_run_ready';checkpoint();return
 if result.get('status')!=verified:raise RuntimeError('Delivery not verified')
 component.update(status='verified',sent_verified_at=stamp(),delivery=result,last_error=None);checkpoint()


# 进度查询与补查使用同一组工具参数，发送继续复用自动投递账本。
for tool_name, description, retry_tool in (
 ('get_meeting_delivery_status', '查询会后逐字稿和录播各自的投递进度，不发送资料。', False),
 ('retry_meeting_delivery', '用户明确要求补查或补发时，检查并投递缺少的逐字稿或录播。目标为配置的自动投递群，私聊调用也投递到该群。复用既有账本，已有发送记录先核对。', True),
):
 properties = {
  'meeting_id': {'type':'string','pattern':'^[0-9]+$'},
  'target_chat_id': {'type':'string','description':'群聊传本轮 chat_id，私聊省略'},
 }
 if retry_tool:
  properties['kind'] = {'type':'string','enum':['transcript','recording_link','missing']}
 TOOLS.append({
  'name':tool_name, 'description':description,
  'inputSchema':{'type':'object','additionalProperties':False,'properties':properties,
                 'required':['meeting_id','kind'] if retry_tool else ['meeting_id']},
 })
