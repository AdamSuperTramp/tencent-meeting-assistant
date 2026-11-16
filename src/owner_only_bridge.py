#!/usr/bin/env python3
"""Containment guard, not team authentication.
Because stdio MCP supplies no trusted per-turn Feishu principal, refuse upstream
access if the fixed allowlist admission configuration changes. Same-UID shell
access is NOT a security boundary; this is channel containment, not per-user data ACL.
"""
import pathlib,json,importlib.util,datetime,os
from settings import CODE_DIR, DATA_DIR, OWNER_OPEN_ID, OPENCLAW_CONFIG, TRUSTED_GROUP_IDS
OWNER=OWNER_OPEN_ID
CONFIG=OPENCLAW_CONFIG
def exclusive_owner(c):
    channels=c.get('channels',{});f=channels.get('feishu',{})
    # DM stays owner-only. A fixed group allowlist is admitted only with @mention;
    # open groups are never admitted. This does not provide per-member data ACL.
    gp=f.get('groupPolicy')
    groups_ok = (gp=='disabled') or (
        gp=='allowlist'
        and f.get('requireMention') is True
        and isinstance(f.get('groupAllowFrom'), list)
        and all(isinstance(x,str) and x.startswith('oc_') for x in f.get('groupAllowFrom',[]))
        and set(f.get('groupAllowFrom',[])) == TRUSTED_GROUP_IDS
        and bool(TRUSTED_GROUP_IDS)
    )
    return (f.get('enabled') is True and f.get('dmPolicy')=='allowlist'
        and f.get('allowFrom')==[OWNER] and groups_ok
        and not any(k!='feishu' and v.get('enabled',False) for k,v in channels.items() if isinstance(v,dict))
        and set(c.get('agents',{}).get('entries',{}))=={'main'})
def guard():
    try:ok=exclusive_owner(json.loads(CONFIG.read_text()))
    except Exception:ok=False
    tools=json.loads(CONFIG.read_text()).get('agents',{}).get('entries',{}).get('main',{}).get('tools',{})
    required={'group:runtime','group:fs','message','sessions_spawn','sessions_send','gateway','nodes','browser','cron'}
    ok=ok and set(tools.get('allow',[])) in ({'bundle-mcp'},{'bundle-mcp','feishu_app_scopes'}) and required.issubset(set(tools.get('deny',[])))
    if not ok:raise PermissionError('Owner-only admission boundary changed; team MCP identity adapter is not implemented')
s=importlib.util.spec_from_file_location('owner_bridge',str(CODE_DIR/'tencent_meeting_mcp_bridge.py'))
b=importlib.util.module_from_spec(s);s.loader.exec_module(b);original=b.rpc
def guarded_rpc(body):
    try:
        guard()
    except Exception:
        audit(body,'deny-boundary');raise
    try:
        result=original(body);audit(body,'upstream-returned');return result
    except Exception:
        audit(body,'upstream-error');raise
def audit(body,outcome):
    # Never include arguments, IDs, returned titles/text, credentials or RPC error strings.
    event={'time':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
           'method':body.get('method'),'tool':(body.get('params') or {}).get('name'),
           'outcome':outcome,'identity':'owner-only-admission-unattributed-stdio','policy_version':1}
    path=DATA_DIR/'bridge-audit.jsonl'
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    with os.fdopen(fd,'a') as f:f.write(json.dumps(event)+'\n')
e_spec=importlib.util.spec_from_file_location('business_export',CODE_DIR/'business_export.py')
e=importlib.util.module_from_spec(e_spec);e_spec.loader.exec_module(e)
def _meeting_has_ended_or_recordings(meeting_id):
    """Refuse cancel side-effects on already-held meetings. Export must never cancel."""
    try:
        info=e.mcp_call('get_meeting',{'meeting_id':str(meeting_id),'is_compact':True})
    except Exception:
        # Fail closed: if we cannot verify, do not cancel.
        return True,'lookup-failed'
    rows=(info or {}).get('meeting_info_list') or []
    if not rows:return True,'missing-meeting'
    m=rows[0] if isinstance(rows[0],dict) else {}
    status=str(m.get('status') or '')
    if status in ('已取消','已结束','取消','结束','closed','cancelled','canceled','ended') or '取消' in status or '结束' in status:
        return True,'status:'+status
    if m.get('records') or m.get('records_total_count') or m.get('minute_total_count'):
        return True,'has-recordings-or-minutes'
    # Scheduled end already in the past (Asia/Shanghai wall clock from API ISO strings).
    end=m.get('end_time') or m.get('schedule_end_time')
    if isinstance(end,str) and end:
        try:
            dt=datetime.datetime.fromisoformat(end)
            now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            if dt.tzinfo is None:dt=dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            if dt <= now:return True,'end-time-passed'
        except Exception:
            pass
    return False,''


def _tool_call_payload(rpc_response):
    """Parse successful tools/call JSON-RPC (possibly skill HTTP envelope) into a dict body."""
    if not isinstance(rpc_response, dict) or rpc_response.get('error'):
        return None
    result=rpc_response.get('result', rpc_response)
    if isinstance(result, dict) and result.get('isError'):
        return None
    content=result.get('content') if isinstance(result, dict) else None
    if not (isinstance(content, list) and content):
        return result if isinstance(result, dict) and not any(result.get(k) for k in ('error', 'error_info', 'failure')) else None
    text=(content[0] or {}).get('text','') if isinstance(content[0], dict) else ''
    try:
        env=json.loads(text)
    except Exception:
        return None
    if isinstance(env, dict) and (env.get('error') or env.get('error_info') or env.get('failure')):
        return None
    if isinstance(env, dict) and 'body' in env and 'status_code' in env:
        body=env.get('body')
        if isinstance(body, str):
            try:body=json.loads(body)
            except Exception:pass
        if env.get('status_code') not in (None, 200):
            return None
        if isinstance(body, dict) and not any(body.get(k) for k in ('error', 'error_info', 'failure')):
            return body
        return None
    return env if isinstance(env, dict) else None

def _feishu_request_meta(params):
    meta=(params.get('_meta') or {})
    # OpenClaw may nest under openclaw/feishu-request
    ctx=meta.get('openclaw/feishu-request') or meta.get('feishu-request') or {}
    if not isinstance(ctx, dict):ctx={}
    return ctx

def _chat_id_from_feishu_ctx(ctx):
    if not isinstance(ctx, dict):return None
    for key in ('chat_id','chatId','group_id'):
        raw=ctx.get(key)
        if isinstance(raw, str) and raw.strip():
            s=raw.strip()
            for prefix in ('feishu:','lark:','chat:','group:','channel:'):
                if s.lower().startswith(prefix):s=s[len(prefix):].strip()
            if s.startswith('oc_'):return s
    sk=ctx.get('sessionKey') or ctx.get('session_key') or ''
    if isinstance(sk, str) and ':feishu:group:oc_' in sk:
        return sk.split(':feishu:group:',1)[1].split(':',1)[0]
    return None

def _record_schedule_meeting_company(params, rpc_response):
    """On successful schedule_meeting via this Feishu assistant: durable company provenance."""
    args=params.get('arguments') or {}
    payload=_tool_call_payload(rpc_response)
    if not payload:return False
    rows=payload.get('meeting_info_list') or []
    row=rows[0] if rows and isinstance(rows[0], dict) else {}
    mid=str(row.get('meeting_id') or args.get('meeting_id') or '')
    if not mid:return False
    subject=(row.get('subject') or args.get('subject') or '').strip() or None
    ctx=_feishu_request_meta(params)
    chat_id=_chat_id_from_feishu_ctx(ctx)
    session_key=ctx.get('sessionKey') or ctx.get('session_key')
    e.record_feishu_assistant_created(
        mid,
        subject=subject,
        created_from_chat_id=chat_id,
        session_key=session_key,
        extra={
            'meeting_code': row.get('meeting_code') or args.get('meeting_code'),
            'join_url': row.get('join_url') or args.get('join_url'),
            'start_time': (row.get('start_time') or args.get('start_time') or None),
            'end_time': (row.get('end_time') or args.get('end_time') or None),
            'time_zone': (row.get('time_zone') or args.get('time_zone') or None),
        },
    )
    return True


def _sync_update_meeting_schedule(params, rpc_response):
    """Keep local start_time in sync after update_meeting (needed for start+20m gate)."""
    args = params.get('arguments') or {}
    payload = _tool_call_payload(rpc_response)
    if payload is None:
        return False
    row = {}
    if isinstance(payload, dict):
        rows = payload.get('meeting_info_list') or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
    mid = str(row.get('meeting_id') or args.get('meeting_id') or '')
    if not mid:
        return False
    start = row.get('start_time') if row.get('start_time') is not None else args.get('start_time')
    end = row.get('end_time') if row.get('end_time') is not None else args.get('end_time')
    subject = (row.get('subject') or args.get('subject') or None)
    if start is None and end is None and subject is None and args.get('time_zone') is None:
        return False
    out = e.sync_local_meeting_schedule(
        mid,
        start_time=start,
        end_time=end,
        time_zone=row.get('time_zone') or args.get('time_zone'),
        subject=subject,
        meeting_code=row.get('meeting_code'),
        join_url=row.get('join_url'),
        source='update_meeting',
    )
    return bool(out.get('ok'))


def _mark_cancelled_local(params, rpc_response):
    args = params.get('arguments') or {}
    payload = _tool_call_payload(rpc_response) if rpc_response is not None else {}
    mid = str(args.get('meeting_id') or '')
    if not mid and isinstance(payload, dict):
        rows = payload.get('meeting_info_list') or []
        if rows and isinstance(rows[0], dict):
            mid = str(rows[0].get('meeting_id') or '')
    if not mid:
        return False
    out = e.sync_local_meeting_schedule(mid, cancelled=True, cancel_reason='cancel_meeting', source='cancel_meeting')
    return bool(out.get('ok'))


def routed_rpc(body):
    guard()
    method=body.get('method');params=body.get('params') or {};name=params.get('name')
    if method == 'tools/call':
        ctx = _feishu_request_meta(params)
        session = ctx.get('sessionKey') or ctx.get('session_key') or ''
        if ':feishu:group:' in session:
            group = _chat_id_from_feishu_ctx(ctx)
            if group not in TRUSTED_GROUP_IDS:
                raise PermissionError('群聊未获授权')
            import group_access
            from types import SimpleNamespace
            service = SimpleNamespace(**e.__dict__, _tool_call_payload=_tool_call_payload)
            routed = group_access.route(service, name, params.get('arguments') or {}, lambda: guarded_rpc(body))
            if routed is not None:
                return routed
        elif session not in ('agent:main:main', 'agent:main:feishu:dm:' + OWNER):
            raise PermissionError('缺少可信的飞书请求会话')
    if method=='tools/list':
        result=guarded_rpc(body)
        result.setdefault('result',{}).setdefault('tools',[]).extend(e.TOOLS)
        # Annotate cancel_meeting description so the model avoids using it during export recovery.
        for tool in result.get('result',{}).get('tools',[]):
            if tool.get('name')=='cancel_meeting':
                desc=tool.get('description') or ''
                note=' CRITICAL SAFETY: Never call cancel_meeting while exporting transcripts/minutes or recovering from export errors. Refuse cancel for ended/cancelled meetings or any meeting that already has recordings/minutes/transcripts.'
                if 'CRITICAL SAFETY' not in desc:
                    tool['description']=desc+note
        return result
    if method=='tools/call' and name in [t['name'] for t in e.TOOLS]:
        try:
            data=e.handle(name,params.get('arguments') or {},(params.get('_meta') or {}).get('openclaw/feishu-request'));audit(body,'export-local-success')
            return {'result':{'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False)}]}}
        except Exception as ex:
            audit(body,'export-local-denied-or-error')
            return {'result':{'isError':True,'content':[{'type':'text','text':str(ex)}]}}
    if method=='tools/call' and name=='cancel_meeting':
        args=params.get('arguments') or {}
        mid=str(args.get('meeting_id') or '')
        blocked,why=_meeting_has_ended_or_recordings(mid) if mid else (True,'missing-meeting-id')
        if blocked:
            audit(body,'cancel-blocked:'+why)
            return {'result':{'isError':True,'content':[{'type':'text','text':'cancel_meeting blocked: meeting already ended/cancelled or has recordings/minutes ('+why+'). Export/transcript recovery must never cancel meetings.'}]}}
        result=guarded_rpc(body)
        try:
            if _tool_call_payload(result) is not None:
                ok=_mark_cancelled_local(params, result)
                audit(body, 'local-cancelled' if ok else 'local-cancel-mark-skip')
        except Exception:
            audit(body, 'local-cancel-mark-error')
        return result
    if method=='tools/call' and name=='schedule_meeting':
        result=guarded_rpc(body)
        try:
            ok=_record_schedule_meeting_company(params, result)
            audit(body, 'feishu-created-company-recorded' if ok else 'feishu-created-company-skip')
        except Exception:
            # Create already succeeded upstream; never fail the user-visible create on registry write.
            audit(body, 'feishu-created-company-error')
        return result
    if method=='tools/call' and name=='update_meeting':
        result=guarded_rpc(body)
        try:
            ok=_sync_update_meeting_schedule(params, result)
            audit(body, 'local-schedule-synced' if ok else 'local-schedule-sync-skip')
        except Exception:
            audit(body, 'local-schedule-sync-error')
        return result
    return guarded_rpc(body)
b.rpc=routed_rpc
if __name__=='__main__':b.main()
