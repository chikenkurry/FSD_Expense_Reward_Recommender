"""Deterministic Section-13-style offline fixture acceptance path."""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credit_card_service import db
with tempfile.TemporaryDirectory() as directory:
    path=str(Path(directory)/'demo.sqlite'); seeded=db.seed_demo(path,'0.015')
    first=db.queue_run(path,seeded['source_id'],{'type':'all'},'admin_refresh');db.process_one(path);a=db.candidate_list(path)[0];_,approved_a=db.decide(path,a['candidate_id'],1,'approve','Synthetic fixture verified.',[],'demo')
    unchanged=db.queue_run(path,seeded['source_id'],{'type':'all'},'admin_refresh');unchanged_run=db.process_one(path)
    db.seed_demo(path,'0.016');second=db.queue_run(path,seeded['source_id'],{'type':'all'},'admin_refresh');db.process_one(path);b=db.candidate_list(path,{'review_status':'pending'})[0];_,approved_b=db.decide(path,b['candidate_id'],1,'approve','Synthetic fixture verified.',[],'demo')
    with db.connect(path) as conn:
        retained=conn.execute('SELECT card_version_id FROM card_versions WHERE card_version_id=?', (approved_a['publication']['card_version_id'],)).fetchone() is not None
    print({'version_a':approved_a['publication'],'unchanged':unchanged_run['counters']['unchanged'],'version_b':approved_b['publication'],'historical_a_retained':retained,'current_revision':db.published_cards(path)[0]})
