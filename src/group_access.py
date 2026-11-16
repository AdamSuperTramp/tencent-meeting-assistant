"""飞书群请求的腾讯会议工具访问规则。"""
import json

LOCAL_TOOLS = {
    "prepare_meeting_export", "send_prepared_meeting_export",
    "deliver_meeting_markdown", "deliver_meeting_recording_link",
    "get_meeting_delivery_status", "retry_meeting_delivery",
}


def company_ids(service):
    path = service.BASE / "registry.json"
    registry = json.loads(path.read_text()).get("meetings", {}) if path.exists() else {}
    ids = {str(mid) for mid, row in registry.items() if row.get("mark") == "company"}
    provenance = service.load_feishu_created_provenance().get("meetings", {})
    ids.update(str(mid) for mid, row in provenance.items() if row.get("mark") == "company")
    return ids


def route(service, name, args, call):
    if name in LOCAL_TOOLS:
        return None
    if name == "schedule_meeting":
        return None
    allowed = company_ids(service)
    mid = str(args.get("meeting_id") or "")
    if name in ("get_meeting", "update_meeting", "cancel_meeting"):
        if mid not in allowed:
            raise PermissionError("群聊仅可操作已登记的公司会议")
        return None
    if name == "search_records":
        if mid and mid not in allowed:
            raise PermissionError("群聊仅可查询已登记的公司会议")
        payload = service._tool_call_payload(call())
        if payload is None:
            raise RuntimeError("腾讯会议查询暂时失败")
        rows = payload.get("records") or []
        visible = [row for row in rows if str(row.get("meeting_id") or "") in allowed]
        return {"result": {"content": [{"type": "text", "text": json.dumps({
            "records": visible,
            "visible_count": len(visible),
            "scope": "registered_company_meetings",
        }, ensure_ascii=False)}]}}
    raise PermissionError("该工具尚未开放群聊访问，请在本人私聊使用")
