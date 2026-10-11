"""轻量 RBAC：角色 → 权限矩阵 + 身份解析.

定位：把「谁能做什么」从业务代码里抽出来，作为交付层的访问控制。
刻意不引入 JWT / OAuth 依赖——身份来源保持可插拔：
- 默认从请求头 `X-DocFlow-User` / `X-DocFlow-Role` 读取（内网反向代理注入的常见形态）；
- 关闭 RBAC 时一律视为 admin，保持向后兼容（单机 CLI / 本地演示不受影响）。

本模块刻意不依赖 `docflow.config`，以免 `config → security → config` 循环导入；
`resolve_principal` 只接收原始参数，由调用方从 Settings 取值。
"""

from __future__ import annotations

import hmac
import json
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, Field

from docflow.errors import PermissionDenied


class Role(StrEnum):
    """角色，权限从低到高."""

    VIEWER = "viewer"  # 只读：看运行结果与复核队列
    REVIEWER = "reviewer"  # 只读 + 人工复核
    OPERATOR = "operator"  # 复核 + 触发处理 + 导出
    ADMIN = "admin"  # 全部，含权限与审计管理


class Permission(StrEnum):
    """一项可授予的权限."""

    VIEW = "view"
    REVIEW = "review"
    PROCESS = "process"
    EXPORT = "export"
    ADMIN = "admin"


# 角色 → 权限矩阵。显式写全，避免「隐式继承」导致的越权。
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.VIEW}),
    Role.REVIEWER: frozenset({Permission.VIEW, Permission.REVIEW}),
    Role.OPERATOR: frozenset({Permission.VIEW, Permission.REVIEW, Permission.PROCESS, Permission.EXPORT}),
    Role.ADMIN: frozenset(Permission),
}


class Principal(BaseModel):
    """一次请求背后的操作者：身份名 + 角色，权限由角色推导."""

    name: str = Field(default="anonymous")
    role: Role = Field(default=Role.ADMIN)

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.can(permission):
            raise PermissionDenied(f"角色「{self.role.value}」无权执行「{permission.value}」操作")


def parse_role(value: str | None, fallback: Role) -> Role:
    """宽松解析角色名：无法识别时退回默认角色，不因一个坏请求头就 500."""
    if not value:
        return fallback
    try:
        return Role(value.strip().lower())
    except ValueError:
        return fallback


def _signature_payload(user: str | None, role: str | None) -> bytes:
    """身份头的规范化字节串.

    用 JSON 数组而不是 `user|role` 直接拼接：后者在 user 里带 `|` 时会产生歧义，
    使得一份合法签名可以被重排成另一组 (user, role) 后仍然通过校验。
    """
    return json.dumps([(user or "").strip(), (role or "").strip()], ensure_ascii=False).encode("utf-8")


def sign_principal(user: str | None, role: str | None, secret: str) -> str:
    """按规范化身份生成 HMAC-SHA256 签名（十六进制），供可信代理注入 X-DocFlow-Signature."""
    return hmac.new(secret.encode("utf-8"), _signature_payload(user, role), sha256).hexdigest()


def verify_principal_signature(user: str | None, role: str | None, signature: str | None, secret: str) -> bool:
    """常量时间比对身份头签名，防止客户端直接伪造身份头."""
    if not signature:
        return False
    return hmac.compare_digest(sign_principal(user, role, secret), signature.strip())


def resolve_principal(
    user: str | None,
    role: str | None,
    *,
    enabled: bool,
    default_role: Role,
) -> Principal:
    """从请求携带的身份信息解析操作者.

    关闭 RBAC 时一律按 admin 处理——本地/单机场景不应被权限拦住。
    """
    if not enabled:
        return Principal(name=(user or "").strip() or "anonymous", role=Role.ADMIN)
    return Principal(name=(user or "").strip() or "anonymous", role=parse_role(role, default_role))
