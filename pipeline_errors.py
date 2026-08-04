"""Structured failures raised by data-pipeline boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class PipelineError(RuntimeError):
    component: str
    operation: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "component": self.component,
            "operation": self.operation,
            "message": self.message,
            "context": self.context,
            "retryable": self.retryable,
        }


class SourceRequestError(PipelineError):
    """A source request exhausted its bounded retry policy."""


class DataValidationError(PipelineError):
    """Staged output failed structural or canonical validation."""


class DataPromotionError(PipelineError):
    """Validated output could not be promoted and was rolled back."""

