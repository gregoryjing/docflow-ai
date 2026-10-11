"""RBAC 权限矩阵 + 审计日志 + Web 层访问控制."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.errors import PermissionDenied
from docflow.security import (
    ROLE_PERMISSIONS,
    AuditAction,
    AuditEvent,
    Permission,
    Principal,
    Role,
    parse_role,
    resolve_principal,
    sign_principal,
    verify_principal_signature,
)
from docflow.store import build_session_factory, list_audit, record_audit

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


# ── 权限矩阵 ──────────────────────────────────────────────────
def test_role_permission_matrix() -> None:
    assert Permission.VIEW in ROLE_PERMISSIONS[Role.VIEWER]
    assert Permission.REVIEW not in ROLE_PERMISSIONS[Role.VIEWER]

    assert Permission.REVIEW in ROLE_PERMISSIONS[Role.REVIEWER]
    assert Permission.PROCESS not in ROLE_PERMISSIONS[Role.REVIEWER]

    assert {Permission.PROCESS, Permission.EXPORT} <= ROLE_PERMISSIONS[Role.OPERATOR]
    assert Permission.ADMIN not in ROLE_PERMISSIONS[Role.OPERATOR]

    assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)


def test_principal_require_raises_for_missing_permission() -> None:
    viewer = Principal(name="bob", role=Role.VIEWER)
    assert viewer.can(Permission.VIEW)
    assert not viewer.can(Permission.PROCESS)
    with pytest.raises(PermissionDenied):
        viewer.require(Permission.PROCESS)


def test_parse_role_is_case_insensitive_and_falls_back() -> None:
    assert parse_role("OPERATOR", Role.VIEWER) is Role.OPERATOR
    assert parse_role("  admin ", Role.VIEWER) is Role.ADMIN
    assert parse_role("wizard", Role.VIEWER) is Role.VIEWER
    assert parse_role(None, Role.REVIEWER) is Role.REVIEWER


def test_resolve_principal_disabled_is_admin() -> None:
    principal = resolve_principal(None, None, enabled=False, default_role=Role.VIEWER)
    assert principal.role is Role.ADMIN
    assert principal.name == "anonymous"


def test_resolve_principal_enabled_uses_header_then_default() -> None:
    explicit = resolve_principal("alice", "reviewer", enabled=True, default_role=Role.VIEWER)
    assert explicit.name == "alice"
    assert explicit.role is Role.REVIEWER

    fallback = resolve_principal("", None, enabled=True, default_role=Role.VIEWER)
    assert fallback.role is Role.VIEWER


# ── 身份头签名 ────────────────────────────────────────────────
def test_sign_and_verify_principal_signature() -> None:
    signature = sign_principal("alice", "operator", "s3cr3t")
    assert verify_principal_signature("alice", "operator", signature, "s3cr3t")
    # 篡改任一身份字段都应校验失败
    assert not verify_principal_signature("alice", "admin", signature, "s3cr3t")
    assert not verify_principal_signature("eve", "operator", signature, "s3cr3t")
    assert not verify_principal_signature("alice", "operator", signature, "wrong-secret")
    assert not verify_principal_signature("alice", "operator", None, "s3cr3t")
    assert not verify_principal_signature("alice", "operator", "deadbeef", "s3cr3t")


def test_signature_payload_disambiguates_separator() -> None:
    """user 里带 `|` 不应产生歧义：不同 (user, role) 组合不能撞出同一签名."""
    assert sign_principal("a|b", "viewer", "k") != sign_principal("a", "b|viewer", "k")


# ── 审计日志 ──────────────────────────────────────────────────
def test_record_and_list_audit(settings: Settings) -> None:
    factory = build_session_factory(settings)
    principal = Principal(name="alice", role=Role.OPERATOR)
    with factory() as session:
        record_audit(session, AuditEvent.of(principal, AuditAction.PROCESS, target="run-1", detail="ok"))
        record_audit(session, AuditEvent.of(principal, AuditAction.EXPORT, target="run-1"))
        session.commit()

    with factory() as session:
        assert len(list_audit(session)) == 2
        exports = list_audit(session, action=AuditAction.EXPORT.value)
        assert len(exports) == 1
        assert exports[0].target == "run-1"
        assert exports[0].actor == "alice"
        assert exports[0].role == "operator"


# ── Web 层访问控制 ────────────────────────────────────────────
def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rbac: bool,
    trust_proxy: bool = False,
    secret: str | None = None,
) -> TestClient:
    """构造 Web 测试客户端.

    RBAC 开启后身份头默认不可信：要么显式 `trust_proxy=True`（可信代理模式），
    要么配 `secret` 并用签名头。什么都不配时应 fail-closed（降级兜底角色）。
    """
    monkeypatch.setenv("DOCFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOCFLOW_SCHEMAS_DIR", str(SCHEMAS_DIR))
    monkeypatch.setenv("DOCFLOW_RBAC_ENABLED", "true" if rbac else "false")
    monkeypatch.setenv("DOCFLOW_RBAC_TRUST_PROXY", "true" if trust_proxy else "false")
    if secret is None:
        monkeypatch.delenv("DOCFLOW_RBAC_HEADER_SECRET", raising=False)
    else:
        monkeypatch.setenv("DOCFLOW_RBAC_HEADER_SECRET", secret)

    from docflow.web.app import app

    return TestClient(app)


def test_web_denies_viewer_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, rbac=True, trust_proxy=True) as client:
        response = client.get("/runs/nonexistent/export.xlsx", headers={"X-DocFlow-Role": "viewer"})
    assert response.status_code == 403


def test_web_operator_passes_permission_but_hits_missing_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, rbac=True, trust_proxy=True) as client:
        response = client.get("/runs/nonexistent/export.xlsx", headers={"X-DocFlow-Role": "operator"})
    assert response.status_code == 404  # 权限通过，只是运行不存在


def test_web_audit_page_requires_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, rbac=True, trust_proxy=True) as client:
        denied = client.get("/audit", headers={"X-DocFlow-Role": "reviewer"})
        allowed = client.get("/audit", headers={"X-DocFlow-Role": "admin", "X-DocFlow-User": "root"})
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert "审计日志" in allowed.text


def test_web_fails_closed_when_identity_headers_untrusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RBAC 开启但既未配签名密钥、也未声明信任代理时，伪造 admin 头必须被忽略."""
    with _client(tmp_path, monkeypatch, rbac=True) as client:
        forged = client.get("/audit", headers={"X-DocFlow-User": "attacker", "X-DocFlow-Role": "admin"})
    assert forged.status_code == 403  # 降级为兜底角色 viewer，而非采信 admin


def test_web_requires_signature_when_secret_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cr3t"
    with _client(tmp_path, monkeypatch, rbac=True, secret=secret) as client:
        missing = client.get("/audit", headers={"X-DocFlow-User": "alice", "X-DocFlow-Role": "admin"})
        bad = client.get(
            "/audit",
            headers={"X-DocFlow-User": "alice", "X-DocFlow-Role": "admin", "X-DocFlow-Signature": "deadbeef"},
        )
        good = client.get(
            "/audit",
            headers={
                "X-DocFlow-User": "alice",
                "X-DocFlow-Role": "admin",
                "X-DocFlow-Signature": sign_principal("alice", "admin", secret),
            },
        )
    assert missing.status_code == 401
    assert bad.status_code == 401
    assert good.status_code == 200


def test_web_records_access_denied_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, rbac=True) as client:
        client.get("/audit", headers={"X-DocFlow-Role": "viewer", "X-DocFlow-User": "eve"})

    settings = Settings()
    factory = build_session_factory(settings)
    with factory() as session:
        rows = list_audit(session, action=AuditAction.ACCESS_DENIED.value)
    assert any(row.actor == "eve" and row.role == "viewer" for row in rows)


def test_web_rbac_disabled_allows_admin_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch, rbac=False) as client:
        response = client.get("/audit")
    assert response.status_code == 200


def test_safe_next_blocks_open_redirect() -> None:
    from docflow.web.app import _safe_next

    assert _safe_next("/review") == "/review"
    assert _safe_next("/runs/abc?status=pending") == "/runs/abc?status=pending"
    assert _safe_next("//evil.com") == "/review"
    assert _safe_next("https://evil.com") == "/review"
    assert _safe_next("\\\\evil.com") == "/review"
    assert _safe_next("") == "/review"


def test_web_rejects_oversized_request_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按 Content-Length 早拒超大请求体，防止 multipart 解析把整包写入磁盘."""
    monkeypatch.setenv("DOCFLOW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DOCFLOW_SCHEMAS_DIR", str(SCHEMAS_DIR))
    monkeypatch.setenv("DOCFLOW_MAX_REQUEST_BYTES", "1024")

    from docflow.web.app import app

    with TestClient(app) as client:
        response = client.post(
            "/process",
            content=b"x" * 4096,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 413
