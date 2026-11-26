"""A-17 审计派办领域验收测试（服务层，不依赖 httpx/TestClient）。

覆盖：派办与跨模块重跑接单待办、同号他单权限隔离、改派/送审并发裁决、
旧地址拒绝留痕、接班资格（直接承继 / 确认后送审）、复核材料唯一性。
HTTP 契约由仓库外的自检脚本与隐藏验收测试覆盖。
"""
from __future__ import annotations

import threading

import pytest

from app.services.audit_dispatch import AuditDispatchService
from app.store import store


@pytest.fixture(autouse=True)
def _restore_store():
    """每个用例前后完整回滚内存仓库，避免本文件的派办数据污染同进程的其他验收测试。"""
    from copy import deepcopy
    snapshot = deepcopy(store._tables)
    yield
    store._tables = snapshot


def _dispatch(no: str = "PD-PY-1", handler: str = "甲", reviewer: str = "丙") -> dict:
    ok, msg, d = AuditDispatchService().dispatch_audit(
        audit_no="A-17", dispatch_no=no, handler=handler, reviewer=reviewer)
    assert ok, msg
    assert d is not None
    return d


def test_a17_seed_is_customer_backfill_anomaly():
    rows = store.rows("model_audit")
    assert any(str(r.get("条目编号")) == "A-17" and "回填异常" in str(r.get("操作类型"))
               for r in rows)


def test_dispatch_creates_cross_module_reprocess_todos():
    svc = AuditDispatchService()
    d = _dispatch("PD-PY-CROSS")
    did = d["id"]
    assert d["处置人"] == "甲" and d["复核人"] == "丙" and d["重跑编号"]
    todos = svc.list_todos(owner="甲", dispatch_id=did)
    kinds = {t["kind"] for t in todos}
    assert "审计处置" in kinds and "重跑接单" in kinds
    assert any(r.get("重跑编号") == d["重跑编号"] for r in store.rows("reprocess_task"))


def test_same_number_other_record_does_not_grant_review_access():
    svc = AuditDispatchService()
    d1 = _dispatch("PD-SAME-NO")
    d2 = _dispatch("PD-SAME-NO-2", handler="丙", reviewer="丁")

    ok1, _, mat1 = svc.review_material(d1["id"], operator="丙")
    assert ok1 is False and mat1 is not None  # 未送审 → NOT_SUBMITTED（资格本身通过）

    ok2, msg2, _ = svc.review_material(d2["id"], operator="丙")
    assert ok2 is False and ("边界" in msg2 or "复核人是 丁" in msg2)

    # 用 d1 的丙令牌打 d2：地址不属于本单
    ok3, msg3, _ = svc.review_material(d2["id"], operator="丙",
                                       token=d1["reviewer_token"])
    assert ok3 is False and "不属于本派办单" in msg3


def test_reassign_first_old_address_rejected_logged_and_confirm_required():
    svc = AuditDispatchService()
    d = _dispatch("PD-OLD-ADDR")
    did, token = d["id"], d["handler_token"]
    assert svc.accept_order(did, operator="甲", token=token)[0]
    ok, _, saved = svc.save_opinion(did, content="甲的原始意见", operator="甲")
    assert ok and saved is not None
    version = saved["version"]

    ok, msg, reas = svc.reassign(did, new_handler="乙", operator="主管",
                                 expected_version=version)
    assert ok and reas["succession"] == "确认后送审", msg

    # 甲持旧地址令牌再提交 → 拒绝
    ok_old, msg_old, _ = svc.save_opinion(
        did, content="甲改派后强改", operator="甲", token=token)
    assert ok_old is False and "失效" in msg_old

    # 已有意见不变
    opinions = svc.list_opinions(dispatch_id=did)
    assert len(opinions) == 1 and opinions[0]["content"] == "甲的原始意见"

    # 被拒请求单独留痕
    rejected = svc.list_logs(dispatch_id=did, only_rejected=True)
    assert any(l["code"] == "ADDRESS_CLOSED" for l in rejected)

    # 乙未确认直接送审被挡
    ok_need, msg_need, _ = svc.submit_for_review(did, operator="乙")
    assert ok_need is False and "须先" in msg_need

    # 确认承继并送审 → 成功，材料唯一
    ok_go, _, _ = svc.confirm_inherit(did, operator="乙", and_submit=True)
    assert ok_go
    mats = [m for m in store.rows("audit_review_material") if m["dispatch_id"] == did]
    assert len(mats) == 1


def test_submit_first_then_reassign_means_direct_inherit():
    svc = AuditDispatchService()
    d = _dispatch("PD-DIRECT")
    did = d["id"]
    svc.save_opinion(did, content="先送审意见", operator="甲")
    ok, _, sub = svc.submit_for_review(did, operator="甲")
    assert ok and sub is not None
    ok, _, reas = svc.reassign(did, new_handler="乙", operator="主管",
                               expected_version=sub["version"])
    assert ok and reas["succession"] == "直接承继"
    assert reas["viewer"]["can_submit_review"] is False
    ok_mat, _, mat = svc.review_material(did, operator="丙")
    assert ok_mat and mat and mat.get("material")


def test_concurrent_reassign_and_submit_one_winner_single_material():
    svc = AuditDispatchService()
    d = _dispatch("PD-RACE")
    did = d["id"]
    svc.save_opinion(did, content="并发意见", operator="甲")
    version = d["version"]
    outcomes: list[bool] = []

    def reassign():
        outcomes.append(svc.reassign(did, new_handler="乙", operator="主管",
                                     expected_version=version)[0])

    def submit():
        outcomes.append(svc.submit_for_review(did, operator="甲",
                                              expected_version=version)[0])

    t1, t2 = threading.Thread(target=reassign), threading.Thread(target=submit)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sum(1 for o in outcomes if o) == 1
    mats = [m for m in store.rows("audit_review_material") if m["dispatch_id"] == did]
    assert len(mats) <= 1


def test_generic_model_audit_action_creates_dispatch():
    from app.services.model_audit import ModelAuditService
    svc = ModelAuditService()
    entry, message, extra = svc.run_action_ctx(
        17, "审计派办", {"处置人": "甲", "复核人": "丙"})
    assert entry is not None and (extra or {}).get("ok")
    assert entry["复核人"] == "丙"


def test_http_action_aliases_and_failure_ok_flag():
    """HTTP 契约回归：专用 /actions 别名不 500，业务拒绝必须回 ok=false。"""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)

    d = c.post("/api/audit/dispatch", json={
        "audit_no": "A-17", "dispatch_no": "PD-HTTP-ALIAS"}).json()["entry"]
    did = d["id"]
    # 未改派前，乙不是处置人：专用 actions 别名应返回 200 + ok=false（不能 500/误报成功）
    resp = c.post(f"/api/audit/dispatch/{did}/actions",
                  json={"action": "暂存意见", "operator": "乙", "content": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["code"] in ("FORBIDDEN", "REJECTED")

    # 通用 model_audit 动作入口同样必须权威回传 ok=false
    resp2 = c.post("/api/model_audit/17/actions",
                   json={"action": "接单", "operator": "不存在的人_xyz",
                         "dispatch_id": did})
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is False
