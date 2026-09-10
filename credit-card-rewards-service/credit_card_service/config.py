from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


_HINT_KEYS = {"reward_patterns", "annual_fee_regex", "annual_fee_currency", "welcome_offer_regex"}
_REWARD_UNITS = {"cashback_percent", "points_per_currency", "miles_per_currency"}


def _valid_regex(value: object, label: str, requires_group: bool = False) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a nonempty regex string")
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise ConfigError(f"{label} is not a valid regex: {exc}") from exc
    if requires_group and compiled.groups < 1:
        raise ConfigError(f"{label} must contain a capture group")


def _validate_hints(hints: object) -> dict:
    if not isinstance(hints, dict):
        raise ConfigError("extraction_hints must be an object")
    unknown = set(hints) - _HINT_KEYS
    if unknown:
        raise ConfigError("unknown extraction_hints keys: " + ", ".join(sorted(unknown)))
    if "reward_patterns" in hints:
        patterns = hints["reward_patterns"]
        if not isinstance(patterns, list):
            raise ConfigError("reward_patterns must be a list")
        for index, pattern in enumerate(patterns):
            label = f"reward_patterns[{index}]"
            if not isinstance(pattern, dict) or set(pattern) - {"regex", "category", "unit"}:
                raise ConfigError(f"{label} must be an object with regex, optional category, and optional unit")
            _valid_regex(pattern.get("regex"), f"{label}.regex", requires_group=True)
            if "category" in pattern and (not isinstance(pattern["category"], str) or not pattern["category"].strip()):
                raise ConfigError(f"{label}.category must be a nonempty string when supplied")
            if "unit" in pattern and pattern["unit"] not in _REWARD_UNITS:
                raise ConfigError(f"{label}.unit must be one of: " + ", ".join(sorted(_REWARD_UNITS)))
    if "annual_fee_regex" in hints:
        _valid_regex(hints["annual_fee_regex"], "annual_fee_regex", requires_group=True)
    if "welcome_offer_regex" in hints:
        _valid_regex(hints["welcome_offer_regex"], "welcome_offer_regex")
    if "annual_fee_currency" in hints and (not isinstance(hints["annual_fee_currency"], str) or not hints["annual_fee_currency"].strip()):
        raise ConfigError("annual_fee_currency must be a nonempty string when supplied")
    return hints


def load_config(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read JSON config: {exc}") from exc
    if not isinstance(data, dict) or isinstance(data.get("version"), bool) or data.get("version") != 1 or not isinstance(data.get("sources"), list):
        raise ConfigError("config must be an object with version: 1 and sources: []")
    seen: set[str] = set()
    sources = []
    for item in data["sources"]:
        if not isinstance(item, dict):
            raise ConfigError("every source must be an object")
        required = ("source_id", "issuer", "name", "card_id", "page_url", "enabled")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required[:-1]) or not isinstance(item.get("enabled"), bool):
            raise ConfigError("source requires nonempty source_id, issuer, name, card_id, page_url and boolean enabled")
        if item["source_id"] in seen:
            raise ConfigError(f"duplicate source_id: {item['source_id']}")
        seen.add(item["source_id"])
        parsed = urlparse(item["page_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ConfigError(f"source {item['source_id']} has invalid HTTP(S) page_url")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigError(f"source {item['source_id']} has invalid URL port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError(f"source {item['source_id']} has invalid URL port")
        delay = item.get("request_delay_seconds", 0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0 or delay > 60:
            raise ConfigError("request_delay_seconds must be a number from 0 to 60")
        hints = _validate_hints(item.get("extraction_hints", {}))
        sources.append({**item, "request_delay_seconds": delay, "extraction_hints": hints})
    return {"version": 1, "sources": sources}


def selected_sources(config: dict, source_ids: list[str] | None = None) -> list[dict]:
    by_id = {source["source_id"]: source for source in config["sources"]}
    if source_ids is None:
        return [source for source in config["sources"] if source["enabled"]]
    missing = [item for item in source_ids if item not in by_id]
    if missing:
        raise ConfigError("unknown source_ids: " + ", ".join(missing))
    return [by_id[item] for item in source_ids]
