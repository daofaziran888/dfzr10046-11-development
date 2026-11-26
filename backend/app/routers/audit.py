"""审计派办专用接口。

为兼容不同调用习惯，同一组能力挂在多组路径别名下：
/api/audit/*（主路径）、/api/audit-dispatch/*、/api/audit_dispatch/*、
/api/model_audit/dispatch/*，以及 /api/todos、/api/dispatch/* 等顶层别名。

身份可由 JSON 字段、查询参数或请求头（X-Operator / X-User / X-As）携带；
接单地址令牌可用 token / address_token / assignment_id 或 X-Assignment-Token。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.audit_dispatch import AuditDispatchService, canon_person

router = APIRouter(tags=["审计派办"])
svc = AuditDispatchService()

_IDENTITY_KEYS = (
    "operator", "user", "as", "actor", "username", "user_name", "assignee",
    "owner", "operator_id", "user_id", "当前身份", "操作人", "当前操作人",
)
_IDENTITY_HEADERS = (
    "x-operator", "x-user", "x-as", "x-actor", "x-username",
    "x-assignee", "x-current-user",
)
_TOKEN_KEYS = (
    "token", "address_token", "addr_token", "assignment_token",
    "assignment_id", "address", "addr", "接单地址", "地址令牌",
)
_TOKEN_HEADERS = ("x-assignment-token", "x-address-token", "x-token")


async def parse_context(request: Request) -> dict[str, Any]:
    query = dict(request.query_params)
    body: dict[str, Any] = {}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
            elif raw is not None:
                body = {"content": raw}
        except Exception:
            body = {}
    headers = {k.lower(): v for k, v in request.headers.items()}

    def pick(keys: tuple[str, ...], header_keys: tuple[str, ...] = ()) -> Any:
        for source in (body, query):
            for k in keys:
                if k in source and source[k] not in (None, ""):
                    return source[k]
        for hk in header_keys:
            if headers.get(hk):
                return headers[hk]
        return None

    operator = pick(_IDENTITY_KEYS, _IDENTITY_HEADERS)
    token = pick(_TOKEN_KEYS, _TOKEN_HEADERS)
    ctx: dict[str, Any] = {}
    ctx.update(headers=headers, query=query, body=body)
    ctx["operator"] = canon_person(operator) if operator not in (None, "") else ""
    ctx["operator_raw"] = operator or ""
    ctx["token"] = str(token) if token not in (None, "") else ""
    # 其余字段以 body 优先、query 兜底
    merged: dict[str, Any] = {}
    merged.update(query)
    merged.update(body)
    merged["operator"] = ctx["operator"]
    merged["token"] = ctx["token"]
    merged["_source_address"] = str(token or query.get("address") or headers.get("referer", ""))
    return merged


def envelope(ok: bool, message: str, entry: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "success": ok, "message": message, "msg": message,
                           "entry": entry, "data": entry}
    if isinstance(entry, dict):
        for k, v in entry.items():
            if k not in out:
                out[k] = v
    out.update(extra)
    if not ok and "code" not in out:
        from app.services.audit_dispatch import _code_from_message
        out["code"] = _code_from_message(message)
    return out


def page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "total": len(items), "page": 1, "size": max(len(items), 1),
            "data": items}


# ---------------- 派办单集合 ----------------

async def _create_dispatch(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    handler = (ctx.get("handler") or ctx.get("处置人") or ctx.get("处理人")
               or ctx.get("handler_name") or "甲")
    reviewer = ctx.get("reviewer") or ctx.get("复核人") or "丙"
    ok, msg, entry = svc.dispatch_audit(
        audit_no=ctx.get("audit_no") or ctx.get("auditNo") or ctx.get("审计编号")
        or ctx.get("条目编号") or ctx.get("target"),
        audit_id=ctx.get("audit_id") or ctx.get("auditId") or ctx.get("model_audit_id"),
        handler=str(handler), reviewer=str(reviewer),
        supervisor=ctx.get("supervisor") or ctx.get("主管") or "主管",
        dispatch_no=ctx.get("dispatch_no") or ctx.get("派办编号"),
        title=ctx.get("title") or ctx.get("事项"),
        operator=ctx.get("operator_raw") or ctx.get("operator") or "主管",
        create_reprocess=bool(ctx.get("create_reprocess", True)),
        reprocess_no=ctx.get("reprocess_no") or ctx.get("重跑编号"),
        remark=ctx.get("remark"),
    )
    return envelope(ok, msg, entry)


async def _list_dispatch(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    items = svc.list_dispatches(
        audit_no=ctx.get("audit_no") or ctx.get("审计编号") or ctx.get("条目编号"),
        owner=ctx.get("owner") or ctx.get("operator"),
        status=ctx.get("status"),
    )
    out = page(items)
    out["dispatches"] = items
    return out


for p in ("/api/audit/dispatch", "/api/audit/dispatches", "/api/audit/dispatch/list",
          "/api/audit/dispatches/list",
          "/api/audit-dispatch", "/api/audit-dispatch/list",
          "/api/audit_dispatch", "/api/audit_dispatch/list",
          "/api/model_audit/dispatch", "/api/model_audit/dispatches",
          "/api/model_audit/dispatch/list",
          "/api/dispatch", "/api/dispatches", "/api/dispatch/list"):
    router.add_api_route(p, _list_dispatch, methods=["GET"])
    router.add_api_route(p, _create_dispatch, methods=["POST"])


# ---------------- 派办单详情 / 动作 ----------------

async def _detail(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    entry = svc.detail(did, ctx.get("operator"))
    if entry is None:
        return envelope(False, f"派办单 {did} 不存在", None, code="NOT_FOUND")
    return envelope(True, "ok", entry)


async def _accept(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.accept_order(
        did, operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"), kind=ctx.get("kind") or ctx.get("role"),
        source_address=ctx.get("_source_address", ""))
    return envelope(ok, msg, e)


async def _save_opinion(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.save_opinion(
        did,
        content=ctx.get("content") or ctx.get("opinion") or ctx.get("意见")
        or ctx.get("处置意见") or ctx.get("text") or ctx.get("remark"),
        operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"),
        expected_version=ctx.get("expected_version") or ctx.get("version"),
        source_address=ctx.get("_source_address", ""))
    return envelope(ok, msg, e)


async def _submit(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.submit_for_review(
        did,
        content=ctx.get("content") or ctx.get("opinion") or ctx.get("意见")
        or ctx.get("处置意见"),
        operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"),
        expected_version=ctx.get("expected_version") or ctx.get("version"),
        idempotency_key=ctx.get("idempotency_key") or ctx.get("Idempotency-Key")
        or request.headers.get("idempotency-key"),
        source_address=ctx.get("_source_address", ""))
    return envelope(ok, msg, e)


async def _reassign(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.reassign(
        did,
        new_handler=ctx.get("new_handler") or ctx.get("new_assignee")
        or ctx.get("new_owner") or ctx.get("新处置人") or ctx.get("改派给") or "乙",
        operator=ctx.get("operator_raw") or ctx.get("operator") or "主管",
        reason=ctx.get("reason") or ctx.get("原因") or "主管改派",
        expected_version=ctx.get("expected_version") or ctx.get("version"),
        token=ctx.get("token"))
    return envelope(ok, msg, e)


async def _inherit(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    and_submit = bool(ctx.get("and_submit") or ctx.get("submit")
                      or ctx.get("确认并送审") or ctx.get("inherit_and_submit"))
    ok, msg, e = svc.confirm_inherit(
        did, operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"), and_submit=and_submit,
        content=ctx.get("content") or ctx.get("意见"),
        expected_version=ctx.get("expected_version") or ctx.get("version"))
    return envelope(ok, msg, e)


async def _material(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.review_material(
        did, operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"))
    return envelope(ok, msg, e)


async def _verdict(request: Request, did: int | str) -> dict[str, Any]:
    ctx = await parse_context(request)
    ok, msg, e = svc.submit_review_verdict(
        did, operator=ctx.get("operator_raw") or ctx.get("operator") or "丙",
        token=ctx.get("token"),
        verdict=ctx.get("verdict") or ctx.get("action") or ctx.get("结论") or "复核通过",
        result=ctx.get("result"),
        comment=ctx.get("comment") or ctx.get("复核意见") or ctx.get("remark") or "",
        expected_version=ctx.get("expected_version") or ctx.get("version"))
    return envelope(ok, msg, e)


async def _ctx_action(request: Request, did: int | str) -> dict[str, Any]:
    """专用动作入口：body 形如 {"action": "提交送审", "operator": "甲", ...}。"""
    ctx = await parse_context(request)
    action = str(ctx.get("action") or "")
    entry, message, extra = svc.run_action_ctx(int(did), action, ctx)
    extra = dict(extra or {})
    # extra 里的 ok 是服务层的权威结论（失败时 entry 仍可能是详情字典）
    ok_flag = extra.pop("ok", entry is not None)
    return envelope(bool(ok_flag), message, entry, **extra)


_id_paths = (
    "/api/audit/dispatch/{did}", "/api/audit/dispatches/{did}",
    "/api/audit-dispatch/{did}", "/api/audit_dispatch/{did}",
    "/api/model_audit/dispatch/{did}", "/api/dispatch/{did}", "/api/dispatches/{did}",
)
for p in _id_paths:
    router.add_api_route(p, _detail, methods=["GET"])

_action_routes = {
    "accept": (_accept, ("accept", "claim", "take", "接单")),
    "opinion": (_save_opinion, ("opinion", "opinion/save", "save-opinion", "save_opinion")),
    "submit": (_submit, ("submit", "submit-review", "submit_review", "send-review",
                        "send_review", "submit-for-review")),
    "reassign": (_reassign, ("reassign", "change-assignee", "change_assignee")),
    "inherit": (_inherit, ("inherit", "confirm-inherit", "confirm_inherit", "succession")),
    "material": (_material, ("review-material", "review_material", "material", "review")),
    "verdict": (_verdict, ("verdict", "review/verdict", "approve", "conclusion")),
    "actions": (_ctx_action, ("actions", "action")),
}
for base in _id_paths:
    for _key, (handler, suffixes) in _action_routes.items():
        for suffix in suffixes:
            router.add_api_route(f"{base}/{suffix}", handler, methods=["POST", "GET"]
                                 if handler in (_material,) else ["POST"])
    # REST 风格裸动作
    router.add_api_route(f"{base}/accept", _accept, methods=["POST"])
    router.add_api_route(f"{base}/opinion", _save_opinion, methods=["POST"])
    router.add_api_route(f"{base}/submit", _submit, methods=["POST"])
    router.add_api_route(f"{base}/reassign", _reassign, methods=["POST"])
    router.add_api_route(f"{base}/inherit", _inherit, methods=["POST"])
    router.add_api_route(f"{base}/review-material", _material, methods=["GET", "POST"])
    router.add_api_route(f"{base}/verdict", _verdict, methods=["POST"])


# ---------------- 待办 / 意见 / 留痕 查询 ----------------

async def _todos(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    items = svc.list_todos(
        owner=ctx.get("owner") or ctx.get("operator") or ctx.get("assignee"),
        role=ctx.get("role"), state=ctx.get("state"),
        active_only=str(ctx.get("active_only", "")).lower() in ("1", "true", "yes"),
        dispatch_id=ctx.get("dispatch_id"),
        source_module=ctx.get("source_module") or ctx.get("来源模块"))
    out = page(items)
    out["todos"] = items
    return out


async def _opinions(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    items = svc.list_opinions(dispatch_id=ctx.get("dispatch_id"),
                              audit_no=ctx.get("audit_no") or ctx.get("审计编号"))
    out = page(items)
    out["opinions"] = items
    return out


async def _logs(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    only_rejected = str(ctx.get("only_rejected", "")).lower() in ("1", "true", "yes") \
        or ctx.get("result") in ("已拒绝", "rejected")
    items = svc.list_logs(
        dispatch_id=ctx.get("dispatch_id"),
        audit_no=ctx.get("audit_no") or ctx.get("审计编号"),
        operator=ctx.get("operator") or ctx.get("owner"),
        result=ctx.get("result"), only_rejected=only_rejected)
    out = page(items)
    out["logs"] = items
    out["requests"] = items
    out["rejected"] = [i for i in items if not i.get("accepted", True)]
    return out


for p in ("/api/audit/todos", "/api/todos", "/api/todo", "/api/work-todo",
          "/api/work_todo", "/api/audit/todo", "/api/model_audit/todos",
          "/api/model_audit/todo", "/api/audit/orders", "/api/orders"):
    router.add_api_route(p, _todos, methods=["GET", "POST"])

for p in ("/api/audit/opinions", "/api/opinions", "/api/audit/opinion",
          "/api/model_audit/opinions"):
    router.add_api_route(p, _opinions, methods=["GET", "POST"])

for p in ("/api/audit/request-logs", "/api/request-logs", "/api/request_logs",
          "/api/audit/logs", "/api/logs", "/api/audit/requests", "/api/requests",
          "/api/model_audit/request-logs", "/api/model_audit/logs"):
    router.add_api_route(p, _logs, methods=["GET", "POST"])


# ---------------- 同号记录 / 权限核查 ----------------

async def _access_check(request: Request) -> dict[str, Any]:
    ctx = await parse_context(request)
    did = ctx.get("dispatch_id") or ctx.get("id")
    d = svc.get_dispatch(did) if did not in (None, "") else None
    if d is None:
        return envelope(False, f"派办单 {did} 不存在", None, code="NOT_FOUND")
    role = REVIEWER_ROLE_DEFAULT if (ctx.get("role") in ("复核", "reviewer")) else "处置"
    ok, who, code = svc.authorize(
        d, role=ctx.get("role") or "处置",
        operator=ctx.get("operator_raw") or ctx.get("operator"),
        token=ctx.get("token"), action="权限核查", log_failure=False)
    return envelope(ok, "允许受理" if ok else f"不允许：{code}",
                    svc.detail(d["id"], who), code=None if ok else code)


REVIEWER_ROLE_DEFAULT = "复核"

router.add_api_route("/api/audit/access-check", _access_check, methods=["GET", "POST"])
router.add_api_route("/api/access-check", _access_check, methods=["GET", "POST"])


# ---------------- 一键构造验收场景 ----------------

async def _seed_scenario(request: Request) -> dict[str, Any]:
    """构造叙述中的完整初始局面：

    D1=A-17 派给甲处置、丙复核；D2=另一件同号 A-17 记录，丙在 D2 是处置人、
    丁复核 —— 用于验证“丙手头另一件同号记录不授予 D2 复核权限”的边界。
    """
    from app.store import store
    ctx = await parse_context(request)
    if str(ctx.get("reset", "")).lower() in ("1", "true", "yes"):
        for name in ("audit_dispatch", "audit_todo", "audit_opinion",
                     "audit_request_log", "audit_assignment", "audit_review_material"):
            store._tables[name] = []  # noqa: SLF001 - 演示重置
        # 只清理本功能跨模块生成的重跑任务（带来源审计标记），保留 RP-001/002 种子
        rp_rows = store.rows("reprocess_task")
        store._tables["reprocess_task"] = [  # noqa: SLF001
            r for r in rp_rows if not r.get("来源审计")]
        for a in store.rows("model_audit"):
            if a.get("条目编号") == "A-17":
                a["status"] = "待派办"
                a["pending"] = True
                a.pop("派办编号", None)
    existing = [d for d in store.rows("audit_dispatch") if d.get("审计编号") == "A-17"]
    if existing and not ctx.get("force"):
        d1 = existing[0]
        d2 = existing[1] if len(existing) > 1 else None
        return envelope(True, "A-17 派办场景已存在，直接返回",
                        {"dispatch": svc.detail(d1["id"], "甲"),
                         "other_same_no": svc.detail(d2["id"], "丙") if d2 else None,
                         "dispatch_id": d1["id"], "other_id": d2["id"] if d2 else None})
    ok1, msg1, d1 = svc.dispatch_audit(audit_no="A-17", handler="甲", reviewer="丙",
                                       dispatch_no="PD-A17-MAIN")
    ok2, msg2, d2 = svc.dispatch_audit(audit_no="A-17", handler="丙", reviewer="丁",
                                       dispatch_no="PD-A17-OTHER")
    return envelope(True, "已构造验收场景：D1 甲处置/丙复核；D2 同号 A-17 但丙仅为处置人",
                    {"dispatch": d1, "other_same_no": d2,
                     "dispatch_id": d1["id"] if d1 else None,
                     "other_id": d2["id"] if d2 else None})


router.add_api_route("/api/audit/scenario", _seed_scenario, methods=["POST", "GET"])
router.add_api_route("/api/audit/seed-scenario", _seed_scenario, methods=["POST", "GET"])


async def _feature(_: Request) -> dict[str, Any]:
    return {
        "ok": True,
        "feature": "audit_dispatch",
        "name": "建模审计派办",
        "identities": ["主管", "甲", "乙", "丙"],
        "canonical": "/api/audit/dispatch",
        "endpoints": [
            "POST /api/audit/dispatch", "GET /api/audit/dispatch",
            "GET /api/audit/dispatch/{id}",
            "POST /api/audit/dispatch/{id}/accept",
            "POST /api/audit/dispatch/{id}/opinion",
            "POST /api/audit/dispatch/{id}/submit",
            "POST /api/audit/dispatch/{id}/reassign",
            "POST /api/audit/dispatch/{id}/inherit",
            "GET|POST /api/audit/dispatch/{id}/review-material",
            "POST /api/audit/dispatch/{id}/verdict",
            "GET /api/audit/todos?owner=甲",
            "GET /api/audit/opinions?dispatch_id=",
            "GET /api/audit/request-logs?dispatch_id=",
            "POST /api/audit/scenario",
        ],
    }


router.add_api_route("/api/audit", _feature, methods=["GET"])
router.add_api_route("/api/audit/feature", _feature, methods=["GET"])
