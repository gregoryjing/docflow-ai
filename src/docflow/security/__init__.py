"""交付层安全：轻量 RBAC 与审计日志."""

from docflow.security.audit import AuditAction, AuditEvent
from docflow.security.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    Principal,
    Role,
    parse_role,
    resolve_principal,
    sign_principal,
    verify_principal_signature,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "AuditAction",
    "AuditEvent",
    "Permission",
    "Principal",
    "Role",
    "parse_role",
    "resolve_principal",
    "sign_principal",
    "verify_principal_signature",
]
