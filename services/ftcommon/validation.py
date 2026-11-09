"""Input validation: strict schemas in resilient mode, type-only schemas in baseline."""
from typing import Type, TypeVar

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from . import config

M = TypeVar("M", bound=BaseModel)


def validate_payload(strict: Type[M], loose: Type[BaseModel], payload: object) -> M:
    model = strict if config.VALIDATION_STRICT else loose
    if not isinstance(payload, dict):
        raise RequestValidationError([{"loc": ("body",), "msg": "JSON object expected", "type": "type_error"}])
    try:
        return model.model_validate(payload)  # type: ignore[return-value]
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_input=False))
