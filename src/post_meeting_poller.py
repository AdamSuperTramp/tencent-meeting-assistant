#!/usr/bin/env python3
"""Mini runtime: A unchanged; B immediate, independent, anchored sparse retries (t296u)."""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import pathlib
import sys
import time
import traceback

from settings import CODE_DIR, DATA_DIR, OPENCLAW_AUTOMATION_CONFIG, OPENCLAW_LOG_DIR
BASE = DATA_DIR
sys.path.insert(0, str(CODE_DIR))

import business_export as e  # noqa: E402
import brain_delivery

TZ = datetime.timezone(datetime.timedelta(hours=8))
CONFIG_PATH = OPENCLAW_AUTOMATION_CONFIG
LEGACY_DRY = OPENCLAW_AUTOMATION_CONFIG.with_name('post-meeting-export.dry-run.json')
STATE_PATH = BASE / 'poller-state' / 'post-meeting-poller-state.json'
LOG_PATH = OPENCLAW_LOG_DIR / 'post-meeting-poller.log'
DEFAULT_TARGET = e.AI_GROUP_CHAT_ID
LOOKBACK_HOURS = 12
TIMEOUT_HOURS = 12
BACKOFF_SECONDS = [60, 300, 900]  # legacy; B uses fixed 5min×6 (t195u)
START_DELAY_MINUTES = 20  # Judgment A arms only after local start_time + 20m
A_INTERVAL_SECONDS = 600  # A end-check cadence (independent of LaunchAgent 300s)
A_MAX_HOURS = 4  # fixed window from arm_at (=start+20m), not extended by failures/restarts
B_INTERVAL_SECONDS = 60  # legacy compatibility only; use B_SLOTS_SECONDS
B_MAX_ATTEMPTS = 11
B_SLOTS_SECONDS = [0, 60, 120, 240, 360, 480, 600, 900, 1200, 1500, 1800]
B_FIRST_DELAY_SECONDS = 0
TERMINAL_STATUSES = frozenset({
    'auto_sent', 'a_gave_up', 'b_gave_up', 'cancelled_skip',
})


def now_sh():
    return datetime.datetime.now(TZ)


def iso(dt=None):
    return (dt or now_sh()).isoformat()


def log(msg, **extra):
    line = {'ts': iso(), 'msg': msg}
    if extra:
        line.update(extra)
    text = json.dumps(line, ensure_ascii=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open('a') as f:
        f.write(text + '\n')
    print(text, flush=True)


def load_config():
    path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_DRY
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg.setdefault('enabled', False)
    cfg.setdefault('send_enabled', False)
    cfg.setdefault('mode', 'dry-run')
    cfg.setdefault('target_chat_id', DEFAULT_TARGET)
    cfg.setdefault('global_interval_seconds', 60)
    cfg.setdefault('lookback_hours', LOOKBACK_HOURS)
    cfg.setdefault('timeout_hours_after_end', TIMEOUT_HOURS)
    cfg.setdefault('backoff_seconds', BACKOFF_SECONDS)
    cfg.setdefault('judgment_a_start_delay_minutes', START_DELAY_MINUTES)
    cfg.setdefault('judgment_a_interval_seconds', A_INTERVAL_SECONDS)
    cfg.setdefault('judgment_a_max_hours', A_MAX_HOURS)
    cfg.setdefault('judgment_b_interval_seconds', B_INTERVAL_SECONDS)
    cfg.setdefault('judgment_b_max_attempts', B_MAX_ATTEMPTS)
    cfg.setdefault('judgment_b_first_delay_seconds', B_FIRST_DELAY_SECONDS)
    cfg.setdefault('global_interval_seconds', B_INTERVAL_SECONDS)
    cfg.setdefault('allow_null_created_from_chat', True)
    return cfg, path


def atomic_state_write(data):
    e._atomic_json_write(STATE_PATH, data)


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return {'version': 1, 'candidates': {}, 'updated_at': iso()}
    data = json.loads(STATE_PATH.read_text())
    data.setdefault('candidates', {})
    return data


def parse_dt(value):
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



def start_delay_minutes(cfg):
    return float(cfg.get('judgment_a_start_delay_minutes') or cfg.get('start_delay_minutes') or START_DELAY_MINUTES)


def local_start_time(mid, prov_entry=None, registry_entry=None, cand=None):
    """Resolve durable local start_time only. Never invent; never MCP."""
    for src in (prov_entry or {}, registry_entry or {}, cand or {}):
        for key in ('start_time', 'scheduled_start'):
            dt = parse_dt(src.get(key))
            if dt is not None:
                return dt
    return None


def local_cancelled(prov_entry=None, registry_entry=None, cand=None):
    for src in (prov_entry or {}, registry_entry or {}, cand or {}):
        if src.get('cancelled') is True:
            return True
    return False


def explicit_meeting_ended(meta):
    """Ended only from explicit live status — NOT from scheduled end_time alone."""
    if not isinstance(meta, dict):
        return False, 'no_meta'
    for key in ('live_status', 'meeting_state', 'status', 'state', 'meeting_status'):
        val = str(meta.get(key) or '').strip()
        if not val:
            continue
        low = val.lower()
        if any(x in val for x in ('已结束', '已取消', '已终止')) or any(x in low for x in ('ended', 'cancelled', 'canceled', 'terminated')):
            return True, f'{key}={val}'
        if any(x in val for x in ('进行中', '未开始', '待开始', '即将开始')) or any(x in low for x in ('in_progress', 'in-progress', 'waiting', 'ready', 'not_started', 'scheduled')):
            return False, f'{key}={val}'
    # No explicit status: refuse to treat scheduled end_time as ended.
    return False, 'no_explicit_ended_status'


def a_arm_at(start_dt, cfg):
    return start_dt + datetime.timedelta(minutes=start_delay_minutes(cfg))


def a_deadline_at(start_dt, cfg):
    """Fixed: start + 20m + 4h. Recomputed from start_time only — never extended."""
    hours = float(cfg.get('judgment_a_max_hours') or A_MAX_HOURS)
    return a_arm_at(start_dt, cfg) + datetime.timedelta(hours=hours)


def a_interval_seconds(cfg):
    return int(cfg.get('judgment_a_interval_seconds') or A_INTERVAL_SECONDS)


def b_interval_seconds(cfg):
    return int(cfg.get('judgment_b_interval_seconds') or B_INTERVAL_SECONDS)


def b_max_attempts(cfg):
    return int(cfg.get('judgment_b_max_attempts') or B_MAX_ATTEMPTS)


def b_first_delay_seconds(cfg):
    return int(cfg.get('judgment_b_first_delay_seconds') or B_FIRST_DELAY_SECONDS)


def due_for_a_end_check(cand, cfg):
    nxt = parse_dt(cand.get('next_a_check_at'))
    if nxt is None:
        return True
    return now_sh() >= nxt


def schedule_a_retry(cand, cfg):
    delay = a_interval_seconds(cfg)
    cand['next_a_check_at'] = iso(now_sh() + datetime.timedelta(seconds=delay))
    return delay


def mark_a_gave_up(cand, why):
    cand['status'] = 'a_gave_up'
    cand['a_gave_up_at'] = iso()
    cand['a_gave_up_why'] = why
    cand['next_a_check_at'] = None
    cand['next_check_at'] = None


def init_b_window(cand, confirmed_at_iso):
    cand['a_confirmed_at'] = confirmed_at_iso
    cand['b_anchor_at'] = confirmed_at_iso
    cand.setdefault('b_attempts', 0)
    cand.setdefault('b_components', {})
    cand['next_check_at'] = confirmed_at_iso


def b_expired(cand):
    anchor = parse_dt(cand.get('b_anchor_at'))
    return bool(anchor and now_sh() > anchor + datetime.timedelta(seconds=1800))


def finish_b_incomplete(cand, why):
    cand.update(status='b_gave_up', b_gave_up_at=iso(), next_check_at=None,
                last_error=why)
    cand['b_incomplete'] = [k for k in ('transcript', 'recording_link')
                            if (cand.get('b_components', {}).get(k) or {}).get('status') != 'verified']


def due_for_b_check(cand, cfg):
    if cand.get('status') in TERMINAL_STATUSES:
        return False
    nxt = parse_dt(cand.get('next_check_at'))
    return nxt is None or now_sh() >= nxt


def schedule_b_after_attempt(cand, cfg):
    # Choose a strictly future absolute slot. Never burst-replay missed slots.
    anchor = parse_dt(cand['b_anchor_at'])
    elapsed = (now_sh() - anchor).total_seconds()
    floor = max(elapsed, float(cand.get('b_last_slot_seconds', -1)))
    slots = [x for x in B_SLOTS_SECONDS if x > floor]
    if not slots or int(cand.get('b_attempts') or 0) >= B_MAX_ATTEMPTS:
        finish_b_incomplete(cand, 'b_window_incomplete')
        return None
    cand['next_check_at'] = iso(anchor + datetime.timedelta(seconds=slots[0]))
    return slots[0] - elapsed


def collect_known_company_mids(prov, registry, state):
    """Known company meeting ids from local files only (no MCP discovery)."""
    mids = set()
    for mid, p in (prov or {}).items():
        if p.get('probe') is True:
            continue
        if p.get('mark') == 'company' or e.FEISHU_CREATED_REASON in str(p.get('reason') or ''):
            mids.add(str(mid))
    for mid, reg in (registry or {}).items():
        if (reg or {}).get('mark') == 'company':
            mids.add(str(mid))
    for mid, cand in (state.get('candidates') or {}).items():
        # Already-tracked candidates remain known (may lack start_time but have ended_at).
        if cand:
            mids.add(str(mid))
    return mids


def eligible_for_auto_send(mid, prov_entry, cfg):
    """Feishu-assistant provenance only; target AI创业小组 (or null created_from backfill)."""
    if not prov_entry:
        return False, 'no_provenance'
    if prov_entry.get('mark') != 'company' and prov_entry.get('reason') != e.FEISHU_CREATED_REASON:
        if e.FEISHU_CREATED_REASON not in str(prov_entry.get('reason') or ''):
            return False, 'not_company_provenance'
    # Skip obvious fake probe ids from tests unless they somehow ended (they won't).
    if prov_entry.get('probe') is True:
        return False, 'probe_entry'
    chat = prov_entry.get('created_from_chat_id')
    target = cfg['target_chat_id']
    if chat is None or chat == '':
        if cfg.get('allow_null_created_from_chat', True):
            return True, 'null_created_from_defaults_to_target'
        return False, 'null_created_from_disallowed'
    if chat == target:
        return True, 'created_in_target_group'
    # Created in another allowlisted group (e.g. test): do not auto-send to AI创业小组.
    return False, 'created_from_other_chat:' + str(chat)


def candidate_record(state, mid):
    c = state['candidates'].setdefault(mid, {
        'meeting_id': mid,
        'first_seen_at': iso(),
        'ended_at': None,
        'status': 'watching',
        'backoff_index': 0,
        'next_check_at': None,
        'last_error': None,
        'attempts': 0,
    })
    return c


def load_registry_meetings():
    path = BASE / 'registry.json'
    if not path.exists():
        return {}
    try:
        return (json.loads(path.read_text()).get('meetings') or {})
    except Exception:
        return {}


def process_candidate(mid, ended_meta, prov, cfg, state, force=False):
    cand = candidate_record(state, mid)
    # 历史双附件均已送达的会议视为完成，不因改版补发录播。
    if cand.get('status') not in TERMINAL_STATUSES:
        old_transcript, _, _ = e.load_auto_send_ledger(mid, 'transcript', cfg['target_chat_id'])
        old_minutes, _, _ = e.load_auto_send_ledger(mid, 'minutes', cfg['target_chat_id'])
        if all((item or {}).get('result', {}).get('status') == 'file-message-readback-verified'
               for item in (old_transcript, old_minutes)):
            cand['status'] = 'auto_sent'
            cand['next_check_at'] = None
            atomic_state_write(state)
            return {'meeting_id': mid, 'action': 'legacy_auto_sent'}
    # Force flags cannot reopen completed/sealed historical candidates.
    if cand.get('status') in TERMINAL_STATUSES:
        return {'meeting_id': mid, 'action': 'terminal_skip'}
    cand['subject'] = (ended_meta or {}).get('subject') or prov.get('subject') or cand.get('subject')
    if not cand.get('b_anchor_at'):
        init_b_window(cand, cand.get('a_confirmed_at') or iso())
    if b_expired(cand):
        finish_b_incomplete(cand, 'b_deadline_30m_incomplete')
        atomic_state_write(state)
        log('b_incomplete', meeting_id=mid, missing=cand['b_incomplete'])
        return {'meeting_id': mid, 'action': 'b_gave_up'}
    if not due_for_b_check(cand, cfg):
        return {'meeting_id': mid, 'action': 'wait_b'}
    cooldown = parse_dt(state.get('b_quota_retry_at'))
    if cooldown and now_sh() < cooldown:
        return {'meeting_id': mid, 'action': 'b_quota_backoff'}
    elapsed = (now_sh() - parse_dt(cand['b_anchor_at'])).total_seconds()
    slot = max(x for x in B_SLOTS_SECONDS if x <= max(0, elapsed))
    if slot <= cand.get('b_last_slot_seconds', -1):
        schedule_b_after_attempt(cand, cfg)
        return {'meeting_id': mid, 'action': 'wait_b'}
    cand['b_last_slot_seconds'] = slot
    cand['b_attempts'] = int(cand.get('b_attempts') or 0) + 1
    cand['status'] = 'partial'
    parts = cand.setdefault('b_components', {})
    dry = (os.environ.get('POST_MEETING_POLLER_FORCE_DRY') == '1'
           or cfg.get('mode') != 'live' or not cfg.get('enabled') or not cfg.get('send_enabled'))
    # Persist consumed slot before any network/side effect; restart skips stale slots.
    schedule_b_after_attempt(cand, cfg)
    atomic_state_write(state)
    for kind in ('transcript', 'recording_link'):
        comp = parts.setdefault(kind, {'status': 'pending'})
        if comp.get('status') == 'verified':
            continue
        if b_expired(cand):
            break
        def checkpoint():
            atomic_state_write(state)
        try:
            e.poller_deliver_component(mid, kind, cfg['target_chat_id'], comp,
                                       dry_run=dry, checkpoint=checkpoint)
        except e.PollerQuotaError:
            comp.update(status='quota_backoff', last_error='quota500246', last_checked_at=iso())
            state['b_quota_retry_at'] = iso(now_sh() + datetime.timedelta(seconds=300))
            log('b_quota_backoff', meeting_id=mid, kind=kind, retry_at=state['b_quota_retry_at'])
            atomic_state_write(state)
            break
        except Exception as ex:
            # Stable diagnostic vocabulary only, never raw SDK/header/token errors.
            detail = str(ex).lower()
            reason = next((label for needle, label in (
                ('no complete readable', 'transcript_not_generated'),
                ('metadata incomplete', 'source_metadata_incomplete'),
                ('incomplete transcript', 'transcript_incomplete'),
                ('readback', 'delivery_readback_unverified'),
                ('blind resend', 'delivery_outcome_uncertain'),
                ('timed out', 'delivery_timeout'),
                ('hash mismatch', 'prepared_source_changed'),
                ('filename', 'native_filename_unavailable'),
            ) if needle in detail), 'operation_failed')
            comp['last_error'] = type(ex).__name__ + ':' + reason
            old, _, _ = e.load_auto_send_ledger(mid, kind, cfg['target_chat_id'])
            comp['status'] = 'uncertain' if old else 'delivery_failed' if comp.get('ready_at') else 'not_ready'
        comp['last_checked_at'] = iso()
        atomic_state_write(state)
        log('b_component', meeting_id=mid, kind=kind, status=comp.get('status'),
            first_ready_at=comp.get('ready_at'), sent_verified_at=comp.get('sent_verified_at'),
            reason=comp.get('last_error'), slot_seconds=slot, b_attempts=cand['b_attempts'])
    if all((parts.get(k) or {}).get('status') == 'verified' for k in ('transcript', 'recording_link')):
        cand.update(status='auto_sent', sent_at=iso(), next_check_at=None, last_error=None)
        cand.pop('b_incomplete', None)
        log('auto_sent', meeting_id=mid, independent_components=True, b_attempts=cand['b_attempts'])
    elif b_expired(cand) or cand['b_attempts'] >= B_MAX_ATTEMPTS:
        finish_b_incomplete(cand, 'b_window_incomplete')
        log('b_incomplete', meeting_id=mid, missing=cand['b_incomplete'])
    else:
        cand['status'] = 'partial'
        schedule_b_after_attempt(cand, cfg)
    atomic_state_write(state)
    return {'meeting_id': mid, 'action': cand['status'], 'b_attempts': cand['b_attempts']}


def deliver_ready_brain_transcripts(cfg, state, prov, registry):
    start = parse_dt(cfg.get('brain_delivery_since'))
    target = cfg.get('target_chat_id')
    if (not start or not cfg.get('enabled') or not cfg.get('send_enabled') or cfg.get('mode') != 'live'
            or not target or target != DEFAULT_TARGET or os.environ.get('POST_MEETING_POLLER_FORCE_DRY') == '1'
            or CONFIG_PATH.with_name('post-meeting-poller.PAUSED').exists()):
        return
    for mid, cand in state.get('candidates', {}).items():
        origin = prov.get(mid) or {}
        reg = registry.get(mid) or {}
        created = parse_dt(origin.get('created_at'))
        payload = cand.get('b_components', {}).get('transcript', {}).get('ready_payload') or {}
        if (not created or created < start or origin.get('created_from_chat_id') != target
                or not eligible_for_auto_send(mid, origin, cfg)[0] or reg.get('mark') != 'company'
                or local_cancelled(origin, reg, cand) or not cand.get('a_ended_confirmed')
                or not payload.get('export_id')):
            continue
        job = cand.setdefault('brain_delivery', {})
        before = job.get('status'), job.get('retry_at')
        if target not in e.allowed_group_chat_ids():
            return
        def save_job():
            with (BASE / 'poller-state' / 'poller.run.lock').open('a') as state_lock:
                fcntl.flock(state_lock, fcntl.LOCK_EX)
                latest = load_state()
                latest.setdefault('candidates', {}).setdefault(mid, {})['brain_delivery'] = dict(job)
                atomic_state_write(latest)
        brain_delivery.deliver(mid, payload['export_id'], job, save_job)
        if before != (job.get('status'), job.get('retry_at')):
            log('brain_delivery', meeting_id=mid, status=job.get('status'), reason=job.get('reason'))
            # 每轮最多处理一场，给下一次飞书轮询留出时间。
            return



def run_brain_delivery():
    folder = BASE / 'poller-state'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'brain.run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with (folder / 'poller.run.lock').open('a') as state_lock:
            fcntl.flock(state_lock, fcntl.LOCK_EX)
            cfg, _ = load_config()
            state = load_state()
            prov = e.load_feishu_created_provenance().get('meetings') or {}
            registry = load_registry_meetings()
        deliver_ready_brain_transcripts(cfg, state, prov, registry)


def run_once(force_meeting_id=None, force_send=False):
    cfg, cfg_path = load_config()
    if force_send:
        cfg = dict(cfg)
        cfg['enabled'] = True
        cfg['send_enabled'] = True
        cfg['mode'] = 'live'

    lock_path = BASE / 'poller-state' / 'poller.run.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load_state()
        prov = e.load_feishu_created_provenance().get('meetings') or {}
        registry = load_registry_meetings()
        known = collect_known_company_mids(prov, registry, state)
        if force_meeting_id:
            known.add(str(force_meeting_id))

        blockers_no_start = []
        too_early = []
        cancelled_skip = []
        a_gave_up = []
        a_due = []
        ended = {}
        terminal_skip = []

        for mid in sorted(known):
            p = prov.get(mid) or {}
            reg = registry.get(mid) or {}
            cand = (state.get('candidates') or {}).get(mid) or {}
            st = cand.get('status')
            latest_start = local_start_time(mid, p, reg, cand)
            cached_start = parse_dt(cand.get('scheduled_start'))
            if latest_start and cached_start and latest_start != cached_start and not cand.get('a_ended_confirmed') and st in ('watching', 'watching_in_progress', 'a_gave_up'):
                cand['scheduled_start'] = latest_start.isoformat()
                cand['next_a_check_at'] = None
                cand['status'] = st = 'watching'
                cand.pop('a_gave_up_at', None)
                cand.pop('a_gave_up_why', None)
            if local_cancelled(p, reg, cand):
                cancelled_skip.append(mid)
                if mid in (state.get('candidates') or {}):
                    state['candidates'][mid]['status'] = 'cancelled_skip'
                continue
            if st in TERMINAL_STATUSES and not (force_meeting_id and mid == str(force_meeting_id)):
                terminal_skip.append(mid)
                if st in ('auto_sent',) or cand.get('ended_at') or cand.get('a_ended_confirmed'):
                    # Keep ended cache for nothing — auto_sent already done
                    pass
                continue
            # Legacy/cached ended → B only (no A MCP)
            if cand.get('a_ended_confirmed') or (
                cand.get('ended_at') and cand.get('status') not in ('watching', 'watching_in_progress')
            ):
                ended[mid] = {
                    'meeting_id': mid,
                    'end_time': cand.get('ended_at'),
                    'subject': cand.get('subject') or p.get('subject') or reg.get('subject'),
                }
                if not cand.get('b_anchor_at') and mid in (state.get('candidates') or {}):
                    init_b_window(state['candidates'][mid], cand.get('a_confirmed_at') or iso())
                continue

            start = local_start_time(mid, p, reg, cand)
            if start is None:
                blockers_no_start.append(mid)
                continue
            arm = a_arm_at(start, cfg)
            deadline = a_deadline_at(start, cfg)
            # Persist fixed deadline from start (recomputed; never extended)
            c = candidate_record(state, mid)
            c['scheduled_start'] = start.isoformat()
            c['a_arm_at'] = arm.isoformat()
            c['a_deadline_at'] = deadline.isoformat()
            c.setdefault('subject', p.get('subject') or reg.get('subject'))

            if now_sh() < arm:
                too_early.append({'meeting_id': mid, 'start_time': start.isoformat(), 'a_arm_at': arm.isoformat()})
                continue
            if now_sh() > deadline:
                mark_a_gave_up(c, 'past_a_deadline_start_plus_20m_plus_4h')
                a_gave_up.append(mid)
                continue
            if not force_meeting_id and not due_for_a_end_check(c, cfg):
                continue
            a_due.append(mid)

        mcp_calls = 0
        results = []
        log('cycle_start', enabled=cfg.get('enabled'), send_enabled=cfg.get('send_enabled'),
            mode=cfg.get('mode'), config=str(cfg_path),
            gate='start_plus_delay_v2', start_delay_min=start_delay_minutes(cfg),
            a_max_hours=float(cfg.get('judgment_a_max_hours') or A_MAX_HOURS),
            b_max_attempts=b_max_attempts(cfg),
            known_company=len(known), a_due=len(a_due), ended_cached=len(ended),
            blockers_no_start=len(blockers_no_start), too_early=len(too_early),
            a_gave_up=len(a_gave_up), cancelled_skip=len(cancelled_skip),
            provenance=len(prov), force_meeting_id=force_meeting_id,
            independent_components=True)
        if blockers_no_start:
            log('a_gate_block_no_start', meeting_ids=blockers_no_start[:50], n=len(blockers_no_start))

        # Judgment A: get_meeting by id only
        for mid in a_due:
            try:
                info = e.mcp_call('get_meeting', {'meeting_id': mid, 'is_compact': True})
                mcp_calls += 1
            except Exception as ex:
                c = candidate_record(state, mid)
                schedule_a_retry(c, cfg)
                # still within deadline? checked next cycle
                results.append({'meeting_id': mid, 'action': 'a_lookup_error', 'why': str(ex)[:200]})
                continue
            if isinstance(info, dict) and info.get('error_info'):
                c = candidate_record(state, mid)
                schedule_a_retry(c, cfg)
                results.append({'meeting_id': mid, 'action': 'a_mcp_error', 'why': info.get('error_info')})
                continue
            rows = (info or {}).get('meeting_info_list') or []
            meta = rows[0] if rows else None
            if not meta:
                c = candidate_record(state, mid)
                schedule_a_retry(c, cfg)
                results.append({'meeting_id': mid, 'action': 'a_no_meeting_row'})
                continue
            ended_ok, why = explicit_meeting_ended(meta)
            c = candidate_record(state, mid)
            c['subject'] = meta.get('subject') or c.get('subject')
            if meta.get('start_time') and not c.get('scheduled_start'):
                c['scheduled_start'] = meta.get('start_time')
            if not ended_ok:
                # Re-check deadline after failed A attempt
                start = local_start_time(mid, prov.get(mid), registry.get(mid), c)
                if start and now_sh() > a_deadline_at(start, cfg):
                    mark_a_gave_up(c, 'past_a_deadline_after_not_ended')
                    a_gave_up.append(mid)
                    results.append({'meeting_id': mid, 'action': 'a_gave_up', 'why': c.get('a_gave_up_why')})
                    continue
                delay = schedule_a_retry(c, cfg)
                c['status'] = 'watching_in_progress'
                c['last_a_status'] = why
                results.append({'meeting_id': mid, 'action': 'a_not_ended', 'why': why, 'backoff_s': delay})
                continue
            confirmed = iso()
            c['a_ended_confirmed'] = True
            c['a_ended_why'] = why
            c['ended_at'] = confirmed  # B/timeout anchor uses A confirm time, not planned end
            c['ended_meta_end_time'] = meta.get('end_time')
            c['next_a_check_at'] = None
            init_b_window(c, confirmed)
            c['status'] = 'ended_pending_materials'
            ended[mid] = meta
            ended[mid] = dict(meta)
            ended[mid]['_a_confirmed_at'] = confirmed
            results.append({'meeting_id': mid, 'action': 'a_ended', 'why': why, 'b_first_at': c.get('next_check_at')})


        # Judgment B for confirmed ended ∩ feishu send-eligible
        mids = set()
        for mid, p in prov.items():
            ok, why = eligible_for_auto_send(mid, p, cfg)
            if not ok:
                continue
            if mid in ended:
                mids.add(mid)
        if force_meeting_id and str(force_meeting_id) in ended:
            mids.add(str(force_meeting_id))
        # Also B-due candidates already in ended cache / state
        for mid, cand in (state.get('candidates') or {}).items():
            if cand.get('a_ended_confirmed') or (cand.get('ended_at') and cand.get('status') not in ('watching', 'watching_in_progress', 'a_gave_up')):
                if mid not in ended and cand.get('status') not in TERMINAL_STATUSES:
                    ended[mid] = {'meeting_id': mid, 'end_time': cand.get('ended_at'), 'subject': cand.get('subject')}
                p = prov.get(mid) or {}
                ok, _ = eligible_for_auto_send(mid, p, cfg)
                if ok or (force_meeting_id and mid == str(force_meeting_id)):
                    mids.add(mid)

        for mid in sorted(mids):
            p = prov.get(mid) or {}
            ok, why = eligible_for_auto_send(mid, p, cfg)
            if not ok and not (force_meeting_id and mid == str(force_meeting_id)):
                results.append({'meeting_id': mid, 'action': 'skip', 'why': why})
                continue
            if force_meeting_id and mid == str(force_meeting_id) and not ok:
                if not e.is_feishu_assistant_created(mid):
                    results.append({'meeting_id': mid, 'action': 'skip', 'why': 'force_but_no_feishu_provenance'})
                    continue
            meta = ended.get(mid) or {'meeting_id': mid}
            force = bool(force_meeting_id and mid == str(force_meeting_id))
            results.append(process_candidate(mid, meta, p, cfg, state, force=force))


        state['updated_at'] = iso()
        state['last_cycle'] = {
            'at': iso(),
            'gate': 'start_plus_delay_v2',
            'mcp_calls_a': mcp_calls,
            'ended_confirmed': len(ended),
            'a_gave_up': a_gave_up[:50],
            'blockers_no_start': blockers_no_start[:50],
            'results': [{k: r.get(k) for k in ('meeting_id', 'action', 'why', 'error', 'b_attempts') if k in r} for r in results],
        }
        atomic_state_write(state)
        log('cycle_end', n_results=len(results), actions=[r.get('action') for r in results],
            mcp_calls_a=mcp_calls, ended_confirmed=len(ended),
            blockers_no_start=len(blockers_no_start), a_gave_up=len(a_gave_up))
        return {
            'config': {k: cfg.get(k) for k in (
                'enabled', 'send_enabled', 'mode', 'target_chat_id',
                'judgment_a_start_delay_minutes', 'judgment_a_max_hours',
                'judgment_b_max_attempts', 'judgment_b_interval_seconds')},
            'results': results, 'state_path': str(STATE_PATH),
            'gate': {
                'blockers_no_start': blockers_no_start, 'too_early': too_early,
                'a_due': a_due, 'a_gave_up': a_gave_up, 'mcp_calls_a': mcp_calls,
            },
        }



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='Run one poll cycle')
    ap.add_argument('--force-meeting-id', default=None)
    ap.add_argument('--force-send', action='store_true', help='Override config to live-send for this run')
    ap.add_argument('--self-check', action='store_true', help='Dry-run readiness for eligible ended meetings')
    args = ap.parse_args()
    if args.self_check:
        os.environ['POST_MEETING_POLLER_FORCE_DRY'] = '1'
        out = run_once(force_meeting_id=args.force_meeting_id, force_send=False)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if args.once or args.force_meeting_id:
        out = run_once(force_meeting_id=args.force_meeting_id, force_send=args.force_send)
        if not args.force_meeting_id and not args.force_send:
            run_brain_delivery()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    ap.error('Use --once or --self-check')


if __name__ == '__main__':
    try:
        main()
    except BlockingIOError:
        log('skip_locked_another_run')
        print(json.dumps({'error': 'locked'}, ensure_ascii=False))
        sys.exit(0)
    except Exception:
        log('fatal', error=traceback.format_exc()[-1500:])
        raise
