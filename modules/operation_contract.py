"""The operation contract: what a capability declares, takes and returns.

Split out of ``operations_registry`` to break an import cycle with
``nexus_tool_router``. It must stay a leaf - pure data and validation, no
project imports - or the cycle comes back. The registry re-exports these, so
``from modules.operations_registry import OperationSpec`` keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OperationValidationError(ValueError):
    pass


@dataclass(slots=True)
class OperationArgSpec:
    name: str
    type: str
    description: str
    required: bool = False
    default: int | str | None = None
    minimum: int | None = None
    maximum: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(slots=True)
class OperationSpec:
    """One capability's metadata, including whether calling it can change state.

    ``mutates_state`` has no default on purpose: a read-only role filters on
    it, so an operation that forgets to declare it must fail to construct
    rather than default to safe.

    ``controller_allowed`` lets the task controller find and call one mutating
    operation; every other read-only role still sees only ``mutates_state=False``.
    """

    name: str
    description: str
    arguments: list[OperationArgSpec] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    mutates_state: bool = field(kw_only=True)
    controller_allowed: bool = field(default=False, kw_only=True)

    @property
    def read_only(self) -> bool:
        return not self.mutates_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.name,
            "description": self.description,
            "arguments": [argument.to_dict() for argument in self.arguments],
            "tags": list(self.tags),
            "mutates_state": self.mutates_state,
        }


@dataclass(slots=True)
class OperationResult:
    operation: str
    success: bool
    output_lines: list[str]
    duration_ms: int
    exit_code: int | None = None
    error_summary: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "success": self.success,
            "output_lines": list(self.output_lines),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "error_summary": self.error_summary,
            "metrics": dict(self.metrics),
        }


def normalize_integer_argument(
    spec: OperationArgSpec,
    value: Any,
    *,
    validate_bounds: bool = True,
) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        raise OperationValidationError(f"{spec.name} muss eine ganze Zahl sein.")
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text[:1] in {"+", "-"} else text
        if not digits or not digits.isascii() or not digits.isdigit():
            raise OperationValidationError(f"{spec.name} muss eine ganze Zahl sein.")
        try:
            value = int(text, 10)
        except ValueError as exc:
            raise OperationValidationError(
                f"{spec.name} muss eine ganze Zahl sein."
            ) from exc
    elif not isinstance(value, int):
        raise OperationValidationError(f"{spec.name} muss eine ganze Zahl sein.")

    if validate_bounds:
        if spec.minimum is not None and value < spec.minimum:
            raise OperationValidationError(
                f"{spec.name} muss mindestens {spec.minimum} sein."
            )
        if spec.maximum is not None and value > spec.maximum:
            raise OperationValidationError(
                f"{spec.name} darf höchstens {spec.maximum} sein."
            )
    return value
