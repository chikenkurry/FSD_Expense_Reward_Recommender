"""SQLite storage. Legacy scraper tables remain; v2 adds a normalized catalogue."""
from __future__ import annotations
import hashlib, json, sqlite3, uuid
from pathlib import Path
from typing import Any
from .util import json_dumps, utc_now

SCHEMA_VERSION=2
DEMO_BANK_ID='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'; DEMO_SOURCE_ID='33333333-3333-4333-8333-333333333333'
DEMO_DOCUMENT_ID='44444444-4444-4444-8444-444444444444'; DEMO_CARD_ID='11111111-1111-4111-8111-111111111111'
class _Connection(sqlite3.Connection):
 def __exit__(self,*a):
  try:return super().__exit__(*a)
  finally:self.close()
def connect(path:str)->sqlite3.Connection:
 Path(path).parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(path,factory=_Connection,timeout=5,isolation_level=None);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=5000');c.execute('PRAGMA journal_mode=WAL');return c
def initialize(path:str)->None:
 with connect(path) as c:
  c.executescript("""
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scrape_runs(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT,status TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS card_snapshots(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES scrape_runs(id),source_id TEXT NOT NULL,card_id TEXT NOT NULL,fetched_at TEXT NOT NULL,content_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_card_idx ON card_snapshots(card_id,id DESC);
CREATE TABLE IF NOT EXISTS current_cards(card_id TEXT PRIMARY KEY,snapshot_id INTEGER NOT NULL REFERENCES card_snapshots(id),source_id TEXT NOT NULL,issuer TEXT NOT NULL,payload_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE VIEW IF NOT EXISTS current_card_view AS SELECT card_id,source_id,issuer,payload_json,updated_at FROM current_cards;
CREATE TABLE IF NOT EXISTS banks(bank_id TEXT PRIMARY KEY CHECK(length(bank_id)=36),name TEXT NOT NULL,normalized_name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY CHECK(length(source_id)=36),bank_id TEXT REFERENCES banks(bank_id),name TEXT NOT NULL,domain TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('official_bank','official_terms','approved_comparison')),adapter_key TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),access_review_status TEXT NOT NULL CHECK(access_review_status IN ('pending','approved','rejected')),refresh_interval_hours INTEGER NOT NULL CHECK(refresh_interval_hours>0),last_attempt_at TEXT,last_success_at TEXT);
CREATE TABLE IF NOT EXISTS source_documents(document_id TEXT PRIMARY KEY CHECK(length(document_id)=36),source_id TEXT NOT NULL REFERENCES sources(source_id),label TEXT NOT NULL,document_type TEXT NOT NULL,path_key TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),fixture_text TEXT,UNIQUE(source_id,path_key));
CREATE TABLE IF NOT EXISTS ingestion_runs(run_id TEXT PRIMARY KEY CHECK(length(run_id)=36),source_id TEXT NOT NULL REFERENCES sources(source_id),scope_hash TEXT NOT NULL,scope_json TEXT NOT NULL,reason TEXT NOT NULL CHECK(reason IN ('scheduled_refresh','admin_refresh','parser_recheck')),status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','partial','failed')),created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,attempt INTEGER NOT NULL DEFAULT 0,lease_owner TEXT,lease_until TEXT,counters_json TEXT NOT NULL,error_summary TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS active_run_unique ON ingestion_runs(source_id,scope_hash) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS run_failures(failure_id INTEGER PRIMARY KEY,run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),document_id TEXT REFERENCES source_documents(document_id),stage TEXT NOT NULL,code TEXT NOT NULL,message TEXT NOT NULL,retryable INTEGER NOT NULL CHECK(retryable IN(0,1)));
CREATE TABLE IF NOT EXISTS raw_snapshots(snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id)=36),document_id TEXT NOT NULL REFERENCES source_documents(document_id),run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),content_hash TEXT NOT NULL,fetched_at TEXT NOT NULL,extracted_text TEXT NOT NULL,UNIQUE(document_id,content_hash));
CREATE TABLE IF NOT EXISTS extraction_candidates(candidate_id TEXT PRIMARY KEY CHECK(length(candidate_id)=36),scrape_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),source_id TEXT NOT NULL REFERENCES sources(source_id),bank_id TEXT NOT NULL REFERENCES banks(bank_id),card_id TEXT,base_card_version_id TEXT,review_status TEXT NOT NULL CHECK(review_status IN ('pending','approved','rejected')),review_revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,content_hash TEXT NOT NULL,parser_version TEXT NOT NULL,material_change INTEGER NOT NULL CHECK(material_change IN(0,1)),candidate_json TEXT NOT NULL,UNIQUE(source_id,content_hash,parser_version));
CREATE TABLE IF NOT EXISTS candidate_issues(issue_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),severity TEXT NOT NULL CHECK(severity IN ('error','warning')),code TEXT NOT NULL,field_path TEXT NOT NULL,message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_diffs(diff_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),operation TEXT NOT NULL CHECK(operation IN ('add','replace','remove')),field_path TEXT NOT NULL,old_value TEXT,new_value TEXT,material INTEGER NOT NULL CHECK(material IN(0,1)));
CREATE TABLE IF NOT EXISTS candidate_audit(audit_id INTEGER PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),action TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards(card_id TEXT PRIMARY KEY CHECK(length(card_id)=36),bank_id TEXT NOT NULL REFERENCES banks(bank_id),market TEXT NOT NULL,product_key TEXT NOT NULL,name TEXT NOT NULL,normalized_name TEXT NOT NULL,UNIQUE(bank_id,market,product_key));
CREATE TABLE IF NOT EXISTS card_versions(card_version_id TEXT PRIMARY KEY CHECK(length(card_version_id)=36),card_id TEXT NOT NULL REFERENCES cards(card_id),created_at TEXT NOT NULL,effective_from TEXT,effective_to TEXT,terms_json TEXT NOT NULL,summary_json TEXT NOT NULL,content_hash TEXT NOT NULL,immutable INTEGER NOT NULL DEFAULT 1 CHECK(immutable=1),UNIQUE(card_id,content_hash));
CREATE TABLE IF NOT EXISTS reward_rules(rule_id INTEGER PRIMARY KEY,card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),rule_key TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('cashback','miles','points')),rate TEXT NOT NULL,reward_unit TEXT NOT NULL,stacking_policy TEXT NOT NULL,period TEXT NOT NULL,rule_json TEXT NOT NULL,UNIQUE(card_version_id,rule_key));
CREATE TABLE IF NOT EXISTS evidence_links(evidence_id TEXT PRIMARY KEY CHECK(length(evidence_id)=36),card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),field_path TEXT NOT NULL,source_url TEXT NOT NULL,document_type TEXT NOT NULL,locator TEXT NOT NULL,snippet TEXT NOT NULL CHECK(length(snippet)<=2000),fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalogue_publications(catalogue_revision INTEGER PRIMARY KEY,card_id TEXT NOT NULL REFERENCES cards(card_id),card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),published_at TEXT NOT NULL,superseded_revision INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS current_publication_one_per_card ON catalogue_publications(card_id) WHERE superseded_revision IS NULL;
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY CHECK(length(decision_id)=36),candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),reason TEXT NOT NULL,actor TEXT NOT NULL,decided_at TEXT NOT NULL,publication_revision INTEGER);
CREATE TABLE IF NOT EXISTS idempotency_records(caller TEXT NOT NULL,endpoint TEXT NOT NULL,idem_key TEXT NOT NULL,body_hash TEXT NOT NULL,status INTEGER NOT NULL,headers_json TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,PRIMARY KEY(caller,endpoint,idem_key));
CREATE INDEX IF NOT EXISTS source_fresh_idx ON sources(enabled,access_review_status,last_success_at);CREATE INDEX IF NOT EXISTS candidates_queue_idx ON extraction_candidates(review_status,created_at DESC,candidate_id);CREATE INDEX IF NOT EXISTS version_lookup_idx ON card_versions(card_id,created_at DESC);CREATE INDEX IF NOT EXISTS idem_expiry_idx ON idempotency_records(expires_at);
""")
  c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)',(SCHEMA_VERSION,utc_now()))
def start_run(path:str,source_id:str)->int:
 initialize(path)
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');x=c.execute("INSERT INTO scrape_runs(source_id,started_at,status)VALUES(?,?,'running')",(source_id,utc_now()));c.commit();return int(x.lastrowid)
def finish_failure(path:str,run_id:int,error:str)->None:
 with connect(path) as c:c.execute("UPDATE scrape_runs SET completed_at=?,status='failed',error=? WHERE id=?",(utc_now(),error[:1000],run_id))
def finish_success(path:str,run_id:int,record:dict)->None:
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');p=json_dumps(record);x=c.execute('INSERT INTO card_snapshots(run_id,source_id,card_id,fetched_at,content_sha256,payload_json)VALUES(?,?,?,?,?,?)',(run_id,record['provenance']['source_id'],record['card_id'],record['provenance']['fetched_at'],record['provenance']['content_sha256'],p));c.execute("INSERT INTO current_cards(card_id,snapshot_id,source_id,issuer,payload_json,updated_at)VALUES(?,?,?,?,?,?) ON CONFLICT(card_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,source_id=excluded.source_id,issuer=excluded.issuer,payload_json=excluded.payload_json,updated_at=excluded.updated_at",(record['card_id'],x.lastrowid,record['provenance']['source_id'],record['issuer'],p,utc_now()));c.execute("UPDATE scrape_runs SET completed_at=?,status='success' WHERE id=?",(utc_now(),run_id));c.commit()
def cards(path:str,issuer:str|None=None,source_id:str|None=None)->list[dict]:
 initialize(path);cl=[];v=[]
 if issuer:cl+=['issuer=?'];v+=[issuer]
 if source_id:cl+=['source_id=?'];v+=[source_id]
 with connect(path) as c:return [json.loads(r['payload_json']) for r in c.execute('SELECT payload_json FROM current_card_view'+(' WHERE '+' AND '.join(cl) if cl else '')+' ORDER BY card_id',v)]
def card(path:str,card_id:str)->dict|None:
 initialize(path)
 with connect(path) as c:r=c.execute('SELECT payload_json FROM current_cards WHERE card_id=?',(card_id,)).fetchone()
 return json.loads(r['payload_json']) if r else None
def runs(path:str,limit:int=50)->list[dict]:
 initialize(path)
 with connect(path) as c:return [dict(x) for x in c.execute('SELECT id,source_id,started_at,completed_at,status,error FROM scrape_runs ORDER BY id DESC LIMIT ?',(max(1,min(limit,100)),))]
def _u()->str:return str(uuid.uuid4())
def _h(x:Any)->str:return hashlib.sha256(json_dumps(x).encode()).hexdigest()
def seed_demo(path:str,rate:str='0.015')->dict:
 initialize(path);text=f'Demo Everyday Card: {rate} cashback on eligible purchases. Annual fee SGD 0.00.'
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');c.execute('INSERT OR IGNORE INTO banks VALUES(?,?,?)',(DEMO_BANK_ID,'Demo Bank','demo bank'));c.execute("INSERT OR IGNORE INTO sources(source_id,bank_id,name,domain,kind,adapter_key,enabled,access_review_status,refresh_interval_hours)VALUES(?,?,?,?,?,?,?,?,?)",(DEMO_SOURCE_ID,DEMO_BANK_ID,'Demo Bank Official Cards (synthetic)','example.test','official_bank','demo_bank_v1',1,'approved',168));c.execute('INSERT OR REPLACE INTO source_documents VALUES(?,?,?,?,?,?,?)',(DEMO_DOCUMENT_ID,DEMO_SOURCE_ID,'Synthetic Demo Everyday terms','product_page','demo-everyday',1,text));c.commit()
 return {'bank_id':DEMO_BANK_ID,'source_id':DEMO_SOURCE_ID,'document_id':DEMO_DOCUMENT_ID,'rate':rate}
def source_list(path:str)->list[dict]:
 initialize(path)
 with connect(path) as c:r=c.execute("SELECT *,CASE WHEN last_success_at IS NULL THEN 'never_fetched' WHEN datetime(last_success_at)<datetime('now','-14 days') THEN 'stale' ELSE 'fresh' END freshness FROM sources ORDER BY name,source_id").fetchall()
 return [dict(x) for x in r]
def queue_run(path:str,source_id:str,scope:dict,reason:str)->dict:
 initialize(path);sh=_h(scope);now=utc_now();rid=_u()
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');s=c.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
  if not s or not s['enabled'] or s['access_review_status']!='approved':c.rollback();raise ValueError('SOURCE_NOT_ENABLED')
  active=c.execute("SELECT run_id FROM ingestion_runs WHERE source_id=? AND scope_hash=? AND status IN('queued','running')",(source_id,sh)).fetchone()
  if active:c.rollback();raise RuntimeError('SOURCE_ALREADY_RUNNING:'+active['run_id'])
  counts={'discovered':0,'fetched':0,'not_modified':0,'parsed':0,'candidates_created':0,'unchanged':0,'failed':0};c.execute('INSERT INTO ingestion_runs(run_id,source_id,scope_hash,scope_json,reason,status,created_at,counters_json)VALUES(?,?,?,?,?,?,?,?)',(rid,source_id,sh,json_dumps(scope),reason,'queued',now,json_dumps(counts)));c.commit()
 return {'run_id':rid,'source_id':source_id,'status':'queued','created_at':now,'status_url':'/api/v1/admin/scrape-runs/'+rid}
def claim_run(path:str,worker_id:str)->dict|None:
 initialize(path)
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute("SELECT * FROM ingestion_runs WHERE status='queued' OR(status='running' AND lease_until<?) ORDER BY created_at LIMIT 1",(utc_now(),)).fetchone()
  if not r:c.rollback();return None
  x=c.execute("UPDATE ingestion_runs SET status='running',started_at=COALESCE(started_at,?),attempt=attempt+1,lease_owner=?,lease_until=datetime('now','+60 seconds') WHERE run_id=? AND(status='queued' OR lease_until<?)",(utc_now(),worker_id,r['run_id'],utc_now())).rowcount
  if not x:c.rollback();return None
  c.commit();return dict(r)
def _payload(rate:str,rid:str,ch:str,base:dict|None)->dict:
 terms={'schema_version':'card_terms.v1','currency':'SGD','annual_fee':'0.00','annual_fee_status':'known','first_year_waiver':'not_applicable','eligibility':{'min_age':21,'income_requirements':[],'additional_conditions':[]},'rules':[{'rule_key':'base_cashback','kind':'cashback','rate':rate,'reward_unit':'SGD','match':{'category_keys':[],'mccs':[],'merchant_keys':[],'channels':[],'contactless':None,'countries':[],'currencies':[]},'excluded_mccs':[],'excluded_merchant_keys':[],'qualification_counter_key':None,'minimum_spend':None,'minimum_transactions':None,'stacking_policy':'base','period':'calendar_month','cap_group_keys':[],'rounding':{'stage':'period_rule_total','mode':'floor','decimal_places':2}}],'qualification_counters':[],'cap_groups':[],'benefits':[],'unsupported_reasons':[]}
 summary={'card_id':DEMO_CARD_ID,'name':'Demo Everyday Card','bank':{'bank_id':DEMO_BANK_ID,'name':'Demo Bank'},'network':'visa','market':'SG','reward_type':'cashback','currency':'SGD','annual_fee':'0.00','annual_fee_status':'known','headline':f'{float(rate)*100:g}% cashback on eligible purchases','simulation_support':'supported','freshness':'fresh','last_verified_at':utc_now()};old=base['terms']['rules'][0]['rate'] if base else None;diff=[] if old==rate else [{'operation':'add' if old is None else 'replace','field_path':'/terms/rules/0/rate','old_value':old,'new_value':rate,'material':True}];ev=[{'evidence_id':_u(),'field_path':'/terms/rules/0/rate','source_url':'https://example.test/cards/demo-everyday','document_type':'product_page','locator':'fixture:rate','snippet':f'Synthetic fixture rate {rate}.','fetched_at':utc_now()}]
 return {'scrape_run_id':rid,'proposed_identity':{'card_id':DEMO_CARD_ID if base else None,'bank_id':DEMO_BANK_ID,'market':'SG','product_key':'demo-everyday','match_status':'matched' if base else 'new_product'},'base_card_version_id':base['card_version_id'] if base else None,'card':summary,'terms':terms,'validation_issues':[],'diff':diff,'evidence':ev,'content_hash':ch,'parser_version':'fixture.v1','schema_version':'card_candidate.v1'}
def process_one(path:str,worker_id:str='worker')->dict|None:
 r=claim_run(path,worker_id)
 if not r:return None
 counts={'discovered':0,'fetched':0,'not_modified':0,'parsed':0,'candidates_created':0,'unchanged':0,'failed':0}
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');docs=c.execute('SELECT * FROM source_documents WHERE source_id=? AND enabled=1',(r['source_id'],)).fetchall();counts['discovered']=len(docs)
  for d in docs:
   if not d['fixture_text']:counts['failed']+=1;c.execute('INSERT INTO run_failures(run_id,document_id,stage,code,message,retryable)VALUES(?,?,?,?,?,0)',(r['run_id'],d['document_id'],'extract','NO_OFFLINE_FIXTURE','No retained fixture is configured.'));continue
   ch=hashlib.sha256(d['fixture_text'].encode()).hexdigest();counts['fetched']+=1
   if c.execute('SELECT 1 FROM raw_snapshots WHERE document_id=? AND content_hash=?',(d['document_id'],ch)).fetchone():counts['not_modified']+=1;counts['unchanged']+=1;continue
   c.execute('INSERT INTO raw_snapshots VALUES(?,?,?,?,?,?)',(_u(),d['document_id'],r['run_id'],ch,utc_now(),d['fixture_text']))
   import re;m=re.search(r'(0\.\d+|\d+(?:\.\d+)?)\s*(?:cashback|%)',d['fixture_text'],re.I);rate=m.group(1) if m and m.group(1).startswith('0.') else (str(float(m.group(1))/100) if m else None)
   if not rate:counts['failed']+=1;continue
   base=current_detail_conn(c,DEMO_CARD_ID);p=_payload(rate,r['run_id'],ch,base);cid=_u();now=utc_now()
   try:
    c.execute('INSERT INTO extraction_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(cid,r['run_id'],r['source_id'],DEMO_BANK_ID,DEMO_CARD_ID if base else None,base['card_version_id'] if base else None,'pending',1,now,now,ch,'fixture.v1',int(bool(p['diff'])),json_dumps(p)));[c.execute('INSERT INTO candidate_diffs(candidate_id,operation,field_path,old_value,new_value,material)VALUES(?,?,?,?,?,?)',(cid,x['operation'],x['field_path'],x['old_value'],x['new_value'],1)) for x in p['diff']];counts['parsed']+=1;counts['candidates_created']+=1
   except sqlite3.IntegrityError:counts['unchanged']+=1
  status='succeeded' if not counts['failed'] else ('partial' if counts['parsed'] else 'failed');c.execute('UPDATE ingestion_runs SET status=?,finished_at=?,lease_owner=NULL,lease_until=NULL,counters_json=?,error_summary=? WHERE run_id=?',(status,utc_now(),json_dumps(counts),'One or more documents failed.' if counts['failed'] else None,r['run_id']));c.commit()
 return run(path,r['run_id'])
def run(path:str,rid:str)->dict|None:
 initialize(path)
 with connect(path) as c:r=c.execute('SELECT * FROM ingestion_runs WHERE run_id=?',(rid,)).fetchone();f=[dict(x) for x in c.execute('SELECT document_id,stage,code,message,retryable FROM run_failures WHERE run_id=?',(rid,))]
 if not r:return None
 x=dict(r);x['counters']=json.loads(x.pop('counters_json'));x['failures']=f;return x
def current_detail_conn(c:sqlite3.Connection,cid:str)->dict|None:
 r=c.execute("SELECT p.catalogue_revision,p.card_version_id,v.terms_json,v.summary_json,v.effective_from,v.effective_to FROM catalogue_publications p JOIN card_versions v ON v.card_version_id=p.card_version_id WHERE p.card_id=? AND p.superseded_revision IS NULL",(cid,)).fetchone()
 if not r:return None
 x=dict(r);x['terms']=json.loads(x.pop('terms_json'));x['card']=json.loads(x.pop('summary_json'));x['evidence']=[dict(e) for e in c.execute('SELECT evidence_id,field_path,source_url,document_type,locator,snippet,fetched_at FROM evidence_links WHERE card_version_id=?',(x['card_version_id'],))];return x
def current_detail(path:str,cid:str)->dict|None:
 initialize(path)
 with connect(path) as c:return current_detail_conn(c,cid)
def catalogue_revision(c:sqlite3.Connection)->int:return int(c.execute('SELECT COALESCE(MAX(catalogue_revision),0) x FROM catalogue_publications').fetchone()['x'])
def published_cards(path:str,filters:dict|None=None)->tuple[int,list[dict]]:
 initialize(path);filters=filters or {}
 with connect(path) as c:r=c.execute("SELECT p.card_version_id,v.summary_json FROM catalogue_publications p JOIN card_versions v ON v.card_version_id=p.card_version_id WHERE p.superseded_revision IS NULL ORDER BY json_extract(v.summary_json,'$.bank.name'),json_extract(v.summary_json,'$.name'),p.card_id").fetchall();rev=catalogue_revision(c)
 out=[]
 for x in r:
  a=json.loads(x['summary_json']);a['card_version_id']=x['card_version_id']
  if filters.get('q') and filters['q'].lower() not in (a['name']+' '+a['bank']['name']).lower():continue
  if filters.get('bank_id') and a['bank']['bank_id']!=filters['bank_id']:continue
  if any(filters.get(k) and a.get(k)!=filters[k] for k in ('reward_type','simulation_support','freshness')):continue
  out.append(a)
 return rev,out
def candidate_list(path:str,filters:dict|None=None)->list[dict]:
 initialize(path);filters=filters or {};q='SELECT candidate_json,candidate_id,review_status,review_revision,created_at,updated_at FROM extraction_candidates';cl=[];v=[]
 for k in ('review_status','source_id','scrape_run_id','bank_id'):
  if filters.get(k):cl+=[k+'=?'];v+=[filters[k]]
 if filters.get('material_change') is not None:cl+=['material_change=?'];v+=[int(filters['material_change'])]
 with connect(path) as c:r=c.execute(q+(' WHERE '+' AND '.join(cl) if cl else '')+' ORDER BY created_at DESC,candidate_id',v).fetchall()
 out=[]
 for x in r:a=json.loads(x['candidate_json']);a.update({k:x[k] for k in ('candidate_id','review_status','review_revision','created_at','updated_at')});out.append(a)
 return out
def get_candidate(path:str,cid:str)->dict|None:return next((x for x in candidate_list(path) if x['candidate_id']==cid),None)
def patch_candidate(path:str,cid:str,expected:int,edits:list[dict],reason:str,actor:str)->dict:
 allowed={'/terms/rules/0/rate':str,'/terms/annual_fee':(str,type(None)),'/card/headline':str}
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM extraction_candidates WHERE candidate_id=?',(cid,)).fetchone()
  if not r:c.rollback();raise LookupError('NOT_FOUND')
  if r['review_status']!='pending':c.rollback();raise RuntimeError('CANDIDATE_NOT_PENDING')
  if r['review_revision']!=expected:c.rollback();raise RuntimeError('REVISION_CONFLICT:'+str(r['review_revision']))
  p=json.loads(r['candidate_json'])
  for e in edits:
   k=e['field_path'];val=e['value']
   if k not in allowed or not isinstance(val,allowed[k]):c.rollback();raise ValueError('VALIDATION_ERROR')
   t=p;z=k.strip('/').split('/')
   for q in z[:-1]:t=t[int(q)] if q.isdigit() else t[q]
   t[int(z[-1]) if z[-1].isdigit() else z[-1]]=val
  now=utc_now();c.execute('UPDATE extraction_candidates SET review_revision=?,updated_at=?,candidate_json=? WHERE candidate_id=?',(expected+1,now,json_dumps(p),cid));c.execute('INSERT INTO candidate_audit(candidate_id,action,actor,at,payload_json)VALUES(?,?,?,?,?)',(cid,'patch',actor,now,json_dumps({'edits':edits,'reason':reason})));c.commit()
 return get_candidate(path,cid) or {}
def decide(path:str,cid:str,expected:int,decision:str,reason:str,acks:list[str],actor:str)->tuple[int,dict]:
 with connect(path) as c:
  c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM extraction_candidates WHERE candidate_id=?',(cid,)).fetchone()
  if not r:c.rollback();raise LookupError('NOT_FOUND')
  if r['review_status']!='pending':c.rollback();raise RuntimeError('CANDIDATE_NOT_PENDING')
  if r['review_revision']!=expected:c.rollback();raise RuntimeError('REVISION_CONFLICT:'+str(r['review_revision']))
  p=json.loads(r['candidate_json']);issues=[dict(x) for x in c.execute('SELECT severity,code FROM candidate_issues WHERE candidate_id=?',(cid,))]
  if decision=='approve' and (any(x['severity']=='error' or(x['severity']=='warning' and x['code'] not in acks) for x in issues) or p['proposed_identity']['match_status']=='needs_review'):c.rollback();raise ValueError('PUBLICATION_VALIDATION_FAILED')
  did=_u();now=utc_now();pub=None
  if decision=='approve':
   cardid=p['proposed_identity'].get('card_id') or DEMO_CARD_ID;vid=_u();rev=catalogue_revision(c)+1;c.execute('INSERT OR IGNORE INTO cards VALUES(?,?,?,?,?,?)',(cardid,DEMO_BANK_ID,'SG','demo-everyday',p['card']['name'],p['card']['name'].lower()));summary={**p['card'],'card_id':cardid,'card_version_id':vid};c.execute('INSERT INTO card_versions VALUES(?,?,?,?,?,?,?,?,?)',(vid,cardid,now,None,None,json_dumps(p['terms']),json_dumps(summary),p['content_hash'],1));
   for rule in p['terms']['rules']:c.execute('INSERT INTO reward_rules(card_version_id,rule_key,kind,rate,reward_unit,stacking_policy,period,rule_json)VALUES(?,?,?,?,?,?,?,?)',(vid,rule['rule_key'],rule['kind'],rule['rate'],rule['reward_unit'],rule['stacking_policy'],rule['period'],json_dumps(rule)))
   for e in p['evidence']:c.execute('INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?,?)',(e['evidence_id'],vid,e['field_path'],e['source_url'],e['document_type'],e['locator'],e['snippet'],e['fetched_at']))
   old=c.execute('SELECT catalogue_revision FROM catalogue_publications WHERE card_id=? AND superseded_revision IS NULL',(cardid,)).fetchone()
   if old:c.execute('UPDATE catalogue_publications SET superseded_revision=? WHERE catalogue_revision=?',(rev,old['catalogue_revision']))
   c.execute('INSERT INTO catalogue_publications VALUES(?,?,?,?,NULL)',(rev,cardid,vid,now));pub={'card_id':cardid,'card_version_id':vid,'catalogue_revision':rev}
  c.execute('UPDATE extraction_candidates SET review_status=?,updated_at=? WHERE candidate_id=?',('approved' if decision=='approve' else 'rejected',now,cid));c.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?)',(did,cid,decision,reason,actor,now,pub['catalogue_revision'] if pub else None));c.commit()
 return 201,{'decision_id':did,'candidate_id':cid,'decision':decision,'decided_at':now,'publication':pub}
def idempotency_get(path:str,caller:str,endpoint:str,key:str,bh:str):
 with connect(path) as c:r=c.execute('SELECT * FROM idempotency_records WHERE caller=? AND endpoint=? AND idem_key=? AND expires_at>?',(caller,endpoint,key,utc_now())).fetchone()
 if not r:return None
 if r['body_hash']!=bh:raise RuntimeError('IDEMPOTENCY_CONFLICT')
 return r['status'],json.loads(r['headers_json']),json.loads(r['response_json'])
def idempotency_put(path:str,caller:str,endpoint:str,key:str,bh:str,status:int,headers:dict,response:dict)->None:
 import datetime
 exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=24)).replace(microsecond=0).isoformat().replace('+00:00','Z')
 with connect(path) as c:c.execute('INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?,?,?)',(caller,endpoint,key,bh,status,json_dumps(headers),json_dumps(response),utc_now(),exp))
