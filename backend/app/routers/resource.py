"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
async def run_action(module: str, entry_id: int, request: Request,
                     payload: ActionPayload | None = None) -> ActionResult:
    service = require_service(module)
    # 兼容无 body / 非标准 body 的调用：动作名优先取 pydantic 载荷，
    # 其余身份、令牌、版本等上下文从原始 JSON、查询参数与请求头合并。
    raw: dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            raw = parsed
    except Exception:
        raw = {}
    if payload is None:
        try:
            payload = ActionPayload(**raw) if raw else ActionPayload(action="")
        except Exception:
            payload = ActionPayload(action=str(raw.get("action", "")) if raw else "")
    action = (payload.action or str(raw.get("action", ""))).strip()

    ctx: dict[str, Any] = dict(raw)
    ctx["remark"] = payload.remark if payload.remark is not None else raw.get("remark")
    ctx.update({k: v for k, v in request.query_params.items()})
    headers = {k.lower(): v for k, v in request.headers.items()}
    for hk, ck in (("x-operator", "operator"), ("x-user", "operator"),
                   ("x-as", "operator"), ("x-actor", "operator"),
                   ("x-assignment-token", "token")):
        if headers.get(hk) and not ctx.get(ck):
            ctx[ck] = headers[hk]

    context_action = getattr(service, "run_action_ctx", None)
    if callable(context_action):
        result = context_action(entry_id, action, ctx)
        # 允许返回 (entry, message) 或 (entry, message, extra)
        if len(result) == 3:
            entry, message, extra = result
            extra = extra or {}
            # 服务层可在 extra 中给出权威 ok（失败时 entry 也可能是详情字典）
            ok = bool(extra["ok"]) if isinstance(extra, dict) and "ok" in extra \
                else entry is not None
            return ActionResult(ok=ok, message=message, entry=entry)
        entry, message = result
        return ActionResult(ok=entry is not None, message=message, entry=entry)

    entry, message = service.run_action(entry_id, action)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
