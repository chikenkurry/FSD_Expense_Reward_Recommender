"""Extract structured card facts from HTML, JSON-LD, and PDF documents.

Supports both deterministic heuristic/regex extraction and high-precision
LLM-assisted extraction via Google Gemini Flash for complex terms (minimum spend,
caps, MCC exclusions).
"""

from __future__ import annotations

from html.parser import HTMLParser
import json
import os
import re
import ssl
from typing import Any
import urllib.error
import urllib.request
import zlib

from .util import decimal_string

PARSER_VERSION = "generic-html-jsonld-llm-v2"
MAX_EVIDENCE = 500
MAX_TEXT = 1_000_000

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")


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
            self._script_kind = (
                "jsonld"
                if (dict(attrs).get("type") or "").strip().lower() == "application/ld+json"
                else "other"
            )
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


def extract_pdf_text(data: bytes) -> str:
    """Extract plain text streams from PDF bytes without external dependencies."""
    text_parts: list[str] = []
    stream_matches = re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL)
    for m in stream_matches:
        raw_stream = m.group(1)
        decompressed = None
        try:
            decompressed = zlib.decompress(raw_stream)
        except Exception:
            try:
                decompressed = zlib.decompress(raw_stream, -zlib.MAX_WBITS)
            except Exception:
                decompressed = raw_stream
        if decompressed:
            bt_matches = re.findall(rb"BT\s+(.*?)\s+ET", decompressed, re.DOTALL)
            for bt in bt_matches:
                for s in re.findall(rb"\(((?:[^\\)]|\\.)*)\)\s*Tj", bt):
                    text_parts.append(s.decode("latin1", errors="replace"))
                for tj in re.findall(rb"\[(.*?)\]\s*TJ", bt, re.DOTALL):
                    for s in re.findall(rb"\(((?:[^\\)]|\\.)*)\)", tj):
                        text_parts.append(s.decode("latin1", errors="replace"))
    return " ".join(text_parts).strip()


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
    for item in ("dining", "restaurant", "groceries", "grocery", "travel", "transport", "fuel", "online", "shopping", "utilities"):
        if item in lower:
            return "groceries" if item == "grocery" else ("dining" if item == "restaurant" else item)
    return "general"


def _append_reward(out: list[dict], category: str, rate: str, unit: str, evidence: str, min_spend: str | None = None, cap_key: str | None = None) -> None:
    try:
        rate = decimal_string(rate)
    except ValueError:
        return
    item: dict[str, Any] = {
        "category": category,
        "earn_rate": rate,
        "unit": unit,
        "spend_unit": "1",
        "evidence": evidence[:MAX_EVIDENCE],
    }
    if min_spend:
        try:
            item["minimum_spend"] = decimal_string(min_spend)
        except ValueError:
            pass
    if cap_key:
        item["cap_group_key"] = cap_key
    if not any((x["category"], x["earn_rate"], x["unit"]) == (item["category"], item["earn_rate"], item["unit"]) for x in out):
        out.append(item)


def extract_with_gemini(corpus: str, source: dict, api_key: str | None = None) -> dict | None:
    """Extract rich structured Singapore credit card terms using Google Gemini Flash."""
    key = api_key or source.get("gemini_api_key") or DEFAULT_GEMINI_KEY
    if not key:
        return None

    prompt = (
        "You are an expert financial analyst extracting credit card reward terms from bank documentation in Singapore.\n"
        "Extract the card terms strictly into a JSON object adhering to this schema:\n"
        "{\n"
        '  "rewards": [\n'
        '    {"category": "dining|groceries|transport|fuel|shopping|travel|utilities|general", '
        '     "earn_rate": "decimal fraction e.g. 0.06 or 0.015", "unit": "cashback_percent|miles_per_currency|points_per_currency", '
        '     "minimum_spend": "800.00 or null", "evidence": "quote snippet"}\n'
        "  ],\n"
        '  "annual_fee": {"amount": "196.20", "currency": "SGD", "first_year_waiver": true, "evidence": "quote snippet"},\n'
        '  "minimum_monthly_spend": "800.00 or null",\n'
        '  "cap_groups": [\n'
        '    {"cap_key": "monthly_rebate_cap", "amount": "70.00", "period": "calendar_month", "mode": "limited"}\n'
        "  ],\n"
        '  "excluded_mccs": ["9399", "6540", "6300", "4900"],\n'
        '  "welcome_offer": {"summary": "summary text", "evidence": "quote snippet"}\n'
        "}\n"
        "Return ONLY raw valid JSON without markdown code blocks or additional explanation.\n\n"
        f"Document text:\n{corpus[:12000]}"
    )

    req_body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_ENDPOINT,
        data=req_body,
        headers={"Content-Type": "application/json", "X-goog-api-key": key},
    )

    try:
        ctx = ssl.create_default_context()
        try:
            resp = urllib.request.urlopen(req, context=ctx, timeout=12)
        except urllib.error.URLError:
            ctx = ssl._create_unverified_context()
            resp = urllib.request.urlopen(req, context=ctx, timeout=12)

        with resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            parsed = json.loads(text)

            rewards: list[dict] = []
            for r in parsed.get("rewards", []):
                _append_reward(
                    rewards,
                    r.get("category", "general"),
                    str(r.get("earn_rate", "0.01")),
                    r.get("unit", "cashback_percent"),
                    r.get("evidence", ""),
                    min_spend=str(r["minimum_spend"]) if r.get("minimum_spend") else None,
                    cap_key=r.get("cap_group_key"),
                )

            annual_fee = None
            if parsed.get("annual_fee") and isinstance(parsed["annual_fee"], dict):
                fee_obj = parsed["annual_fee"]
                try:
                    annual_fee = {
                        "amount": decimal_string(str(fee_obj.get("amount", "0.00"))),
                        "currency": fee_obj.get("currency", "SGD"),
                        "first_year_waiver": bool(fee_obj.get("first_year_waiver", False)),
                        "evidence": fee_obj.get("evidence", "")[:MAX_EVIDENCE],
                    }
                except ValueError:
                    annual_fee = None

            cap_groups = []
            for cap in parsed.get("cap_groups", []):
                if isinstance(cap, dict) and cap.get("cap_key"):
                    try:
                        amt = decimal_string(str(cap["amount"])) if cap.get("amount") else None
                    except ValueError:
                        amt = None
                    cap_groups.append({
                        "cap_key": str(cap["cap_key"]),
                        "amount": amt,
                        "period": str(cap.get("period", "calendar_month")),
                        "mode": str(cap.get("mode", "limited")),
                    })

            min_spend = None
            if parsed.get("minimum_monthly_spend"):
                try:
                    min_spend = decimal_string(str(parsed["minimum_monthly_spend"]))
                except ValueError:
                    pass

            welcome = None
            if parsed.get("welcome_offer") and isinstance(parsed["welcome_offer"], dict):
                welcome = {
                    "summary": str(parsed["welcome_offer"].get("summary", ""))[:MAX_EVIDENCE],
                    "evidence": str(parsed["welcome_offer"].get("evidence", ""))[:MAX_EVIDENCE],
                }

            reset_period = "calendar_month"
            if str(parsed.get("reset_period", "")).lower() in ("calendar_month", "statement_cycle", "quarterly"):
                reset_period = str(parsed["reset_period"]).lower()

            return {
                "annual_fee": annual_fee,
                "rewards": rewards,
                "welcome_offer": welcome,
                "minimum_monthly_spend": min_spend,
                "reset_period": reset_period,
                "cap_groups": cap_groups,
                "excluded_mccs": [str(c) for c in parsed.get("excluded_mccs", []) if str(c).isdigit()],
                "foreign_currency_fee_rate": "0.0325",
            }
    except Exception:
        return None


def extract(source: dict, html: str | bytes) -> dict:
    """Extract structured rewards, fees, minimum spends, and cap rules from HTML or PDF."""
    if isinstance(html, bytes):
        if html.startswith(b"%PDF"):
            corpus = extract_pdf_text(html)
        else:
            html = html.decode("utf-8", errors="replace")

    if isinstance(html, str):
        if html.startswith("%PDF"):
            corpus = extract_pdf_text(html.encode("latin1", errors="replace"))
        else:
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

    # 1. Attempt LLM-assisted extraction if explicitly requested or if LLM key is configured
    if source.get("use_llm") or source.get("use_gemini"):
        llm_result = extract_with_gemini(corpus, source)
        if llm_result and (llm_result["rewards"] or llm_result["annual_fee"]):
            return llm_result

    # 2. Deterministic heuristic & regex extraction fallback
    rewards: list[dict] = []
    annual_fee = None
    welcome = None
    min_spend = None
    cap_groups: list[dict] = []
    hints = source.get("extraction_hints", {})
    snippets = _snippets(corpus)

    cashback = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%\s*(?:cash\s*back|cashback|cash\s*rebates?|rebates?)", re.I)
    earn = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(points?|miles?)\s*(?:per|/|for each)\s*(?:S?\$|USD\s*|SGD\s*|\$)?\s*(?:1|one)\b", re.I)
    amount_pattern = r"(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?"
    fee_direct = re.compile(r"annual\s+fee[^\d$]{0,30}(S\$|US\$|SGD\s*|USD\s*|\$)?\s*(" + amount_pattern + r")", re.I)
    min_spend_re = re.compile(r"min(?:imum)?\s+(?:monthly\s+)?(?:qualifying\s+)?spend\s+(?:of\s+)?(?:S?\$|SGD\s*)?(" + amount_pattern + r")", re.I)
    cap_re = re.compile(r"cap(?:ped)?\s+(?:at\s+)?(?:S?\$|SGD\s*)?(" + amount_pattern + r")\s*(?:per\s+(?:calendar\s+month|month|statement))?", re.I)

    for snippet in snippets:
        if min_spend is None:
            m_spend = min_spend_re.search(snippet)
            if m_spend:
                try:
                    min_spend = decimal_string(m_spend.group(1).replace(",", ""))
                except ValueError:
                    pass

        m_cap = cap_re.search(snippet)
        if m_cap and not cap_groups:
            try:
                cap_groups.append({
                    "cap_key": "monthly_rebate_cap",
                    "amount": decimal_string(m_cap.group(1).replace(",", "")),
                    "period": "calendar_month",
                    "mode": "limited",
                })
            except ValueError:
                pass

        for match in cashback.finditer(snippet):
            _append_reward(rewards, _category(snippet), match.group(1), "cashback_percent", snippet, min_spend=min_spend)
        for match in earn.finditer(snippet):
            _append_reward(rewards, _category(snippet), match.group(1), "points_per_currency" if match.group(2).lower().startswith("point") else "miles_per_currency", snippet, min_spend=min_spend)
        if annual_fee is None:
            matched = fee_direct.search(snippet)
            if matched:
                symbol, amount = matched.groups()
                marker = (symbol or "").strip().upper()
                currency = "SGD" if marker in ("S$", "SGD") else ("USD" if marker in ("US$", "USD") else "UNKNOWN")
                annual_fee = {"amount": decimal_string(amount), "currency": currency, "evidence": snippet[:MAX_EVIDENCE]}
        if welcome is None and re.search(r"\b(welcome|sign[ -]?up)\b", snippet, re.I) and re.search(r"\b(offer|bonus|earn|receive)\b", snippet, re.I):
            welcome = {"summary": snippet[:MAX_EVIDENCE], "evidence": snippet[:MAX_EVIDENCE]}

    # Explicit operator patterns are additive
    for pattern in hints.get("reward_patterns", []):
        if not isinstance(pattern, dict) or not isinstance(pattern.get("regex"), str):
            continue
        try:
            match = re.search(pattern["regex"], corpus, re.I)
        except re.error:
            continue
        if match and match.groups():
            _append_reward(rewards, str(pattern.get("category", "general")), match.group(1), str(pattern.get("unit", "points_per_currency")), match.group(0), min_spend=min_spend)

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

    # Standard Singapore exclusions (MCCs)
    standard_mccs: list[str] = []
    if re.search(r"\b(excludes?|exclusions?|excluding|not\s+eligible)\b", corpus, re.I):
        if re.search(r"\b(tax|government|town\s*council)\b", corpus, re.I):
            standard_mccs.append("9399")
        if re.search(r"\b(wallet|grab|shopee|prepaid|stored\s*value)\b", corpus, re.I):
            standard_mccs.append("6540")
        if re.search(r"\b(insurance)\b", corpus, re.I):
            standard_mccs.append("6300")
        if re.search(r"\b(utilities|electricity)\b", corpus, re.I):
            standard_mccs.append("4900")

    reset_period = "calendar_month"
    if re.search(r"\b(quarterly|each\s+quarter|per\s+quarter)\b", corpus, re.I):
        reset_period = "quarterly"
    elif re.search(r"\b(statement\s+(?:month|cycle)|billing\s+cycle)\b", corpus, re.I):
        reset_period = "statement_cycle"

    return {
        "annual_fee": annual_fee,
        "rewards": rewards,
        "welcome_offer": welcome,
        "minimum_monthly_spend": min_spend,
        "reset_period": reset_period,
        "cap_groups": cap_groups,
        "excluded_mccs": standard_mccs,
        "foreign_currency_fee_rate": "0.0325",
    }
