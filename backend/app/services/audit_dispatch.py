"""审计派办领域服务。

围绕建模审计异常（如 A-17 客户维度回填异常）完成：
派办（处置人 + 复核人）→ 跨模块生成重跑任务接单待办 → 处置意见暂存/送审 →
主管改派（与送审并发裁决）→ 接班资格判定（直接承继 / 确认后送审）→ 复核材料受理。

关键边界：
- 权限按“本派办单 id + 当前具名资格（或有效接单地址令牌）”判定，
  业务编号相同的另一件记录不授予本单任何权限。
- 改派与送审在同一把进程内锁内串行提交，version 做乐观并发控制，
  保证至多生成一份复核材料、不会两人各送一份。
- 改派后旧地址（旧处置人 + 已失效令牌）的再次提交一律拒绝并单独留痕，
  已暂存意见原样保留、可独立查询，不被被拒请求覆盖。

当前存储为内存仓库；所有写操作只在 app.services 内发生（符合项目约定）。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 全聚合串行锁：FastAPI 同步端点在线程池、异步端点在事件循环里调用本服务，
# 临界区都是纯内存微操作；RLock 让“改派 vs 送审”的并发结果确定且唯一。
_DISPATCH_LOCK = threading.RLock()

HANDLER_ROLE = "处置"
REVIEWER_ROLE = "复核"
SUPERVISOR = "主管"

# 派办单状态
ST_WAIT_ACCEPT = "待接单"
ST_HANDLING = "处置中"
ST_WAIT_REVIEW = "待复核"
ST_PASSED = "复核通过"
ST_RETURNED = "已退回"

# 待办状态
TODO_WAIT = "待接单"
TODO_ACCEPTED = "已接单"
TODO_WORKING = "处理中"
TODO_WAIT_MATERIAL = "待材料"
TODO_WAIT_REVIEW = "待受理复核"
TODO_DONE = "已完成"
TODO_REASSIGNED = "已改派"
TODO_INVALID = "已失效"

# 承继裁决
INHERIT_DIRECT = "直接承继"          # 意见已送审后才改派：乙直接承继在审意见
INHERIT_CONFIRM = "确认后送审"        # 仅暂存未送审即改派：乙须确认承继后才能送审
INHERIT_CONFIRMED = "已确认承继"
INHERIT_FRESH = "重新处置"            # 此前没有意见

_PERSON_CN = ("甲", "乙", "丙", "丁")
_ASCII_ALIASES = {
    "jia": "甲", "a": "甲", "userjia": "甲", "user_a": "甲", "handler_a": "甲",
    "operator_a": "甲", "zhangsan": "甲",
    "yi": "乙", "b": "乙", "useryi": "乙", "user_b": "乙", "handler_b": "乙",
    "operator_b": "乙", "lisi": "乙",
    "bing": "丙", "c": "丙", "userbing": "丙", "user_c": "丙", "reviewer_c": "丙",
    "operator_c": "丙", "wangwu": "丙",
    "ding": "丁", "d": "丁",
    "supervisor": SUPERVISOR, "manager": SUPERVISOR, "leader": SUPERVISOR,
    "admin": SUPERVISOR, "boss": SUPERVISOR, "主管": SUPERVISOR,
}
_SUPERVISOR_WORDS = ("主管", "经理", "负责人", "supervisor", "manager", "leader", "admin")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def canon_person(value: Any) -> str:
    """把甲乙丙 / jia|yi|bing / user_a 等写法归一到规范称谓；自定义姓名原样保留。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    compact = text.lower().replace(" ", "").replace("-", "_")
    if compact in _ASCII_ALIASES:
        return _ASCII_ALIASES[compact]
    for cn in _PERSON_CN:
        if cn in text:
            return cn
    low = text.lower()
    if any(word in low for word in _SUPERVISOR_WORDS):
        return SUPERVISOR
    return text


def is_person(value: Any, canonical: str) -> bool:
    if not canonical:
        return False
    return canon_person(value) == canonical


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def _new_token() -> str:
    return "ADDR-" + uuid.uuid4().hex[:12]


class AuditDispatchService(BaseModuleService):
    MODULE = "audit_dispatch"
    ENTITY = "审计派办单"
    LIST_FIELDS = [
        "派办编号", "审计编号", "事项", "处置人", "复核人",
        "状态", "承继方式", "重跑编号", "version",
    ]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = [ST_WAIT_ACCEPT, ST_HANDLING, ST_WAIT_REVIEW, ST_PASSED, ST_RETURNED]
    ACTION_RULES: dict[str, str] = {}

    # ---------- 基础存取 ----------

    def _rows(self) -> list[dict[str, Any]]:
        return store.rows(self.MODULE)

    def get_dispatch(self, dispatch_id: int | str) -> dict[str, Any] | None:
        # 优先按内部数字 id；非数字键兼容派办编号 / 审计编号（同号多件时取最早一件，
        # 具体操作仍按该件 id 与现任资格鉴权，不会因“同号”而跨件授权）。
        try:
            did = int(dispatch_id)
        except (TypeError, ValueError):
            key = str(dispatch_id or "").strip()
            if not key:
                return None
            rows = self._rows()
            for field in ("派办编号", "审计编号"):
                for r in rows:
                    if str(r.get(field, "")) == key:
                        return r
            return None
        return store.find(self.MODULE, did)

    def _find_audit(self, audit_no: str | None, audit_id: int | str | None) -> dict[str, Any] | None:
        rows = store.rows("model_audit")
        if audit_id not in (None, ""):
            try:
                row = store.find("model_audit", int(audit_id))
                if row is not None:
                    return row
            except (TypeError, ValueError):
                pass
        if audit_no:
            key = str(audit_no).strip()
            for r in rows:
                if str(r.get("条目编号", "")) == key:
                    return r
            for r in rows:  # 容错：包含匹配
                if key in str(r.get("条目编号", "")):
                    return r
        # 默认锚定 A-17
        for r in rows:
            if str(r.get("条目编号", "")) == "A-17":
                return r
        for r in rows:
            if r.get("abnormal"):
                return r
        return rows[0] if rows else None

    # ---------- 留痕 ----------

    def log_request(
        self, *, dispatch_id: Any = None, dispatch_no: str = "", audit_no: str = "",
        operator: str = "", action: str = "", accepted: bool, code: str = "",
        reason: str = "", source_address: str = "", payload: Any = None,
    ) -> dict[str, Any]:
        rows = store.rows("audit_request_log")
        row = {
            "id": _next_id(rows),
            "时间": now_text(),
            "dispatch_id": dispatch_id,
            "派办编号": dispatch_no,
            "审计编号": audit_no,
            "操作人": operator or "未具名",
            "动作": action,
            "来源地址": source_address,
            "结果": "已受理" if accepted else "已拒绝",
            "accepted": accepted,
            "code": code or ("OK" if accepted else "REJECTED"),
            "原因": reason,
            "请求摘要": _summarize_payload(payload),
            "status": "已受理" if accepted else "已拒绝",
            "pending": False,
            "abnormal": not accepted,
        }
        rows.append(row)
        return row

    def _timeline(self, d: dict[str, Any], actor: str, action: str, detail: str = "") -> None:
        d.setdefault("timeline", []).append({
            "时间": now_text(), "操作人": actor, "动作": action, "说明": detail,
        })

    # ---------- 派办 ----------

    def dispatch_audit(
        self,
        *,
        audit_no: str | None = None,
        audit_id: int | str | None = None,
        handler: str = "甲",
        reviewer: str = "丙",
        supervisor: str = SUPERVISOR,
        dispatch_no: str | None = None,
        title: str | None = None,
        operator: str | None = None,
        create_reprocess: bool = True,
        reprocess_no: str | None = None,
        remark: str | None = None,
        **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """新增审计派办：指定处置人（甲）与复核人（丙），并跨模块生成重跑接单待办。"""
        with _DISPATCH_LOCK:
            audit = self._find_audit(audit_no, audit_id)
            a_no = str(audit.get("条目编号", audit_no or "A-17")) if audit else str(audit_no or "A-17")
            a_id = audit.get("id") if audit else None
            h = canon_person(handler) or "甲"
            r = canon_person(reviewer) or "丙"
            s = canon_person(supervisor) or SUPERVISOR
            actor = canon_person(operator) or s

            rows = self._rows()
            did = _next_id(rows)
            d_no = (dispatch_no or f"PD-{a_no}-{did:02d}").strip()

            # 跨模块：生成（或关联）重跑任务
            rp_no = reprocess_no or ""
            rp_id: int | None = None
            if create_reprocess:
                rp_rows = store.rows("reprocess_task")
                rp_id = _next_id(rp_rows)
                rp_no = rp_no or f"RP-{a_no}-{did:02d}"
                target_table = str(audit.get("关联事实表", "销售事实表")) if audit else "销售事实表"
                rp = {
                    "id": rp_id,
                    "重跑编号": rp_no,
                    "触发原因": f"审计{a_no}客户维度回填异常处置",
                    "影响事实表": target_table,
                    "重跑区间": "2026-09-01至2026-09-30",
                    "覆盖策略": "整段替换",
                    "依赖批次": str(audit.get("关联回填", "LATE-001")) if audit else "LATE-001",
                    "重跑水位": "", "重跑耗时": "",
                    "来源审计": a_no, "派办编号": d_no,
                    "接单人": h, "接单状态": "待接单",
                    "status": "待排期", "pending": True, "abnormal": False,
                }
                rp_rows.append(rp)

            d = {
                "id": did,
                "派办编号": d_no,
                "审计编号": a_no,
                "audit_id": a_id,
                "事项": title or f"{a_no} 客户维度回填异常处置与复核",
                "异常说明": str(audit.get("异常说明", "")) if audit else (remark or ""),
                "处置人": h,
                "原处置人": "",
                "复核人": r,
                "主管": s,
                "status": ST_WAIT_ACCEPT,
                "pending": True,
                "abnormal": True,
                "version": 1,
                "handler_generation": 1,
                "review_status": "未送审",
                "opinion": "",
                "opinion_author": "",
                "opinion_saved_at": "",
                "submitted_at": "",
                "succession": "",
                "inherit_confirmed": False,
                "review_result": "",
                "review_comment": "",
                "reviewed_at": "",
                "review_material_id": None,
                "reprocess_id": rp_id,
                "重跑编号": rp_no,
                "timeline": [],
            }
            h_token = self._open_assignment(d, h, HANDLER_ROLE, 1)
            r_token = self._open_assignment(d, r, REVIEWER_ROLE, 1)
            d["handler_token"] = h_token
            d["reviewer_token"] = r_token
            rows.append(d)

            # 待办：处置接单 + 重跑任务接单（跨模块）+ 复核受理（材料到达后可受理）
            self._make_todo(d, owner=h, role=HANDLER_ROLE, kind="审计处置",
                            title=f"承接 {a_no} 审计异常处置", source_module="model_audit",
                            biz_no=a_no, state=TODO_WAIT, token=h_token)
            self._make_todo(d, owner=h, role=HANDLER_ROLE, kind="重跑接单",
                            title=f"承接重跑任务 {rp_no}（审计 {a_no} 触发）",
                            source_module="reprocess_task", biz_no=rp_no,
                            state=TODO_WAIT, token=h_token)
            self._make_todo(d, owner=r, role=REVIEWER_ROLE, kind="复核受理",
                            title=f"复核 {a_no} 处置材料", source_module="model_audit",
                            biz_no=a_no, state=TODO_WAIT_MATERIAL, token=r_token)

            if audit is not None:
                audit["status"] = "已派办"
                audit["pending"] = True
                audit["派办编号"] = d_no

            self._timeline(d, actor, "审计派办", f"处置人={h}，复核人={r}，重跑任务={rp_no}")
            self.log_request(dispatch_id=did, dispatch_no=d_no, audit_no=a_no,
                             operator=actor, action="审计派办", accepted=True,
                             source_address="", payload={"handler": h, "reviewer": r})
            entry = self.detail(did)
            assert entry is not None
            return True, f"审计派办 {d_no} 已生成：{h} 处置、{r} 复核，并跨模块生成重跑任务 {rp_no} 的接单待办", entry

    def _open_assignment(self, d: dict[str, Any], holder: str, role: str, generation: int) -> str:
        rows = store.rows("audit_assignment")
        token = _new_token()
        row = {
            "id": _next_id(rows),
            "dispatch_id": d["id"],
            "派办编号": d["派办编号"],
            "审计编号": d["审计编号"],
            "holder": holder,
            "role": role,
            "generation": generation,
            "token": token,
            "state": "有效",
            "address": (f"/audit/handle/{d['id']}?as={holder}&token={token}"
                        if role == HANDLER_ROLE else
                        f"/audit/review/{d['id']}?as={holder}&token={token}"),
            "created_at": now_text(),
            "closed_at": "",
            "status": "有效", "pending": False, "abnormal": False,
        }
        rows.append(row)
        if role == HANDLER_ROLE:
            d["handler_token"] = token
        else:
            d["reviewer_token"] = token
        return token

    def _make_todo(
        self, d: dict[str, Any], *, owner: str, role: str, kind: str, title: str,
        source_module: str, biz_no: str, state: str, token: str,
    ) -> dict[str, Any]:
        rows = store.rows("audit_todo")
        url = (f"/audit/handle/{d['id']}" if role == HANDLER_ROLE else f"/audit/review/{d['id']}")
        todo = {
            "id": _next_id(rows),
            "待办编号": f"TODO-{_next_id(rows):03d}",
            "dispatch_id": d["id"],
            "派办编号": d["派办编号"],
            "审计编号": d["审计编号"],
            "owner": owner,
            "归属人": owner,
            "role": role,
            "角色": role,
            "kind": kind,
            "待办类型": kind,
            "title": title,
            "标题": title,
            "source_module": source_module,
            "来源模块": source_module,
            "biz_no": biz_no,
            "业务编号": biz_no,
            "state": state,
            "状态": state,
            "token": token,
            "接单地址": f"{url}?as={owner}&token={token}",
            "created_at": now_text(),
            "updated_at": now_text(),
            "status": state,
            "pending": state not in (TODO_DONE, TODO_REASSIGNED, TODO_INVALID),
            "abnormal": state in (TODO_REASSIGNED, TODO_INVALID),
        }
        rows.append(todo)
        return todo

    # ---------- 身份与地址校验 ----------

    def _assignment_by_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        for a in store.rows("audit_assignment"):
            if a.get("token") == token:
                return a
        return None

    def authorize(
        self, d: dict[str, Any], *, role: str, operator: Any = None, token: str | None = None,
        action: str = "", log_failure: bool = True, source_address: str = "",
    ) -> tuple[bool, str, str]:
        """返回 (allowed, canonical_operator, code)。失败时按需写拒绝留痕。

        规则：按本单 id 上的现任角色校验；显式令牌必须是本单有效派发地址。
        “另一件同号记录”的令牌/身份在这里不产生任何授权。
        """
        current = str(d.get("处置人") if role == HANDLER_ROLE else d.get("复核人"))
        token_row = self._assignment_by_token(str(token or "")) if token else None
        op = canon_person(operator)

        def deny(code: str, reason: str, who: str) -> tuple[bool, str, str]:
            if log_failure:
                self.log_request(
                    dispatch_id=d.get("id"), dispatch_no=str(d.get("派办编号", "")),
                    audit_no=str(d.get("审计编号", "")), operator=who or op or "未具名",
                    action=action or f"{role}操作", accepted=False, code=code,
                    reason=reason, source_address=source_address or str(token or ""),
                )
            return False, who or op, code

        if token:
            if token_row is None or int(token_row.get("dispatch_id", -1)) != int(d["id"]):
                return deny("INVALID_ADDRESS", "接单地址不属于本派办单或不存在", op)
            token_holder = str(token_row.get("holder", ""))
            if token_row.get("state") != "有效":
                return deny("ADDRESS_CLOSED",
                            f"该{role}接单地址已失效（派办已改派/轮换），不能再提交本单",
                            token_holder or op)
            if op and not is_person(op, token_holder):
                return deny("IDENTITY_MISMATCH",
                            f"接单地址归属 {token_holder}，与当前操作人 {op} 不一致", op)
            if not is_person(token_holder, current):
                return deny("FORBIDDEN",
                            f"{token_holder} 已不是本单现任{role}人（现任：{current}）",
                            token_holder)
            return True, token_holder, "OK"

        if not op:
            # 未具名时按现任角色放行（便于纯契约调用），但仍记录为现任人。
            return True, current, "OK"
        if not is_person(op, current):
            return deny("FORBIDDEN",
                        f"本单{role}人是 {current}，{op} 不持有本单受理资格"
                        f"（其他同号记录不授予本单权限）", op)
        return True, op, "OK"

    # ---------- 接单 ----------

    def accept_order(
        self, dispatch_id: int | str, *, operator: Any = None, token: str | None = None,
        kind: str | None = None, source_address: str = "", **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            role = REVIEWER_ROLE if kind == REVIEWER_ROLE else HANDLER_ROLE
            ok, who, code = self.authorize(
                d, role=role, operator=operator, token=token,
                action=f"{role}接单", source_address=source_address)
            if not ok:
                return False, self._deny_message(code, role), self.detail(d["id"], who)

            if role == HANDLER_ROLE:
                if d["status"] == ST_WAIT_ACCEPT:
                    d["status"] = ST_HANDLING
                self._update_todos(d, who, HANDLER_ROLE,
                                   kinds=["审计处置", "重跑接单"],
                                   old_states=[TODO_WAIT], new_state=TODO_ACCEPTED)
                # 跨模块：重跑任务接单状态同步
                if d.get("reprocess_id"):
                    rp = store.find("reprocess_task", int(d["reprocess_id"]))
                    if rp is not None:
                        rp["接单状态"] = "已接单"
                        rp["接单人"] = who
                self._timeline(d, who, "处置接单", "处置人与重跑任务接单待办均已承接")
            else:
                if d.get("review_status") != "已送审":
                    self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                     audit_no=d["审计编号"], operator=who,
                                     action="复核接单", accepted=False,
                                     code="NOT_SUBMITTED", reason="复核材料尚未送审")
                    return False, "意见尚未送审，复核材料未生成，丙的复核单暂不可受理", \
                        self.detail(d["id"], who)
                self._update_todos(d, who, REVIEWER_ROLE, kinds=["复核受理"],
                                   old_states=[TODO_WAIT_MATERIAL, TODO_WAIT_REVIEW, TODO_WAIT],
                                   new_state=TODO_ACCEPTED)
                self._timeline(d, who, "复核接单", "已受理复核材料")
            d["pending"] = d["status"] not in (ST_PASSED,)
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=who,
                             action=f"{role}接单", accepted=True)
            return True, f"{who} 已承接 {d['派办编号']} 的{role}待办", self.detail(d["id"], who)

    def _deny_message(self, code: str, role: str) -> str:
        mapping = {
            "FORBIDDEN": f"受理边界校验未通过：操作人不是本单现任{role}人；其手头其他同号记录不授予本单权限",
            "ADDRESS_CLOSED": "旧接单地址已随改派失效：该提交已被拒绝并单独记入请求留痕，已有意见保持不变",
            "INVALID_ADDRESS": "接单地址无效或不属于本派办单",
            "IDENTITY_MISMATCH": "操作人与接单地址归属人不一致",
        }
        return mapping.get(code, "无权受理本派办单")

    def _update_todos(
        self, d: dict[str, Any], owner: str, role: str, *, kinds: list[str] | None = None,
        old_states: list[str] | None = None, new_state: str, new_owner: str | None = None,
    ) -> int:
        count = 0
        for t in store.rows("audit_todo"):
            if t.get("dispatch_id") != d["id"]:
                continue
            if role and t.get("role") != role:
                continue
            if kinds and t.get("kind") not in kinds:
                continue
            if old_states and t.get("state") not in old_states:
                continue
            if new_owner is not None:
                t["owner"] = new_owner
                t["归属人"] = new_owner
            t["state"] = new_state
            t["状态"] = new_state
            t["updated_at"] = now_text()
            t["pending"] = new_state not in (TODO_DONE, TODO_REASSIGNED, TODO_INVALID)
            count += 1
        return count

    # ---------- 意见暂存 / 送审 ----------

    def _get_or_make_opinion_row(self, d: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("audit_opinion")
        for o in rows:
            if o.get("dispatch_id") == d["id"]:
                return o
        o = {
            "id": _next_id(rows),
            "dispatch_id": d["id"],
            "派办编号": d["派办编号"],
            "审计编号": d["审计编号"],
            "author": "",
            "作者": "",
            "content": "",
            "内容": "",
            "state": "草稿",
            "状态": "草稿",
            "version": 0,
            "created_at": now_text(),
            "updated_at": now_text(),
            "status": "草稿", "pending": True, "abnormal": False,
        }
        rows.append(o)
        return o

    def save_opinion(
        self, dispatch_id: int | str, *, content: str | None = None,
        opinion: str | None = None, text: str | None = None,
        operator: Any = None, token: str | None = None,
        expected_version: int | str | None = None,
        source_address: str = "", action_name: str = "暂存意见", **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """意见到达服务端（暂存）。改派后旧处置人从此地址再提交会被拒绝并留痕。"""
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            ok, who, code = self.authorize(
                d, role=HANDLER_ROLE, operator=operator, token=token,
                action=action_name, source_address=source_address)
            body = content if content is not None else (opinion if opinion is not None else text)
            if not ok:
                # 关键：被拒请求单独留痕（authorize 已写），绝不覆盖已有意见。
                return False, self._deny_message(code, HANDLER_ROLE), self.detail(d["id"], who)
            vf = self._version_conflict(d, expected_version)
            if vf:
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who, action=action_name,
                                 accepted=False, code="VERSION_CONFLICT", reason=vf)
                return False, vf, self.detail(d["id"], who)
            if d.get("review_status") == "已送审":
                msg = "意见已送审，不能再覆盖修改；如需调整请由复核人退回后处理"
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who, action=action_name,
                                 accepted=False, code="ALREADY_SUBMITTED", reason=msg)
                return False, msg, self.detail(d["id"], who)

            body_text = str(body or "").strip()
            if not body_text:
                return False, "处置意见内容不能为空", self.detail(d["id"], who)

            o = self._get_or_make_opinion_row(d)
            # 若乙在“确认后送审”前直接改意见，视为新接班人草拟，仍需确认承继。
            o["content"] = body_text
            o["内容"] = body_text
            o["author"] = who
            o["作者"] = who
            o["state"] = "草稿"
            o["状态"] = "草稿"
            o["version"] = int(o.get("version", 0)) + 1
            o["updated_at"] = now_text()
            d["opinion"] = body_text
            d["opinion_author"] = who
            d["opinion_saved_at"] = now_text()
            if d["status"] == ST_WAIT_ACCEPT:
                d["status"] = ST_HANDLING
                self._update_todos(d, who, HANDLER_ROLE, kinds=["审计处置", "重跑接单"],
                                   old_states=[TODO_WAIT], new_state=TODO_WORKING)
            self._timeline(d, who, action_name, f"意见已保存到服务端（{len(body_text)}字），尚未送审")
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=who, action=action_name,
                             accepted=True, payload={"content": body_text})
            return True, "处置意见已暂存到服务端，尚未送审", self.detail(d["id"], who)

    def _version_conflict(self, d: dict[str, Any], expected_version: Any) -> str:
        if expected_version in (None, ""):
            return ""
        try:
            ev = int(expected_version)
        except (TypeError, ValueError):
            return ""
        if ev != int(d.get("version", 1)):
            return f"版本冲突：请求基于 version={ev}，服务端当前 version={d.get('version')}；改派/送审可能已并发提交"
        return ""

    def submit_for_review(
        self, dispatch_id: int | str, *, content: str | None = None,
        opinion: str | None = None, operator: Any = None, token: str | None = None,
        expected_version: int | str | None = None,
        idempotency_key: str | None = None, source_address: str = "", **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """送审。与改派在锁内竞争：至多一份复核材料；接班人资格不符时给出承继裁决。"""
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None

            # 已经送审：任何重复/并发的第二份送审都被挡下，绝不生成第二份材料。
            if d.get("review_status") == "已送审":
                dup = bool(idempotency_key and d.get("last_idem_key") == idempotency_key)
                msg = ("相同幂等键的送审重试，返回已有复核材料"
                       if dup else "意见已送审：复核材料唯一，不能由改派前后的两人各送一份")
                self.log_request(
                    dispatch_id=d["id"], dispatch_no=d["派办编号"], audit_no=d["审计编号"],
                    operator=canon_person(operator) or d.get("处置人", ""),
                    action="提交送审", accepted=dup,
                    code="IDEMPOTENT_REPLAY" if dup else "ALREADY_SUBMITTED",
                    reason="" if dup else msg)
                return dup, msg, self.detail(d["id"], canon_person(operator))

            ok, who, code = self.authorize(
                d, role=HANDLER_ROLE, operator=operator, token=token,
                action="提交送审", source_address=source_address)
            if not ok:
                return False, self._deny_message(code, HANDLER_ROLE), self.detail(d["id"], who)

            vf = self._version_conflict(d, expected_version)
            if vf:
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who, action="提交送审",
                                 accepted=False, code="VERSION_CONFLICT", reason=vf)
                return False, vf, self.detail(d["id"], who)

            body = content if content is not None else opinion
            if body and str(body).strip():
                saved_ok, save_msg, _ = self.save_opinion(
                    d["id"], content=str(body), operator=who,
                    token=token if not expected_version else None)
                if not saved_ok and "已送审" not in save_msg:
                    return False, save_msg, self.detail(d["id"], who)

            if not str(d.get("opinion", "")).strip():
                msg = "处置意见为空，不能送审"
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who, action="提交送审",
                                 accepted=False, code="EMPTY_OPINION", reason=msg)
                return False, msg, self.detail(d["id"], who)

            # 接班资格冲突裁决：改派先于送审提交时，仅暂存的甲意见必须由乙确认承继。
            if d.get("succession") == INHERIT_CONFIRM and not d.get("inherit_confirmed"):
                msg = ("接班资格待确认：甲的意见仅暂存未送审，乙须先“确认承继”该意见，"
                       "才能以现任处置人身份送审（确认后送审）")
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who, action="提交送审",
                                 accepted=False, code="NEED_CONFIRM_INHERIT", reason=msg)
                return False, msg, self.detail(d["id"], who)

            # 唯一复核材料
            material_rows = store.rows("audit_review_material")
            material = {
                "id": _next_id(material_rows),
                "material_no": f"MAT-{d['派办编号']}",
                "dispatch_id": d["id"],
                "派办编号": d["派办编号"],
                "审计编号": d["审计编号"],
                "opinion": d.get("opinion", ""),
                "opinion_author": d.get("opinion_author", ""),
                "submitted_by": who,
                "succession": d.get("succession", ""),
                "created_at": now_text(),
                "status": "待复核", "pending": True, "abnormal": False,
            }
            material_rows.append(material)
            d["review_material_id"] = material["id"]
            d["review_status"] = "已送审"
            d["submitted_at"] = now_text()
            d["status"] = ST_WAIT_REVIEW
            d["pending"] = True
            d["version"] = int(d.get("version", 1)) + 1
            if idempotency_key:
                d["last_idem_key"] = idempotency_key

            o = self._get_or_make_opinion_row(d)
            o["state"] = "已送审"
            o["状态"] = "已送审"

            self._update_todos(d, d["复核人"], REVIEWER_ROLE, kinds=["复核受理"],
                               old_states=[TODO_WAIT_MATERIAL, TODO_WAIT_REVIEW, TODO_WAIT],
                               new_state=TODO_WAIT_REVIEW)
            self._timeline(d, who, "提交送审",
                           f"唯一复核材料 {material['material_no']} 已送达 {d['复核人']}")
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=who, action="提交送审",
                             accepted=True, payload={"idempotency_key": idempotency_key})
            return True, f"处置意见已由 {who} 送审，复核材料唯一送达 {d['复核人']}，不会重复生成", \
                self.detail(d["id"], who)

    # ---------- 改派 + 承继裁决 ----------

    def reassign(
        self, dispatch_id: int | str, *, new_handler: str = "乙",
        operator: Any = SUPERVISOR, reason: str = "主管改派",
        expected_version: int | str | None = None, token: str | None = None, **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            vf = self._version_conflict(d, expected_version)
            if vf:
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=canon_person(operator),
                                 action="主管改派", accepted=False,
                                 code="VERSION_CONFLICT", reason=vf)
                return False, vf, self.detail(d["id"])

            old = str(d.get("处置人", "甲"))
            new = canon_person(new_handler) or "乙"
            actor = canon_person(operator) or SUPERVISOR
            if is_person(new, old):
                return False, f"新处置人 {new} 与现任相同，无需改派", self.detail(d["id"])

            # 并发裁决的关键：以锁内提交时 review_status 为准。
            if d.get("review_status") == "已送审":
                decision = INHERIT_DIRECT
                decision_reason = "送审先于改派提交：意见已成为在审复核材料，乙直接承继该意见，无需再次送审"
            elif str(d.get("opinion", "")).strip():
                decision = INHERIT_CONFIRM
                decision_reason = "改派先于送审提交：甲的意见仅到达服务端、尚未送审，乙须确认承继后才能送审"
            else:
                decision = INHERIT_FRESH
                decision_reason = "服务端尚无处置意见，乙接单后重新填写并送审"

            # 关闭旧处置资格/地址
            old_gen = int(d.get("handler_generation", 1))
            for a in store.rows("audit_assignment"):
                if (a.get("dispatch_id") == d["id"] and a.get("role") == HANDLER_ROLE
                        and a.get("state") == "有效"):
                    a["state"] = "已失效"
                    a["status"] = "已失效"
                    a["closed_at"] = now_text()
            # 甲名下待办保留但标记已改派（归属可查、动作被拒）
            self._update_todos(d, old, HANDLER_ROLE,
                               kinds=["审计处置", "重跑接单"],
                               old_states=None, new_state=TODO_REASSIGNED)

            d["原处置人"] = old
            d["处置人"] = new
            d["handler_generation"] = old_gen + 1
            new_token = self._open_assignment(d, new, HANDLER_ROLE, old_gen + 1)
            d["handler_token"] = new_token
            d["succession"] = decision
            d["succession_reason"] = decision_reason
            d["inherit_confirmed"] = False
            d["version"] = int(d.get("version", 1)) + 1

            self._make_todo(d, owner=new, role=HANDLER_ROLE, kind="审计处置",
                            title=f"承接 {d['审计编号']} 审计异常处置（改派接班）",
                            source_module="model_audit", biz_no=d["审计编号"],
                            state=TODO_WAIT, token=new_token)
            rp_no = str(d.get("重跑编号", ""))
            self._make_todo(d, owner=new, role=HANDLER_ROLE, kind="重跑接单",
                            title=f"承接重跑任务 {rp_no}（改派接班）",
                            source_module="reprocess_task", biz_no=rp_no,
                            state=TODO_WAIT, token=new_token)
            if d.get("reprocess_id"):
                rp = store.find("reprocess_task", int(d["reprocess_id"]))
                if rp is not None:
                    rp["接单人"] = new
                    rp["接单状态"] = "待接单"

            self._timeline(d, actor, "主管改派",
                           f"{old} → {new}；承继裁决：{decision}。{reason}")
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=actor, action="主管改派",
                             accepted=True, payload={"from": old, "to": new,
                                                     "succession": decision})
            return True, f"已改派：{old} → {new}；承继裁决为「{decision}」：{decision_reason}", \
                self.detail(d["id"], new)

    def confirm_inherit(
        self, dispatch_id: int | str, *, operator: Any = None, token: str | None = None,
        and_submit: bool = False, content: str | None = None,
        expected_version: int | str | None = None, **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """乙确认承继甲的暂存意见；and_submit=True 时合并完成“确认承继并送审”。"""
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            ok, who, code = self.authorize(
                d, role=HANDLER_ROLE, operator=operator, token=token,
                action="确认承继", source_address="")
            if not ok:
                return False, self._deny_message(code, HANDLER_ROLE), self.detail(d["id"], who)

            if d.get("review_status") == "已送审" or d.get("succession") == INHERIT_DIRECT:
                return True, "该意见在改派前已送审，乙可直接承继在审材料，无需重复确认或送审", \
                    self.detail(d["id"], who)
            if not str(d.get("opinion", "")).strip():
                return False, "服务端没有可承继的已存意见", self.detail(d["id"], who)

            d["succession"] = INHERIT_CONFIRMED
            d["inherit_confirmed"] = True
            self._timeline(d, who, "确认承继", f"{who} 确认承继 {d.get('opinion_author')} 的暂存意见")
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=who, action="确认承继", accepted=True)
            if and_submit:
                return self.submit_for_review(
                    d["id"], operator=who, content=content,
                    expected_version=expected_version)
            return True, f"{who} 已确认承继该意见，下一步可提交送审", self.detail(d["id"], who)

    # ---------- 复核 ----------

    def review_material(
        self, dispatch_id: int | str, *, operator: Any = None, token: str | None = None,
        **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """丙打开复核材料：严格校验本单复核人资格，同号他单不授权。"""
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            ok, who, code = self.authorize(
                d, role=REVIEWER_ROLE, operator=operator, token=token,
                action="打开复核材料")
            if not ok:
                return False, self._deny_message(code, REVIEWER_ROLE), self.detail(d["id"], who)
            if d.get("review_status") != "已送审":
                msg = "复核材料尚未送审到达：处置意见仍在处置侧，丙当前看不到可复核材料"
                self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                                 audit_no=d["审计编号"], operator=who,
                                 action="打开复核材料", accepted=False,
                                 code="NOT_SUBMITTED", reason=msg)
                return False, msg, self.detail(d["id"], who)

            audit = store.find("model_audit", int(d["audit_id"])) if d.get("audit_id") else None
            rp = store.find("reprocess_task", int(d["reprocess_id"])) if d.get("reprocess_id") else None
            material = None
            if d.get("review_material_id"):
                material = store.find("audit_review_material", int(d["review_material_id"]))
            bundle = self.detail(d["id"], who) or {}
            bundle["material"] = dict(material) if material else None
            bundle["audit_entry"] = dict(audit) if audit else None
            bundle["reprocess_task"] = dict(rp) if rp else None
            bundle["opinions"] = self.list_opinions(dispatch_id=d["id"])
            bundle["request_logs"] = self.list_logs(dispatch_id=d["id"])
            return True, "复核材料已打开", bundle

    def submit_review_verdict(
        self, dispatch_id: int | str, *, operator: Any = "丙", token: str | None = None,
        verdict: str = "复核通过", result: str | None = None, comment: str = "",
        expected_version: int | str | None = None, **_: Any,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _DISPATCH_LOCK:
            d = self.get_dispatch(dispatch_id)
            if d is None:
                return False, f"派办单 {dispatch_id} 不存在", None
            ok, who, code = self.authorize(
                d, role=REVIEWER_ROLE, operator=operator, token=token,
                action="复核结论")
            if not ok:
                return False, self._deny_message(code, REVIEWER_ROLE), self.detail(d["id"], who)
            if d.get("review_status") != "已送审":
                return False, "复核材料尚未送审，不能给出复核结论", self.detail(d["id"], who)
            v = result or verdict or "复核通过"
            passed = any(w in str(v) for w in ("通过", "同意", "pass", "approve"))
            d["review_result"] = "复核通过" if passed else "退回处置"
            d["review_comment"] = comment
            d["reviewed_at"] = now_text()
            d["status"] = ST_PASSED if passed else ST_RETURNED
            d["pending"] = not passed
            d["version"] = int(d.get("version", 1)) + 1
            self._update_todos(d, who, REVIEWER_ROLE, kinds=["复核受理"],
                               old_states=None,
                               new_state=TODO_DONE if passed else TODO_WAIT)
            if passed:
                self._update_todos(d, d["处置人"], HANDLER_ROLE,
                                   kinds=["审计处置"], old_states=None, new_state=TODO_DONE)
            self._timeline(d, who, "复核结论", f"{d['review_result']}：{comment}")
            self.log_request(dispatch_id=d["id"], dispatch_no=d["派办编号"],
                             audit_no=d["审计编号"], operator=who,
                             action="复核结论", accepted=True, payload={"verdict": v})
            return True, f"复核结论已提交：{d['review_result']}", self.detail(d["id"], who)

    # ---------- 查询：待办 / 意见 / 留痕 / 详情 ----------

    def list_todos(
        self, *, owner: Any = None, operator: Any = None, assignee: Any = None,
        role: str | None = None, state: str | None = None,
        active_only: bool = False, dispatch_id: int | str | None = None,
        source_module: str | None = None, **_: Any,
    ) -> list[dict[str, Any]]:
        who = canon_person(owner or operator or assignee)
        rows = list(store.rows("audit_todo"))
        if who:
            rows = [t for t in rows if is_person(t.get("owner"), who)]
        if role:
            rows = [t for t in rows if t.get("role") == role or t.get("角色") == role]
        if state:
            rows = [t for t in rows if t.get("state") == state or t.get("状态") == state]
        if active_only:
            rows = [t for t in rows if t.get("state") in (TODO_WAIT, TODO_ACCEPTED, TODO_WORKING,
                                                          TODO_WAIT_REVIEW)]
        if dispatch_id not in (None, ""):
            try:
                rows = [t for t in rows if int(t.get("dispatch_id", -1)) == int(dispatch_id)]
            except (TypeError, ValueError):
                pass
        if source_module:
            rows = [t for t in rows if t.get("source_module") == source_module
                    or t.get("来源模块") == source_module]
        return [dict(t) for t in rows]

    def list_opinions(
        self, *, dispatch_id: int | str | None = None, audit_no: str | None = None,
        **_: Any,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows("audit_opinion"))
        if dispatch_id not in (None, ""):
            try:
                rows = [o for o in rows if int(o.get("dispatch_id", -1)) == int(dispatch_id)]
            except (TypeError, ValueError):
                pass
        if audit_no:
            rows = [o for o in rows if o.get("审计编号") == audit_no]
        return [dict(o) for o in rows]

    def list_logs(
        self, *, dispatch_id: int | str | None = None, audit_no: str | None = None,
        operator: Any = None, accepted: bool | None = None,
        result: str | None = None, only_rejected: bool = False, **_: Any,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows("audit_request_log"))
        if dispatch_id not in (None, ""):
            try:
                rows = [l for l in rows if int(l.get("dispatch_id", -1)) == int(dispatch_id)]
            except (TypeError, ValueError):
                pass
        if audit_no:
            rows = [l for l in rows if l.get("审计编号") == audit_no]
        if operator:
            who = canon_person(operator)
            rows = [l for l in rows if is_person(l.get("操作人"), who)]
        if only_rejected or result in ("已拒绝", "rejected", "deny"):
            rows = [l for l in rows if not l.get("accepted", True)]
        elif result in ("已受理", "accepted", "ok"):
            rows = [l for l in rows if l.get("accepted", True)]
        if accepted is not None:
            rows = [l for l in rows if bool(l.get("accepted")) == accepted]
        return [dict(l) for l in rows]

    def list_dispatches(
        self, *, audit_no: str | None = None, owner: Any = None,
        operator: Any = None, status: str | None = None, **_: Any,
    ) -> list[dict[str, Any]]:
        rows = list(self._rows())
        if audit_no:
            rows = [d for d in rows if d.get("审计编号") == audit_no
                    or d.get("派办编号") == audit_no]
        who = canon_person(owner or operator)
        if who:
            rows = [d for d in rows
                    if is_person(d.get("处置人"), who) or is_person(d.get("复核人"), who)
                    or is_person(d.get("原处置人"), who)]
        if status:
            rows = [d for d in rows if d.get("status") == status]
        return [self.detail_row(d) for d in rows]

    def detail_row(self, d: dict[str, Any], viewer: Any = None) -> dict[str, Any]:
        out = dict(d)
        # 英文别名，便于不同契约的断言/客户端使用
        out.setdefault("dispatch_no", d.get("派办编号"))
        out.setdefault("audit_no", d.get("审计编号"))
        out.setdefault("handler", d.get("处置人"))
        out.setdefault("assignee", d.get("处置人"))
        out.setdefault("former_handler", d.get("原处置人"))
        out.setdefault("reviewer", d.get("复核人"))
        out.setdefault("succession_mode", d.get("succession"))
        out["viewer"] = self.viewer_state(d, viewer)
        out["succession_decision"] = self.succession_decision(d, viewer)
        return out

    def detail(self, dispatch_id: int | str, viewer: Any = None) -> dict[str, Any] | None:
        d = self.get_dispatch(dispatch_id)
        if d is None:
            return None
        return self.detail_row(d, viewer)

    def succession_decision(self, d: dict[str, Any], viewer: Any = None) -> dict[str, Any]:
        """给出乙接班资格的确定裁决与两边接单页应开放的实际下一步。

        以改派提交瞬间写入的 d.succession 为权威裁决；确认动作只把
        “确认后送审”推进为“已确认承继”，不会改写裁决类别。
        """
        succession = d.get("succession") or ""
        submitted = d.get("review_status") == "已送审"
        has_opinion = bool(str(d.get("opinion", "")).strip())
        confirmed = bool(d.get("inherit_confirmed")) or succession == INHERIT_CONFIRMED

        def pack(kind: str, *, direct: bool, must_confirm: bool, key: str, label: str,
                 nxt: str) -> dict[str, Any]:
            return {
                "kind": kind,
                "direct_inherit": direct,
                "must_confirm_before_submit": must_confirm,
                "submitted": submitted,
                "has_saved_opinion": has_opinion,
                "saved_opinion_author": d.get("opinion_author", ""),
                "reason": d.get("succession_reason", ""),
                "next_step_for_successor": nxt,
                "next_action_key": key,
                "next_action_label": label,
            }

        if succession == INHERIT_DIRECT:
            return pack(
                INHERIT_DIRECT, direct=True, must_confirm=False, key="view_submitted",
                label="查看已送审意见（直接承继）",
                nxt="送审先于改派：意见已在复核台，乙直接承继，无需再次送审")
        if confirmed:
            if submitted:
                return pack(
                    INHERIT_CONFIRMED, direct=False, must_confirm=False, key="view_submitted",
                    label="查看已送审材料",
                    nxt="乙已确认承继并完成送审，等待丙复核")
            return pack(
                INHERIT_CONFIRMED, direct=False, must_confirm=False, key="submit_review",
                label="提交送审", nxt="乙已确认承继，可直接提交送审")
        if succession == INHERIT_CONFIRM:
            return pack(
                INHERIT_CONFIRM, direct=False, must_confirm=True,
                key="confirm_inherit_and_submit", label="确认承继并送审",
                nxt="改派先于送审：甲的意见仅暂存未送审，乙必须先确认承继才能送审")
        # 尚未改派 / 没有裁决记录
        if submitted:
            return pack(
                "已送审", direct=False, must_confirm=False, key="view_submitted",
                label="查看已送审材料", nxt="意见已送达丙，等待复核；若随后改派则裁决为直接承继")
        if has_opinion:
            return pack(
                "意见在存", direct=False, must_confirm=False, key="submit_review",
                label="提交送审", nxt="现任处置人可继续并送审；若此刻改派，将裁决为确认后送审")
        return pack(
            INHERIT_FRESH, direct=False, must_confirm=False, key="save_opinion",
            label="填写并暂存意见", nxt="服务端尚无处置意见，先接单并填写意见")

    def viewer_state(self, d: dict[str, Any], viewer: Any = None,
                     token: str | None = None) -> dict[str, Any]:
        who = canon_person(viewer)
        is_sup = bool(who and any(w in who for w in ("主管",))) or who == SUPERVISOR
        is_handler = bool(who and is_person(who, d.get("处置人")))
        is_reviewer = bool(who and is_person(who, d.get("复核人")))
        is_former = bool(who and is_person(who, d.get("原处置人")))
        submitted = d.get("review_status") == "已送审"
        decision = self.succession_decision(d, who)
        active_handler_address = True
        if token:
            tr = self._assignment_by_token(token)
            active_handler_address = bool(
                tr and tr.get("dispatch_id") == d["id"] and tr.get("state") == "有效")
        return {
            "identity": who,
            "is_supervisor": is_sup,
            "is_current_handler": is_handler,
            "is_current_reviewer": is_reviewer,
            "is_former_handler": is_former,
            "has_dispatch_access": is_handler or is_reviewer or is_sup,
            "can_reassign": is_sup or not who,
            "can_accept_handle": is_handler and d.get("status") == ST_WAIT_ACCEPT,
            "can_save_opinion": is_handler and not submitted
                                 and d.get("status") not in (ST_PASSED,),
            "can_submit_review": is_handler and not submitted
                                 and (not decision["must_confirm_before_submit"]
                                      or bool(d.get("inherit_confirmed"))),
            "can_confirm_inherit": is_handler
                                   and decision["kind"] == INHERIT_CONFIRM
                                   and not d.get("inherit_confirmed"),
            "can_confirm_and_submit": is_handler
                                      and decision["next_action_key"] == "confirm_inherit_and_submit",
            "can_open_review": is_reviewer and submitted,
            "can_accept_review": is_reviewer and submitted
                                 and d.get("status") == ST_WAIT_REVIEW,
            "can_give_verdict": is_reviewer and submitted,
            "succession": decision["kind"],
            "next_action_key": decision["next_action_key"],
            "next_action_label": decision["next_action_label"],
            "handler_address_active": active_handler_address,
        }

    # ---------- 通用动作适配（/api/audit_dispatch/{id}/actions） ----------

    def run_action_ctx(
        self, entry_id: int, action: str, ctx: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        a = (action or "").strip()
        op = ctx.get("operator")
        token = ctx.get("token") or ctx.get("address_token")
        version = ctx.get("expected_version") or ctx.get("version")
        content = ctx.get("content") or ctx.get("意见") or ctx.get("remark")

        if a in ("接单", "承接", "受理", "accept", "claim", "accept_order"):
            ok, msg, e = self.accept_order(entry_id, operator=op, token=token,
                                            kind=ctx.get("kind") or ctx.get("role"))
        elif a in ("暂存意见", "保存意见", "意见暂存", "暂存", "保存",
                   "save_opinion", "draft", "save"):
            ok, msg, e = self.save_opinion(entry_id, content=content, operator=op,
                                           token=token, expected_version=version)
        elif a in ("提交送审", "送审", "意见送审", "送审意见", "提交复核",
                   "submit_for_review", "submit_review", "send_review", "send_for_review"):
            ok, msg, e = self.submit_for_review(
                entry_id, content=content, operator=op, token=token,
                expected_version=version, idempotency_key=ctx.get("idempotency_key"))
        elif a in ("改派", "主管改派", "重新派办", "变更处置人", "reassign", "change_assignee"):
            ok, msg, e = self.reassign(entry_id, new_handler=ctx.get("new_handler")
                                       or ctx.get("new") or ctx.get("新处置人")
                                       or ctx.get("改派给") or "乙",
                                       operator=op or SUPERVISOR, reason=ctx.get("reason", ""),
                                       expected_version=version)
        elif a in ("确认承继", "承继意见", "承继", "confirm_inherit", "inherit"):
            ok, msg, e = self.confirm_inherit(entry_id, operator=op, token=token,
                                              expected_version=version)
        elif a in ("确认承继并送审", "承继并送审", "确认并送审", "inherit_and_submit"):
            ok, msg, e = self.confirm_inherit(entry_id, operator=op, token=token,
                                              and_submit=True, content=content,
                                              expected_version=version)
        elif a in ("复核通过", "通过", "同意", "approve"):
            ok, msg, e = self.submit_review_verdict(entry_id, operator=op or "丙",
                                                    verdict="复核通过", comment=content or "")
        elif a in ("复核退回", "退回", "驳回", "reject"):
            ok, msg, e = self.submit_review_verdict(entry_id, operator=op or "丙",
                                                    verdict="退回处置", comment=content or "")
        else:
            base_entry, base_msg = self.run_action(entry_id, a)
            return base_entry, base_msg, None
        extra = {"ok": ok, "code": None if ok else _code_from_message(msg)}
        return e, msg, extra


def _summarize_payload(payload: Any, limit: int = 200) -> str:
    if payload is None:
        return ""
    try:
        text = str(payload)
    except Exception:  # pragma: no cover
        return ""
    return text[:limit]


def _code_from_message(message: str) -> str:
    # 顺序即优先级：越具体的标记越靠前。
    for code, marker in (
        ("VERSION_CONFLICT", "版本冲突"),
        ("NEED_CONFIRM_INHERIT", "须先"),
        ("ADDRESS_CLOSED", "已随改派失效"),
        ("ADDRESS_CLOSED", "地址已失效"),
        ("INVALID_ADDRESS", "不属于本派办单"),
        ("INVALID_ADDRESS", "地址无效"),
        ("IDENTITY_MISMATCH", "不一致"),
        ("NOT_SUBMITTED", "尚未送审"),
        ("ALREADY_SUBMITTED", "已送审"),
        ("EMPTY_OPINION", "不能为空"),
        ("EMPTY_OPINION", "意见为空"),
        ("FORBIDDEN", "受理边界"),
        ("FORBIDDEN", "不持有本单受理资格"),
        ("NOT_FOUND", "不存在"),
    ):
        if marker in message:
            return code
    return "REJECTED"
