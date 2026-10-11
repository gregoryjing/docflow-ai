"""审计事件：把「谁、在什么时候、对什么、做了什么」记成一条结构化流水.

审计与 RBAC 是一体两面：权限决定动作能不能发生，审计记录动作确实发生过。
两者都只在**改变状态**的动作上生效（处理、复核、导出、越权尝试），读操作不落流水。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from docflow.security.rbac import Principal


class AuditAction(StrEnum):
    PROCESS = "process"  # 触发一次文档处理
    REVIEW = "review"  # 人工复核
    EXPORT = "export"  # 导出结果
    ACCESS_DENIED = "access_denied"  # 权限不足被拦截


class AuditEvent(BaseModel):
    actor: str
    role: str
    action: str
    target: str = ""
    detail: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def of(
        cls,
        principal: Principal,
        action: AuditAction,
        target: str = "",
        detail: str = "",
    ) -> AuditEvent:
        return cls(actor=principal.name, role=principal.role.value, action=action.value, target=target, detail=detail)
