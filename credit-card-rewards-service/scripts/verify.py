"""Low-dependency artifact verification used locally and in CI."""
from __future__ import annotations
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
required=['openapi.json','schemas/CardTermsV1.schema.json','schemas/CardCatalogueSnapshotV1.schema.json','Dockerfile','compose.yaml']
for name in required:
    path=ROOT/name
    if not path.exists(): raise SystemExit('missing '+name)
for name in required[:3]: json.loads((ROOT/name).read_text())
contract=(ROOT/'docs/CREDIT_CARD_API_CONTRACT.md').read_text()
api=json.loads((ROOT/'openapi.json').read_text())
expected=['/api/v1/cards','/api/v1/cards/{card_id}','/api/v1/cards/compare','/api/v1/internal/catalogue-snapshots/current','/api/v1/internal/card-versions/{card_version_id}','/api/v1/admin/sources','/api/v1/admin/scrape-runs','/api/v1/admin/scrape-runs/{run_id}','/api/v1/admin/extraction-candidates','/api/v1/admin/extraction-candidates/{candidate_id}','/api/v1/admin/extraction-candidates/{candidate_id}/decisions']
if set(expected)-set(api['paths']): raise SystemExit('OpenAPI route parity failure')
if 'TODO' in (ROOT/'credit_card_service').joinpath('api.py').read_text(): raise SystemExit('TODO in runtime')
print(json.dumps({'status':'verified','openapi_paths':len(api['paths']),'contract_bytes':len(contract)}))
