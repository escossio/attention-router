"""Validate JSON without coercion, defaults, network access or engine imports."""

import copy
import json
import math
from dataclasses import dataclass
from importlib.resources import files
from typing import TypeGuard, cast

from jsonschema import Draft202012Validator, FormatChecker

from ._types import IntegrationContractMessage

_SCHEMA = json.loads(files(__package__).joinpath("_schema.json").read_text(encoding="utf-8"))
SCHEMA_ID: str = _SCHEMA["$id"]
_VALIDATOR = Draft202012Validator(_SCHEMA, format_checker=FormatChecker())


@dataclass(frozen=True)
class ValidationIssue:
    """A JSON Pointer and validation keyword, without rejected payload values."""

    path: str
    keyword: str


class ContractValidationError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]):
        self.issues = issues
        super().__init__("Invalid integration contract")


def _pointer(parts) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _check_json(value: object, path: tuple, ancestors: set[int]) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (dict, list) or id(value) in ancestors:
        raise ContractValidationError((ValidationIssue(_pointer(path), "json"),))
    ancestors.add(id(value))
    try:
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, child in items:
            if isinstance(value, dict) and type(key) is not str:
                raise ContractValidationError((ValidationIssue(_pointer(path), "json"),))
            _check_json(child, (*path, key), ancestors)
    finally:
        ancestors.remove(id(value))


def validate_contract(value: object) -> IntegrationContractMessage:
    """Return a detached, validated JSON value. Never normalize or mutate the input."""
    try:
        _check_json(value, (), set())
        result = copy.deepcopy(value)
        errors = tuple(_VALIDATOR.iter_errors(result))
    except RecursionError:
        raise ContractValidationError((ValidationIssue("", "json"),)) from None
    if errors:
        # Do not expose jsonschema messages, instances or exception contexts.
        raise ContractValidationError(tuple(
            ValidationIssue(_pointer(error.absolute_path), str(error.validator))
            for error in errors
        ))
    return cast(IntegrationContractMessage, result)


def _invalid_constant(_value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def parse_contract(text: str) -> IntegrationContractMessage:
    """Parse JSON text and validate the unmodified wire representation."""
    if not isinstance(text, str):
        raise ContractValidationError((ValidationIssue("", "json"),))
    try:
        value = json.loads(text, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise ContractValidationError((ValidationIssue("", "json"),)) from None
    return validate_contract(value)


def serialize_contract(value: object) -> str:
    """Validate before serialization; output is JSON, not a signing/canonicalization format."""
    return json.dumps(validate_contract(value), ensure_ascii=True, allow_nan=False)


def is_contract(value: object) -> TypeGuard[IntegrationContractMessage]:
    try:
        validate_contract(value)
    except ContractValidationError:
        return False
    return True


def get_schema() -> dict:
    """Return a copy so callers cannot change the SDK validator's schema."""
    return copy.deepcopy(_SCHEMA)
