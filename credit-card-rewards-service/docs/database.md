# Database

Schema version 2 preserves v1 scraper tables for CLI/export compatibility and adds normalized `banks`, `sources`, `source_documents`, `ingestion_runs`, `run_failures`, `raw_snapshots`, candidates/audit/diff tables, `cards`, immutable `card_versions`, `reward_rules`, evidence, publications, decisions, and idempotency records. Flexible candidate/audit payloads and fully serialized contract terms are JSON; searchable identity, state, rates, relationships, and uniqueness invariants are relational.

Foreign keys are enabled on every connection. `current_publication_one_per_card` prevents two current versions; `active_run_unique` prevents duplicate active source/scope jobs; `UNIQUE(card_id, content_hash)` prevents duplicate immutable versions. V1-to-v2 is additive and deterministic: opening an old database applies v2 schema without modifying legacy snapshots.
