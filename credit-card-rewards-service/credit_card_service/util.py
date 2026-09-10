from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_string(value: str) -> str:
    try:
        number = Decimal(value.replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid non-negative decimal: {value!r}")
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
