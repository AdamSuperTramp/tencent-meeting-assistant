"""会后资料进度和按项补查，复用既有发送账本。"""
import datetime
import fcntl
import json

KINDS = ("transcript", "recording_link")
VERIFIED = {
    "transcript": "file-message-readback-verified",
    "recording_link": "text-message-readback-verified",
}
LABELS = {
    "verified": "已送达",
    "not_ready": "资料尚未就绪",
    "ready": "资料已就绪，等待发送",
    "uncertain": "发送结果待核对",
    "delivery_failed": "发送失败，可补查",
    "quota_backoff": "接口限流，等待重试",
    "pending": "尚未检查",
}


def authorize(service, args):
    if not set(args).issubset({"meeting_id", "kind", "target_chat_id"}):
        raise ValueError("参数不支持")
    mid = str(args.get("meeting_id", ""))
    dest = service.resolve_delivery_target(args.get("target_chat_id"))
    service.permitted(mid)
    target = service.AI_GROUP_CHAT_ID
    if dest["kind"] == "group" and dest["chat_id"] != target:
        raise PermissionError("请在会后自动投递目标群或本人私聊查询")
    return mid, target


def read_status(service, mid, target, state):
    cand = state.get("candidates", {}).get(mid, {})
    components = {}
    for kind in KINDS:
        comp = cand.get("b_components", {}).get(kind, {})
        ledger, _, _ = service.load_auto_send_ledger(mid, kind, target)
        result = (ledger or {}).get("result", {})
        status = comp.get("status", "pending")
        if result.get("status") == VERIFIED[kind]:
            status = "verified"
        elif ledger:
            status = "uncertain"
        if status == "pending" and cand.get("status") in ("auto_sent", "b_gave_up"):
            status = "unknown_legacy"
        components[kind] = {
            "status": status,
            "label": LABELS.get(status, "历史记录不完整，需补查核对"),
            "verified_at": comp.get("sent_verified_at"),
        }
    brain = cand.get("brain_delivery")
    if brain:
        phase = brain.get("status")
        label = {
            "accepted": "逐字稿已交给大脑",
            "retry": "等待自动重试",
            "uploading": "上传回执待核对",
            "opening": "新会话回执待核对",
            "uncertain": "提交结果待核对",
        }.get(phase, "等待提交")
        components["brain_transcript"] = {"status": phase, "label": label}
    return {
        "meeting_id": mid,
        "tracking_status": cand.get("status", "not_tracked"),
        "components": components,
        "reply_instruction": "分别说明逐字稿、录播和已有的大脑投递状态。历史记录不完整时说明需核对。",
    }


def status(service, args):
    mid, target = authorize(service, args)
    path = service.BASE / "poller-state/post-meeting-poller-state.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return read_status(service, mid, target, state)


def retry(service, args):
    from post_meeting_poller import explicit_meeting_ended
    mid, target = authorize(service, args)
    kind = args.get("kind")
    if kind not in (*KINDS, "missing"):
        raise ValueError("请选择 transcript、recording_link 或 missing")
    cfg = json.loads(service.OPENCLAW_AUTOMATION_CONFIG.read_text())
    paused = service.OPENCLAW_AUTOMATION_CONFIG.with_name("post-meeting-poller.PAUSED")
    if paused.exists() or not cfg.get("enabled") or not cfg.get("send_enabled") or cfg.get("mode") != "live":
        raise PermissionError("会后投递当前已暂停或关闭")
    folder = service.BASE / "poller-state"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "poller.run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("正在检查会后资料，请稍后补查") from None
        path = folder / "post-meeting-poller-state.json"
        state = json.loads(path.read_text()) if path.exists() else {"version": 1, "candidates": {}}
        provenance = service.load_feishu_created_provenance().get("meetings", {}).get(mid, {})
        from post_meeting_poller import eligible_for_auto_send
        if not eligible_for_auto_send(mid, provenance, cfg)[0] or cfg.get("target_chat_id") != target:
            raise PermissionError("该会议未授权向自动投递目标群发送")
        cand = state.setdefault("candidates", {}).setdefault(mid, {})
        if not cand.get("a_ended_confirmed"):
            info = service.mcp_call("get_meeting", {"meeting_id": mid, "is_compact": True})
            rows = info.get("meeting_info_list") or []
            if not rows or not explicit_meeting_ended(rows[0])[0]:
                raise RuntimeError("尚未确认会议已结束")
            cand["a_ended_confirmed"] = True
        selected = KINDS if kind == "missing" else (kind,)
        for item in selected:
            comp = cand.setdefault("b_components", {}).setdefault(item, {})
            if comp.get("status") == "verified":
                continue
            def checkpoint():
                service._atomic_json_write(path, state)
            try:
                service.poller_deliver_component(mid, item, target, comp, checkpoint=checkpoint)
            except Exception:
                ledger, _, _ = service.load_auto_send_ledger(mid, item, target)
                comp["status"] = "uncertain" if ledger else "delivery_failed"
            checkpoint()
        if all(cand.get("b_components", {}).get(item, {}).get("status") == "verified" for item in KINDS):
            cand["status"] = "auto_sent"
            cand["next_check_at"] = None
        state["updated_at"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        service._atomic_json_write(path, state)
        return read_status(service, mid, target, state)
