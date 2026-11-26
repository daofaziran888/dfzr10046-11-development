"""业务路由汇总：审计派办专用路由在前，通用资源路由在后。"""
from __future__ import annotations

from app.routers import audit, resource

ROUTERS = [audit, resource]
