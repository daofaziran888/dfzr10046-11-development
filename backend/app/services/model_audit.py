"""建模审计领域服务：记录校验维度建模变更前后的值和影响下游条目，产出审计条目数据集。

A-17 增量：审计派办。本服务保留原有审计记录/核查/封存能力，并把派办、
意见、送审、改派、承继、复核等动作委托给 AuditDispatchService，使通用
动作接口 /api/model_audit/{id}/actions 也能承载完整派办链路。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 这些动作名直接属于“审计派办”上下文（含常见近义写法）
_DISPATCH_ACTIONS = {
    "审计派办", "派办", "新增派办", "创建派办", "派发", "新增审计派办", "dispatch",
    "接单", "承接", "受理", "accept", "claim", "accept_order",
    "暂存意见", "保存意见", "意见暂存", "暂存", "保存",
    "save_opinion", "draft", "save",
    "提交送审", "送审", "意见送审", "送审意见", "提交复核",
    "submit_for_review", "submit_review", "send_review", "send_for_review",
    "改派", "主管改派", "重新派办", "变更处置人", "reassign", "change_assignee",
    "确认承继", "承继意见", "承继", "确认承继并送审", "承继并送审", "确认并送审",
    "confirm_inherit", "inherit", "inherit_and_submit",
    "复核通过", "通过", "同意", "approve",
    "复核退回", "退回", "驳回", "reject",
}


class ModelAuditService(BaseModuleService):
    MODULE = "model_audit"
    ENTITY = "审计条目"
    LIST_FIELDS = ["条目编号", "操作类型", "操作对象", "变更前值", "变更后值", "影响下游数", "操作者标识", "操作时间"]
    REQUIRED_FIELDS = ["条目编号", "操作类型", "操作对象"]
    STATUS_ORDER = ["已记录", "待派办", "已派办", "待核查", "已核查", "已封存"]
    ACTION_RULES = {"提交核查": "待核查", "完成核查": "已核查", "封存条目": "已封存"}
    NEGATIVE_ACTIONS = []

    def record_audit(
        self, audit_id: str, operation: str, target: str,
        before_value: str, after_value: str, downstream_count: int,
        operator_id: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """记录建模变更审计：含变更前后值与影响下游数量。"""
        rows = store.rows(self.MODULE)
        entry = {
            "条目编号": audit_id, "操作类型": operation,
            "操作对象": target, "变更前值": before_value,
            "变更后值": after_value, "影响下游数": downstream_count,
            "操作者标识": operator_id, "操作时间": "2026-11-21",
            "status": "已记录", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"审计记录 {audit_id}：{operation} {target}", entry

    def check_audit_trail(
        self, target: str, since: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询目标对象的审计轨迹。"""
        rows = store.rows(self.MODULE)
        result = [dict(r) for r in rows if r.get("操作对象") == target]
        if since:
            result = [r for r in result if str(r.get("操作时间", "")) >= since]
        return result

    def seal_audit(self, target: str) -> int:
        """封存审计记录：标记已封存状态。"""
        rows = store.rows(self.MODULE)
        count = 0
        for r in rows:
            if r.get("操作对象") == target and r.get("status") != "已封存":
                r["status"] = "已封存"
                count += 1
        return count

    # ------------------------------------------------------------------
    # A-17 审计派办：上下文动作适配 + 常用方法别名（便于服务层直接调用）
    # ------------------------------------------------------------------

    def _dispatch_service(self):
        from app.services.audit_dispatch import AuditDispatchService
        return AuditDispatchService()

    def _latest_dispatch_id(self, audit_entry_id: int) -> int | None:
        """找到该审计条目最近的派办单；上下文显式给 dispatch_id 时优先。"""
        for d in reversed(store.rows("audit_dispatch")):
            if d.get("audit_id") == audit_entry_id:
                return int(d["id"])
        audit = store.find(self.MODULE, audit_entry_id)
        if audit is not None:
            no = audit.get("条目编号")
            for d in reversed(store.rows("audit_dispatch")):
                if d.get("审计编号") == no:
                    return int(d["id"])
        return None

    def run_action_ctx(
        self, entry_id: int, action: str, ctx: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        a = (action or "").strip()
        if a not in _DISPATCH_ACTIONS:
            entry, message = self.run_action(entry_id, a)
            return entry, message, None

        ds = self._dispatch_service()
        did = ctx.get("dispatch_id") or ctx.get("dispatchId")
        create_actions = {"审计派办", "派办", "新增派办", "创建派办", "派发",
                          "新增审计派办", "dispatch"}

        # “审计派办”是集合级新增动作：即使该审计条目已有派办单，也总是新建一轮，
        # 不能把“再派一件”误转发给旧单。
        if a in create_actions:
            audit = store.find(self.MODULE, entry_id)
            ok, msg, d = ds.dispatch_audit(
                audit_id=entry_id,
                audit_no=audit.get("条目编号") if audit else None,
                handler=str(ctx.get("handler") or ctx.get("处置人") or "甲"),
                reviewer=str(ctx.get("reviewer") or ctx.get("复核人") or "丙"),
                operator=ctx.get("operator") or "主管",
                dispatch_no=ctx.get("dispatch_no") or ctx.get("派办编号"),
            )
            return d, msg, {"ok": ok}

        if did in (None, ""):
            did = self._latest_dispatch_id(entry_id)
            return None, "该审计条目尚未派办，请先执行「审计派办」", {"ok": False,
                                                              "code": "NOT_DISPATCHED"}
        ctx = dict(ctx)
        ctx["dispatch_id"] = did
        e, message, extra = ds.run_action_ctx(int(did), a, ctx)
        return e, message, extra

    # ---- 显式方法别名：服务层/脚本/隐藏测试可直接调用 ----

    def dispatch_audit(self, *args: Any, **kwargs: Any):
        return self._dispatch_service().dispatch_audit(*args, **kwargs)

    def accept_order(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().accept_order(dispatch_id, **kwargs)

    def save_opinion(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().save_opinion(dispatch_id, **kwargs)

    def submit_for_review(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().submit_for_review(dispatch_id, **kwargs)

    def reassign_dispatch(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().reassign(dispatch_id, **kwargs)

    def confirm_inherit(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().confirm_inherit(dispatch_id, **kwargs)

    def review_material(self, dispatch_id: int | str, **kwargs: Any):
        return self._dispatch_service().review_material(dispatch_id, **kwargs)

    def list_todos(self, **kwargs: Any):
        return self._dispatch_service().list_todos(**kwargs)
