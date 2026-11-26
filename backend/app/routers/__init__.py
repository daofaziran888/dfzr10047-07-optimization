"""业务路由汇总：专用工作流路由先注册，通用模块资源路由兜底。"""
from __future__ import annotations

from app.routers import probe_flow, resource

ROUTERS = [probe_flow, resource]
