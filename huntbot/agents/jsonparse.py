"""Robust JSON extraction from model replies (think blocks, fences, trailing commas, missing keys)."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar, get_args, get_origin
import typing

from pydantic import BaseModel, ValidationError

from .schemas import DEFAULTS, Objection, _unknown_member

T = TypeVar("T", bound=BaseModel)


class ParseError(ValueError):
    pass


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _first_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _repair(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    return s


def _clean_list(items: list, limit: int) -> list:
    return [x[:120] if isinstance(x, str) else x for x in items[:limit]]


def _coerce(model: type[BaseModel], data: dict) -> dict:
    defaults = DEFAULTS.get(model, {})
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        value = data.get(name, defaults.get(name))
        if value is None:
            value = defaults.get(name)
        ann = field.annotation
        origin = get_origin(ann)
        if origin is typing.Literal:
            if value not in get_args(ann):
                value = _unknown_member(ann)
        elif origin is list:
            inner = get_args(ann)[0]
            if not isinstance(value, list):
                value = [] if value in (None, "") else [value]
            if get_origin(inner) is typing.Literal:
                value = [v for v in value if v in get_args(inner)]
            elif inner is Objection:
                value = [_coerce(Objection, v) for v in value if isinstance(v, dict)][:8]
            else:
                limit = 3 if name == "questions_for_seller" else 5
                value = _clean_list([v for v in value if isinstance(v, str)], limit)
        elif ann is bool:
            value = value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1")
        elif ann in (int, float):
            try:
                value = ann(float(str(value).replace(",", ".")))
            except (TypeError, ValueError):
                value = 0
        elif ann is str:
            value = "" if value is None else str(value)
        out[name] = value
    return out


def parse(text: str, model: type[T]) -> T:
    text = _THINK.sub("", text or "")
    fenced = _FENCE.search(text)
    if fenced and "{" in fenced.group(1):
        text = fenced.group(1)
    blob = _first_object(text)
    if blob is None:
        raise ParseError("no JSON object in reply")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        try:
            data = json.loads(_repair(blob))
        except json.JSONDecodeError as exc:
            raise ParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("JSON is not an object")
    # An entirely foreign object (no overlap with the schema) is garbage, not "all defaults".
    if not set(data) & set(model.model_fields):
        raise ParseError("JSON has none of the expected keys")
    try:
        return model.model_validate(_coerce(model, data))
    except ValidationError as exc:
        raise ParseError(str(exc)[:200]) from exc
