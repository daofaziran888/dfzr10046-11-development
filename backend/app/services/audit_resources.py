"""审计派办配套表的通用只读/登记服务：待办、意见、请求留痕、地址资格、复核材料。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class AuditTodoService(BaseModuleService):
    MODULE = "audit_todo"
    ENTITY = "审计接单待办"
    LIST_FIELDS = ["待办编号", "派办编号", "审计编号", "owner", "归属人", "role", "角色",
                   "kind", "待办类型", "title", "source_module", "来源模块",
                   "biz_no", "业务编号", "state", "状态"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["待接单", "已接单", "处理中", "待材料", "待受理复核", "已完成", "已改派", "已失效"]
    ACTION_RULES: dict[str, str] = {}


class AuditOpinionService(BaseModuleService):
    MODULE = "audit_opinion"
    ENTITY = "审计处置意见"
    LIST_FIELDS = ["派办编号", "审计编号", "author", "作者", "content", "内容", "state", "状态", "version"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["草稿", "已送审", "已承继"]
    ACTION_RULES: dict[str, str] = {}


class AuditRequestLogService(BaseModuleService):
    MODULE = "audit_request_log"
    ENTITY = "审计请求留痕"
    LIST_FIELDS = ["时间", "派办编号", "审计编号", "操作人", "动作", "来源地址",
                   "结果", "code", "原因", "state", "状态"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["已受理", "已拒绝"]
    ACTION_RULES: dict[str, str] = {}


class AuditAssignmentService(BaseModuleService):
    MODULE = "audit_assignment"
    ENTITY = "审计接单地址资格"
    LIST_FIELDS = ["派办编号", "holder", "role", "generation", "token", "state", "address"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["有效", "已失效"]
    ACTION_RULES: dict[str, str] = {}


class AuditReviewMaterialService(BaseModuleService):
    MODULE = "audit_review_material"
    ENTITY = "审计复核材料"
    LIST_FIELDS = ["material_no", "派办编号", "审计编号", "opinion_author",
                   "submitted_by", "succession", "created_at"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["待复核", "已复核"]
    ACTION_RULES: dict[str, str] = {}
