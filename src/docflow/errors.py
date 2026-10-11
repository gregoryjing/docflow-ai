"""领域异常。所有对外抛出的异常都继承 DocFlowError，方便 CLI / Web 统一转成用户可读提示."""

from __future__ import annotations


class DocFlowError(Exception):
    """平台内所有可预期错误的基类."""


class UnsupportedFormatError(DocFlowError):
    """文件类型不在支持列表内."""


class ParseError(DocFlowError):
    """文件能识别类型但解析失败（损坏、加密、结构异常）."""


class SchemaError(DocFlowError):
    """Schema 定义或引用有误."""


class PipelineError(DocFlowError):
    """管线执行失败."""


class OcrUnavailableError(DocFlowError):
    """OCR 链路被调用但依赖或模型服务不可用."""


class StorageError(DocFlowError):
    """持久化层出错."""


class PermissionDenied(DocFlowError):
    """当前身份的角色不具备执行该操作所需的权限."""
