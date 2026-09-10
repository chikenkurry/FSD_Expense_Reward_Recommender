from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from typing import Any

from .util import decimal_string

PARSER_VERSION = "generic-html-jsonld-v1"
MAX_EVIDENCE = 500
MAX_TEXT = 1_000_000


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignore = 0
        self._script_kind: str | None = None
        self._script: list[str] = []
        self.jsonld: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "noscript"):
            self._ignore += 1
        if tag == "script":
            self._script_kind = "jsonld" if (dict(attrs).get("type") or "").strip().lower() == "application/ld+json" else "other"
            self._script = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "noscript") and self._ignore:
            self._ignore -= 1
        if tag == "script":
            if self._script_kind == "jsonld":
                self.jsonld.append("".join(self._script))
            self._script_kind = None
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script_kind == "jsonld":
            self._script.append(data)
        elif self._script_kind is None and not self._ignore:
            self.parts.append(data)


def _jsonld_strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"description", "name", "offers", "headline", "text"} and isinstance(child, str):
                result.append(child)
            result.extend(_jsonld_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_jsonld_strings(child))
    return result


def _snippets(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
    return [re.sub(r"\s+", " ", piece).strip()[:MAX_EVIDENCE] for piece in pieces if piece.strip()]


def _category(snippet: str) -> str:
    lower = snippet.lower()
    for item in ("dining", "restaurant", "groceries", "grocery", "travel", "transport", "fuel", "online", "shopping"):
        if item in lower:
            return "groceries" if item == "grocery" else ("dining" if item == "restaurant" else item)
    return "general"


def _append_reward(out: list[dict], category: str, rate: str, unit: str, evidence: str) -> None:
    try:
        rate = decimal_string(rate)
    except ValueError:
        return
    item = {"category": category, "earn_rate": rate, "unit": unit, "spend_unit": "1", "evidence": evidence[:MAX_EVIDENCE]}
    if not any((x["category"], x["earn_rate"], x["unit"]) == (item["category"], item["earn_rate"], item["unit"]) for x in out):
        out.append(item)


def extract(source: dict, html: str) -> dict:
    parser = _TextParser()
    parser.feed(html[:MAX_TEXT])
    visible = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    ld_text: list[str] = []
    for raw in parser.jsonld:
        try:
            ld_text.extend(_jsonld_strings(json.loads(raw)))
        except json.JSONDecodeError:
            continue
    corpus = visible + "\n" + "\n".join(ld_text)
    rewards: list[dict] = []
    annual_fee = None
    welcome = None
    hints = source.get("extraction_hints", {})
    snippets = _snippets(corpus)
    cashback = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%\s*(?:cash\s*back|cashback|cash\s*rebates?|rebates?)", re.I)
    earn = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(points?|miles?)\s*(?:per|/|for each)\s*(?:S?\$|USD\s*|SGD\s*|\$)?\s*(?:1|one)\b", re.I)
    amount_pattern = r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?"
    fee_direct = re.compile(r"annual\s+fee[^\d$]{0,30}(S\$|US\$|SGD\s*|USD\s*|\$)?\s*(" + amount_pattern + r")", re.I)
    for snippet in snippets:
        for match in cashback.finditer(snippet):
            _append_reward(rewards, _category(snippet), match.group(1), "cashback_percent", snippet)
        for match in earn.finditer(snippet):
            _append_reward(rewards, _category(snippet), match.group(1), "points_per_currency" if match.group(2).lower().startswith("point") else "miles_per_currency", snippet)
        if annual_fee is None:
            matched = fee_direct.search(snippet)
            if matched:
                symbol, amount = matched.groups()
                marker = (symbol or "").strip().upper()
                currency = "SGD" if marker in ("S$", "SGD") else ("USD" if marker in ("US$", "USD") else "UNKNOWN")
                annual_fee = {"amount": decimal_string(amount), "currency": currency, "evidence": snippet[:MAX_EVIDENCE]}
        if welcome is None and re.search(r"\b(welcome|sign[ -]?up)\b", snippet, re.I) and re.search(r"\b(offer|bonus|earn|receive)\b", snippet, re.I):
            welcome = {"summary": snippet[:MAX_EVIDENCE], "evidence": snippet[:MAX_EVIDENCE]}
    # Explicit operator patterns are additive: useful for unusual official wording.
    for pattern in hints.get("reward_patterns", []):
        if not isinstance(pattern, dict) or not isinstance(pattern.get("regex"), str):
            continue
        try:
            match = re.search(pattern["regex"], corpus, re.I)
        except re.error:
            continue
        if match and match.groups():
            _append_reward(rewards, str(pattern.get("category", "general")), match.group(1), str(pattern.get("unit", "points_per_currency")), match.group(0))
    if annual_fee is None and isinstance(hints.get("annual_fee_regex"), str):
        try:
            match = re.search(hints["annual_fee_regex"], corpus, re.I)
            if match and match.groups():
                annual_fee = {"amount": decimal_string(match.group(1)), "currency": str(hints.get("annual_fee_currency", "UNKNOWN")), "evidence": match.group(0)[:MAX_EVIDENCE]}
        except (re.error, ValueError):
            pass
    if welcome is None and isinstance(hints.get("welcome_offer_regex"), str):
        try:
            match = re.search(hints["welcome_offer_regex"], corpus, re.I)
            if match:
                welcome = {"summary": match.group(0)[:MAX_EVIDENCE], "evidence": match.group(0)[:MAX_EVIDENCE]}
        except re.error:
            pass
    return {"annual_fee": annual_fee, "rewards": rewards, "welcome_offer": welcome}
