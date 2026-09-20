"""The operation contract: what a capability declares, takes and returns.

Split out of ``operations_registry`` to break an import cycle, not to make a
new layer. The registry imports ``mailbox_processor`` at module scope, which
reaches ``modules.mailbox`` -> failure analysis -> ``nexus_tool_router``, and
that router needs ``OperationValidationError`` and
``normalize_integer_argument`` back. Importing the registry first was fine;
importing the router first - which is what collecting
``tests/test_task_orchestrator.py`` on its own does - found the registry
half-built, its definitions not yet executed, and failed with
``cannot import name 'OperationValidationError' from partially initialized
module``.

These pieces are pure data and validation with no project dependencies, so as
a leaf they can be imported from either side at any point in the cycle. The
registry re-exports them, so ``from modules.operations_registry import
OperationSpec`` keeps working and remains the ordinary way to reach them.
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

    ``mutates_state`` is required and keyword-only on purpose. Read-only used
    to be defined negatively - "everything in the catalog except the names on
    a denylist" - which made the safe set depend on somebody remembering to
    extend that list whenever a mutating operation was added. A new operation
    would have been read-only by default, and the diagnostic role that may
    only read would have inherited it silently. Declaring access per
    operation makes the question unskippable: a spec that does not answer it
    does not construct, and a role that may only read filters on the answer
    rather than on a list maintained somewhere else.
    """

    name: str
    description: str
    arguments: list[OperationArgSpec] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    mutates_state: bool = field(kw_only=True)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "success": self.success,
            "output_lines": list(self.output_lines),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "error_summary": self.error_summary,
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
