#!/usr/bin/env python3
"""Prospective company-meeting recording ACL granter (Mini poller helper).

Authorized by t178u (2026-11-09 21:16 Asia/Shanghai). Grants per meeting_record_id
member ACL (permission=1 可管理) + optional download_enable without widening
share audience (does not change permission_point_id / switch_state to public/corp-wide).

Never touches private/pending. Never backfills meetings ended before cutoff.
Independent of Feishu MD delivery ledger / already_auto_sent.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from settings import DATA_DIR
BASE = DATA_DIR
DEFAULT_MEMBERS_PATH = BASE / 'record-acl-members.json'
DEFAULT_CUTOFF = '2026-11-09T21:16:00+08:00'
TZ = datetime.timezone(datetime.timedelta(hours=8))

McpFn = Callable[[str, dict], Any]


def now_sh() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def parse_dt(value) -> Optional[datetime.datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, TZ)
    s = str(value).strip()
    try:
        dt = datetime.datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def load_members_config(path: Optional[pathlib.Path] = None) -> dict:
    p = pathlib.Path(path or DEFAULT_MEMBERS_PATH)
    if not p.exists():
        return {'members': [], 'pending': [], 'error': 'members_config_missing'}
    data = json.loads(p.read_text())
    data.setdefault('members', [])
    data.setdefault('pending', [])
    return data


def active_member_targets(members_doc: dict) -> List[dict]:
    out = []
    seen = set()
    for m in members_doc.get('members') or []:
        if m.get('status') != 'active':
            continue
        oid = str(m.get('open_id') or '').strip()
        if not oid or oid in seen:
            raise ValueError('Active ACL targets require unique nonempty open_ids')
        seen.add(oid)
        out.append({
            'display_name': m.get('display_name') or m.get('username_query'),
            'open_id': oid,
            'permission': int(m.get('permission') if m.get('permission') is not None else 1),
        })
    return out


def acl_cfg_from_export(cfg: dict) -> dict:
    ra = dict(cfg.get('record_acl') or {})
    ra.setdefault('enabled', False)
    ra.setdefault('cutoff', DEFAULT_CUTOFF)
    ra.setdefault('permission', 1)
    ra.setdefault('enable_download', True)
    ra.setdefault('ensure_view_minutes', True)
    ra.setdefault('preserve_sharing_mode', True)
    ra.setdefault('members_config', str(DEFAULT_MEMBERS_PATH))
    ra.setdefault('lookback_hours', max(float(cfg.get('lookback_hours') or 12), 72))
    ra.setdefault('deny_meeting_ids', [])
    return ra


def is_company_for_acl(mid: str, feishu_prov_entry: Optional[dict], registry_entry: Optional[dict]) -> Tuple[bool, str]:
    """Feishu-assistant provenance wins (standing company rule). Else registry company.
    Pure private/pending without feishu provenance are excluded.
    """
    mid = str(mid)
    fp = feishu_prov_entry or {}
    reg = registry_entry or {}
    mark = reg.get('mark')
    if fp.get('probe') is True:
        return False, 'probe_entry'
    feishu_company = bool(
        fp.get('mark') == 'company'
        or (fp and (fp.get('reason') == 'created_via_feishu_assistant'
                    or 'created_via_feishu_assistant' in str(fp.get('reason') or '')))
    )
    if feishu_company:
        return True, 'feishu_assistant_created'
    if mark == 'private':
        return False, 'registry_private'
    if mark == 'pending':
        return False, 'registry_pending'
    if mark == 'company':
        return True, 'registry_company'
    if fp:
        return False, 'feishu_prov_not_company'
    return False, 'not_company'


def after_cutoff(ended_meta: Optional[dict], cutoff_iso: str) -> Tuple[bool, str]:
    cutoff = parse_dt(cutoff_iso)
    if not cutoff:
        return False, 'bad_cutoff'
    end = parse_dt((ended_meta or {}).get('end_time'))
    if end is None:
        # No end_time: do not grant on historical unknowns; require end_time for prospective gate.
        return False, 'missing_end_time'
    if end >= cutoff:
        return True, 'end_on_or_after_cutoff'
    return False, 'ended_before_cutoff'


def list_meeting_record_ids(mcp: McpFn, mid: str) -> List[dict]:
    """Return unique meeting_record_id rows (video/transcript/etc)."""
    found: Dict[str, dict] = {}
    # Prefer get_meeting.records (has type/subject)
    try:
        info = mcp('get_meeting', {'meeting_id': str(mid), 'is_compact': True}) or {}
        for m in (info.get('meeting_info_list') or []):
            for r in (m.get('records') or []):
                mrid = str(r.get('meeting_record_id') or '')
                if mrid and mrid != '0':
                    found[mrid] = {
                        'meeting_record_id': mrid,
                        'record_file_id': str(r.get('record_file_id') or '') or None,
                        'subject': r.get('subject'),
                        'type': r.get('type'),
                        'state': r.get('state'),
                        'source': 'get_meeting',
                    }
    except Exception:
        pass
    try:
        out = mcp('search_records', {'meeting_id': str(mid), 'page_size': 50, 'is_compact': True}) or {}
        for r in (out.get('records') or []):
            mrid = str(r.get('meeting_record_id') or '')
            if not mrid or mrid == '0':
                continue
            prev = found.get(mrid) or {'meeting_record_id': mrid, 'source': 'search_records'}
            prev.setdefault('record_file_id', str(r.get('record_file_id') or '') or None)
            prev.setdefault('subject', r.get('subject') or r.get('file_name'))
            prev.setdefault('type', r.get('file_type'))
            prev['source'] = (prev.get('source') or '') + '+search_records' if prev.get('source') and 'search' not in prev.get('source') else prev.get('source') or 'search_records'
            found[mrid] = prev
    except Exception:
        pass
    return list(found.values())


def _norm_perm(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    if '管理' in s:
        return 1
    if '查看' in s:
        return 0
    try:
        return int(float(s))
    except Exception:
        return None


def _payload_dicts(value, depth=0):
    """Walk JSON/envelopes, including MCP content, response.body and nested results."""
    if depth > 20:
        return
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(decoded, (dict, list)):
            yield from _payload_dicts(decoded, depth + 1)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _payload_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _payload_dicts(child, depth + 1)


def response_errors(value):
    errors = []
    for node in _payload_dicts(value):
        for key in ('error_info', 'error'):
            if node.get(key):
                errors.append(key)
        if node.get('isError') is True or node.get('success') is False:
            errors.append('explicit_failure')
        if 'code' in node and node['code'] not in (None, 0, '0'):
            errors.append('business_code:' + str(node['code']))
        status = node.get('status_code')
        if status is not None and str(status).isdigit() and not 200 <= int(status) < 300:
            errors.append('http_status:' + str(status))
    return sorted(set(errors))


def _audit_safe(value):
    # Audit business payloads only; never persist auth headers/token/password values.
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if re.search(r'token|authorization|cookie|secret|password|^pwd$|api.?key', str(k), re.I)
                         and str(k) not in ('page_token', 'next_page_token') else _audit_safe(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_audit_safe(v) for v in value]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            cleaned = re.sub(r'(?i)((?:bearer|basic)\s+)[^\s"\',}]+', r'\1[REDACTED]', value)
            return re.sub(r'(?i)((?:[\w-]*(?:token|secret|authorization|cookie|password|api[_-]?key)[\w-]*|pwd)\s*[:=]\s*)[^\s,;}]+',
                          r'\1[REDACTED]', cleaned)
        if isinstance(decoded, (dict, list)):
            return json.dumps(_audit_safe(decoded), ensure_ascii=False)
    return value


def audited_mcp(mcp, name, args):
    """Persist each request/response privately; no credentials are requested/read here."""
    import uuid
    directory = BASE / 'private-record-acl-audit'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('ACL audit directory must not be a symlink')
    os.chmod(directory, 0o700)
    path = directory / (now_sh().strftime('%Y%m%d%H%M%S%f') + '-' + uuid.uuid4().hex + '.json')
    payload = {'at': now_sh().isoformat(), 'tool': name, 'arguments': _audit_safe(args)}
    def save():
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
    # Persist request BEFORE an external write, so audit failure cannot permit an unaudited call.
    save()
    try:
        result = mcp(name, args)
    except Exception as exc:
        payload['exception_type'] = type(exc).__name__
        with path.open('w') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        raise
    payload['response'] = _audit_safe(result)
    with path.open('w') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return result


def member_payload(value):
    metadata = {}
    for node in _payload_dicts(value):
        for key in ('has_more', 'next_page_token', 'total_count'):
            if key in node:
                metadata[key] = node[key]
        if any(isinstance(node.get(key), list) for key in ('members', 'access_members', 'users', 'authorized_members')):
            return dict(metadata, **node)
    return {}


def member_rows(value):
    node = member_payload(value)
    # Prefer real rows over an empty compatibility alias.
    lists = [node[k] for k in ('members', 'access_members', 'users', 'authorized_members') if isinstance(node.get(k), list)]
    return next((rows for rows in lists if rows), lists[0] if lists else [])


def members_evidence_complete(value):
    if not isinstance(value, dict) or response_errors(value):
        return False
    if value.get('_acl_complete') is False:
        return False
    node = member_payload(value)
    if not node or node.get('has_more') in (True, 1, 'true', 'True'):
        return False
    total = node.get('total_count')
    rows = member_rows(value)
    try:
        if total is not None and int(total) > len(rows):
            return False
    except (ValueError, TypeError):
        return False
    return all(isinstance(row, dict) and bool(row.get('open_id')) and
               _norm_perm(row.get('permission') if 'permission' in row else row.get('permission_type')) is not None
               for row in rows)


def sharing_current(value):
    if response_errors(value):
        return {}
    for node in _payload_dicts(value):
        if isinstance(node.get('current'), dict):
            return node['current']
        if 'download_enable' in node or 'view_minutes_enable' in node:
            return node
    return {}


def inspect_share_result(value, requested, wanted):
    """HTTP/top-level success is not member success; inspect every nested child/body."""
    errors = response_errors(value)
    failed_ids = set()
    failed_rows = 0
    recognized = False
    has_results = False
    observed = set()
    required = []
    if requested.get('add_members'):
        required.append('add_members')
    if requested.get('sharing_config'):
        required.append('modify_config')
    for node in _payload_dicts(value):
        if 'results' in node:
            has_results = True
        if node.get('ok') is True or node.get('success') is True or node.get('code') in (0, '0'):
            recognized = True
        for key in required:
            if key in node and isinstance(node[key], dict):
                child = node[key]
                observed.add(key)
                if child.get('executed') is False or child.get('success') is False:
                    errors.append(key + ':not_successful')
        if 'fail_access_members' in node:
            failed = node['fail_access_members']
            if not isinstance(failed, list):
                errors.append('malformed_fail_access_members')
            elif failed:
                errors.append('fail_access_members')
                failed_rows = max(failed_rows, len(failed))
                for row in failed:
                    if isinstance(row, dict):
                        oid = row.get('to_operator_id') or row.get('open_id')
                        if oid:
                            failed_ids.add(str(oid))
    if has_results:
        errors.extend(key + ':missing_result' for key in required if key not in observed)
    if not recognized:
        errors.append('unrecognized_write_response')
    failed_names = [w.get('display_name') or str(w['open_id'])[:8] for w in wanted if str(w['open_id']) in failed_ids]
    return {'ok': not errors, 'errors': sorted(set(errors)), 'failed_names': failed_names,
            'failed_count': max(len(failed_ids), failed_rows),
            'unmatched_failed_count': max(len(failed_ids) - len(failed_names), 0)}


def members_index(members_body: dict) -> Dict[str, dict]:
    idx = {}
    for row in member_rows(members_body):
        if not isinstance(row, dict):
            continue
        # Never reinterpret a userid/ms_open_id as an open_id.
        oid = str(row.get('open_id') or '').strip()
        if not oid:
            continue
        idx[oid] = {'open_id': oid,
                    'permission': _norm_perm(row.get('permission') if 'permission' in row else row.get('permission_type')),
                    'raw': {k: row.get(k) for k in row if k != 'open_id'}}
    return idx


def sharing_needs_download(sharing_body: dict, enable_download: bool, ensure_view_minutes: bool) -> dict:
    cur = sharing_current(sharing_body)
    patch = {}
    if enable_download:
        de = cur.get('download_enable')
        if de is False or de == 0 or str(de).lower() in ('false', '0'):
            patch['download_enable'] = True
        elif de is None and 'download_enable' in str(cur):
            # unknown shape — still request enable
            patch['download_enable'] = True
        elif de is None:
            patch['download_enable'] = True
    if ensure_view_minutes:
        vm = cur.get('view_minutes_enable')
        if vm is False or vm == 0 or str(vm).lower() in ('false', '0'):
            patch['view_minutes_enable'] = True
        elif vm is None:
            # only set if we know field exists or want ensure
            patch['view_minutes_enable'] = True
    return patch


def compute_member_diff(wanted: List[dict], existing: Dict[str, dict], default_permission: int = 1) -> Tuple[List[dict], List[dict]]:
    """Return (to_add, already_ok). Upgrade view->manage by re-add with permission=1 (add_members)."""
    to_add = []
    ok = []
    for w in wanted:
        oid = w['open_id']
        want_p = int(w.get('permission') if w.get('permission') is not None else default_permission)
        ex = existing.get(oid)
        if not ex:
            to_add.append({'open_id': oid, 'permission': want_p, 'display_name': w.get('display_name')})
            continue
        have_p = ex.get('permission')
        if have_p is not None and have_p >= want_p:
            ok.append({'open_id': oid, 'permission': have_p, 'display_name': w.get('display_name')})
        else:
            # need upgrade to manage
            to_add.append({'open_id': oid, 'permission': want_p, 'display_name': w.get('display_name'), 'upgrade_from': have_p})
    return to_add, ok


def verify_members(existing: Dict[str, dict], wanted: List[dict], default_permission: int = 1) -> Tuple[bool, List[str]]:
    missing = []
    for w in wanted:
        oid = w['open_id']
        want_p = int(w.get('permission') if w.get('permission') is not None else default_permission)
        ex = existing.get(oid)
        if not ex:
            missing.append(w.get('display_name') or oid[:8])
            continue
        have_p = ex.get('permission')
        if have_p is None or have_p < want_p:
            missing.append((w.get('display_name') or oid[:8]) + ':perm<' + str(want_p))
    return (bool(wanted) and len(missing) == 0), missing


def fetch_all_members(mcp: McpFn, meeting_record_id: str) -> dict:
    """Bounded pagination; never turn business errors/incomplete pages into an empty ACL."""
    all_rows, seen, errors = [], set(), []
    token = None
    last = {}
    complete = False
    for _ in range(20):
        args = {'action': 'members', 'meeting_record_id': str(meeting_record_id)}
        if token:
            args['page_token'] = token
        raw = mcp('record_access', args)
        errors.extend(response_errors(raw))
        last = member_payload(raw)
        if errors or not last:
            errors.append('members_response_unusable')
            break
        all_rows.extend(member_rows(last))
        if last.get('has_more') not in (True, 1, 'true', 'True'):
            complete = True
            break
        token = last.get('next_page_token')
        if not token or token in seen:
            errors.append('members_page_token_missing_or_repeated')
            break
        seen.add(token)
    merged = dict(last)
    merged.update(members=all_rows, access_members=all_rows, has_more=not complete,
                  _acl_complete=complete and not errors, _acl_errors=sorted(set(errors)))
    if not members_evidence_complete(merged):
        merged['_acl_complete'] = False
    return merged



def ensure_one_record(
    mcp: McpFn,
    meeting_record_id: str,
    wanted: List[dict],
    *,
    enable_download: bool,
    ensure_view_minutes: bool,
    preserve_sharing_mode: bool,
    default_permission: int,
    dry_run: bool,
) -> dict:
    raw_mcp = mcp
    mcp = lambda name, args: audited_mcp(raw_mcp, name, args)
    mrid = str(meeting_record_id)
    members_body = fetch_all_members(mcp, mrid)
    if not members_evidence_complete(members_body):
        return {'meeting_record_id': mrid, 'action': 'readback_incomplete', 'verified': False,
                'missing': [w.get('display_name') for w in wanted], 'members_complete': False}
    sharing_body = mcp('record_access', {'action': 'sharing', 'meeting_record_id': mrid}) or {}
    if not members_evidence_complete(members_body) or response_errors(sharing_body) or not sharing_current(sharing_body):
        return {'meeting_record_id': mrid, 'action': 'readback_incomplete', 'verified': False,
                'missing': [w.get('display_name') for w in wanted], 'members_complete': False}
    existing = members_index(members_body)
    to_add, already_ok = compute_member_diff(wanted, existing, default_permission)
    share_patch = sharing_needs_download(sharing_body, enable_download, ensure_view_minutes) if preserve_sharing_mode else {}
    # Safety: never include permission_point_id / switch_state in patch
    for banned in ('permission_point_id', 'switch_state', 'pwd'):
        share_patch.pop(banned, None)

    planned = {
        'meeting_record_id': mrid,
        'existing_count': len(existing),
        'already_ok_count': len(already_ok),
        'to_add_count': len(to_add),
        'to_add_names': [x.get('display_name') for x in to_add],
        'share_patch': share_patch,
        'dry_run': dry_run,
    }

    if dry_run:
        planned['action'] = 'dry_run'
        planned['would_call_record_share_set'] = bool(to_add or share_patch)
        return planned

    if not to_add and not share_patch:
        ok, missing = verify_members(existing, wanted, default_permission)
        planned['action'] = 'noop_verified' if ok else 'noop_incomplete'
        planned['verified'] = ok
        planned['missing'] = missing
        return planned

    args = {'meeting_record_id': mrid}
    if to_add:
        args['add_members'] = [{'open_id': x['open_id'], 'permission': int(x['permission'])} for x in to_add]
    if share_patch:
        args['sharing_config'] = share_patch
    # Do NOT pass remove_members — preserve original members
    write_result = mcp('record_share_set', args)
    write_status = inspect_share_result(write_result, args, wanted)
    planned['write_status'] = write_status
    if not write_status['ok']:
        planned.update(action='write_failed', verified=False, members_verified=False,
                       missing=sorted(set(verify_members(existing, wanted, default_permission)[1] + write_status['failed_names'])),
                       failed_names=write_status['failed_names'])
        return planned
    # Re-read verify
    members2 = fetch_all_members(mcp, mrid)
    sharing2 = mcp('record_access', {'action': 'sharing', 'meeting_record_id': mrid}) or {}
    existing2 = members_index(members2)
    ok, missing = verify_members(existing2, wanted, default_permission)
    cur2 = sharing_current(sharing2)
    ok = bool(ok and wanted and members_evidence_complete(members2) and not response_errors(sharing2))
    download_ok = True
    if enable_download:
        de = cur2.get('download_enable')
        download_ok = de is True or de == 1 or str(de).lower() == 'true'
    minutes_ok = True
    if ensure_view_minutes:
        vm = cur2.get('view_minutes_enable')
        minutes_ok = vm is True or vm == 1 or str(vm).lower() == 'true'
    planned.update({
        'action': 'wrote',
        'write_result_keys': sorted(list(write_result.keys())) if isinstance(write_result, dict) else type(write_result).__name__,
        'verified': bool(ok and download_ok and minutes_ok),
        'members_verified': ok,
        'missing': missing,
        'download_ok': download_ok,
        'minutes_ok': minutes_ok,
        'sharing_after': {
            'download_enable': cur2.get('download_enable'),
            'view_minutes_enable': cur2.get('view_minutes_enable'),
            'permission_point_id': cur2.get('permission_point_id'),
            'switch_state': cur2.get('switch_state'),
        },
        'existing_count_after': len(existing2),
    })
    return planned


def ensure_meeting_acl(
    mcp: McpFn,
    mid: str,
    *,
    ended_meta: Optional[dict],
    feishu_prov_entry: Optional[dict],
    registry_entry: Optional[dict],
    cfg: dict,
    dry_run: bool = False,
) -> dict:
    ra = acl_cfg_from_export(cfg)
    mid = str(mid)
    out = {
        'meeting_id': mid,
        'subject': (ended_meta or {}).get('subject') or (feishu_prov_entry or {}).get('subject') or (registry_entry or {}).get('subject'),
        'dry_run': dry_run,
    }
    if not ra.get('enabled'):
        out['action'] = 'disabled'
        return out
    if mid in set(str(x) for x in (ra.get('deny_meeting_ids') or [])):
        out['action'] = 'denied_list'
        return out
    ok_co, why_co = is_company_for_acl(mid, feishu_prov_entry, registry_entry)
    if not ok_co:
        out['action'] = 'skip_not_company'
        out['why'] = why_co
        return out
    ok_cut, why_cut = after_cutoff(ended_meta, ra.get('cutoff') or DEFAULT_CUTOFF)
    if not ok_cut:
        out['action'] = 'skip_before_cutoff'
        out['why'] = why_cut
        return out

    members_doc = load_members_config(ra.get('members_config'))
    wanted = active_member_targets(members_doc)
    # Apply global permission override from export cfg
    perm = int(ra.get('permission') if ra.get('permission') is not None else 1)
    for w in wanted:
        w['permission'] = perm
    out['pending_members'] = [
        {k: p.get(k) for k in ('display_name', 'username_query', 'reason', 'hit_count')}
        for p in (members_doc.get('pending') or [])
    ]
    out['wanted_count'] = len(wanted)
    out['wanted_names'] = [w.get('display_name') for w in wanted]
    if not wanted:
        out['action'] = 'no_active_members'
        return out

    records = list_meeting_record_ids(mcp, mid)
    out['records_found'] = len(records)
    if not records:
        out['action'] = 'waiting_records'
        out['verified'] = False
        return out

    results = []
    all_verified = True
    any_write = False
    for rec in records:
        state_txt = str(rec.get('state') or '')
        if any(x in state_txt for x in ('录制中', '转码中')):
            results.append({
                'meeting_record_id': rec.get('meeting_record_id'),
                'type': rec.get('type'),
                'subject': rec.get('subject'),
                'action': 'waiting_record_state',
                'state': state_txt,
                'verified': False,
                'to_add_count': None,
                'dry_run': dry_run,
            })
            all_verified = False
            continue
        one = ensure_one_record(
            mcp,
            rec['meeting_record_id'],
            wanted,
            enable_download=bool(ra.get('enable_download', True)),
            ensure_view_minutes=bool(ra.get('ensure_view_minutes', True)),
            preserve_sharing_mode=bool(ra.get('preserve_sharing_mode', True)),
            default_permission=perm,
            dry_run=dry_run,
        )
        one['type'] = rec.get('type')
        one['subject'] = rec.get('subject')
        results.append(one)
        if one.get('action') == 'write_failed':
            any_write = True
            all_verified = False
            break
        if one.get('action') == 'wrote':
            any_write = True
        if dry_run:
            if one.get('to_add_count') or one.get('share_patch'):
                all_verified = False  # would change
            # dry-run: verified means already satisfied
            elif one.get('action') == 'noop_verified' or (one.get('to_add_count') == 0 and not one.get('share_patch')):
                pass
            else:
                all_verified = False
        else:
            if not one.get('verified'):
                all_verified = False

    out['records'] = [
        {k: r.get(k) for k in (
            'meeting_record_id', 'type', 'subject', 'action', 'to_add_count', 'to_add_names',
            'share_patch', 'verified', 'missing', 'download_ok', 'minutes_ok', 'existing_count',
            'write_status', 'failed_names', 'members_complete',
            'existing_count_after', 'sharing_after', 'dry_run', 'would_call_record_share_set'
        ) if k in r}
        for r in results
    ]
    if dry_run:
        out['action'] = 'dry_run'
        out['would_modify'] = any(r.get('would_call_record_share_set') for r in results)
        out['verified'] = all(r.get('to_add_count') == 0 and not r.get('share_patch') for r in results)
    elif all_verified:
        out['action'] = 'verified'
        out['verified'] = True
    elif any_write:
        out['action'] = 'partial_or_unverified'
        out['verified'] = False
    else:
        out['action'] = 'incomplete'
        out['verified'] = False
    return out


def merge_acl_state(cand: dict, result: dict) -> None:
    """Update poller candidate record_acl section; never clears delivery fields."""
    prev = dict(cand.get('record_acl') or {})
    prev['updated_at'] = now_sh().isoformat()
    prev['last_action'] = result.get('action')
    prev['verified'] = bool(result.get('verified'))
    prev['why'] = result.get('why')
    prev['records_found'] = result.get('records_found')
    prev['wanted_names'] = result.get('wanted_names')
    prev['pending_members'] = result.get('pending_members')
    # Per-record detail without open_ids
    prev['records'] = result.get('records')
    if result.get('action') in ('skip_before_cutoff', 'skip_not_company', 'denied_list', 'disabled'):
        prev['status'] = result['action']
        prev['next_acl_check_at'] = None
    elif result.get('verified'):
        prev['status'] = 'verified'
        # Still recheck periodically for late recordings
        prev['next_acl_check_at'] = (now_sh() + datetime.timedelta(hours=1)).isoformat()
        prev['verified_at'] = now_sh().isoformat()
    elif result.get('action') == 'waiting_records':
        prev['status'] = 'waiting_records'
        prev['next_acl_check_at'] = (now_sh() + datetime.timedelta(minutes=5)).isoformat()
    elif result.get('action') == 'dry_run':
        prev['status'] = 'dry_run'
        prev['next_acl_check_at'] = (now_sh() + datetime.timedelta(minutes=10)).isoformat()
    else:
        prev['status'] = 'needs_retry'
        prev['next_acl_check_at'] = (now_sh() + datetime.timedelta(minutes=5)).isoformat()
        prev['last_error'] = json.dumps({k: result.get(k) for k in ('action', 'why')}, ensure_ascii=False)[:500]
    cand['record_acl'] = prev


def due_for_acl_check(cand: dict) -> bool:
    ra = cand.get('record_acl') or {}
    nxt = parse_dt(ra.get('next_acl_check_at'))
    if nxt is None:
        return True
    return now_sh() >= nxt
