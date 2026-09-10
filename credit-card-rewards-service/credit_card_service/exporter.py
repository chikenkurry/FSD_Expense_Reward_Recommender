from __future__ import annotations

import csv
import io
import json

from . import db


def render(db_path: str, fmt: str) -> str:
    records = db.cards(db_path)
    if fmt == "json":
        return json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if fmt != "csv":
        raise ValueError("format must be json or csv")
    output = io.StringIO(newline="")
    columns = ["card_id", "issuer", "name", "source_url", "annual_fee_amount", "annual_fee_currency", "reward_category", "earn_rate", "unit", "spend_unit", "reward_evidence", "welcome_offer", "source_id", "fetched_at", "content_sha256", "parser_version", "status", "error"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for record in records:
        fee = record.get("annual_fee") or {}
        provenance = record["provenance"]
        base = {"card_id": record["card_id"], "issuer": record["issuer"], "name": record["name"], "source_url": record["source_url"],
                "annual_fee_amount": fee.get("amount", ""), "annual_fee_currency": fee.get("currency", ""),
                "welcome_offer": (record.get("welcome_offer") or {}).get("summary", ""), "source_id": provenance["source_id"],
                "fetched_at": provenance["fetched_at"], "content_sha256": provenance["content_sha256"], "parser_version": provenance["parser_version"],
                "status": record.get("status"), "error": record.get("error") or ""}
        rewards = record.get("rewards") or [{}]
        for reward in rewards:
            writer.writerow({**base, "reward_category": reward.get("category", ""), "earn_rate": reward.get("earn_rate", ""),
                             "unit": reward.get("unit", ""), "spend_unit": reward.get("spend_unit", ""), "reward_evidence": reward.get("evidence", "")})
    return output.getvalue()
