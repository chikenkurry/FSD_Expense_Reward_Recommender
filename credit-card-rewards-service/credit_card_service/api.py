"""Dependency-free HTTP API with explicit boundary validation and no secret logging."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from . import db
from .config import ConfigError, selected_sources
from .scraper import scrape_sources

MAX_BODY=64_000
UUID=lambda x: _uuid(x)
def _uuid(v:object)->str|None:
 try:
  value=str(v); return value if str(uuid.UUID(value))==value else None
 except (ValueError,TypeError,AttributeError): return None
def _json(value:object)->bytes:return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def _etag(revision:int, representation:str)->str:return '"catalogue-'+str(revision)+'-'+hashlib.sha256(representation.encode()).hexdigest()[:12]+'"'
def _cursor(secret:str, filters:dict, revision:int, offset:int)->str:
 payload={'f':filters,'r':revision,'o':offset,'e':int(time.time())+900}; raw=_json(payload); sig=hmac.new(secret.encode(),raw,hashlib.sha256).digest();return base64.urlsafe_b64encode(raw+b'.'+sig).decode().rstrip('=')
def _decode_cursor(secret:str, token:str, filters:dict, revision:int)->int:
 try:
  raw=base64.urlsafe_b64decode(token+'='*(-len(token)%4)); body,sig=raw.rsplit(b'.',1)
  if not hmac.compare_digest(sig,hmac.new(secret.encode(),body,hashlib.sha256).digest()):raise ValueError
  p=json.loads(body)
  if p['f']!=filters:raise LookupError('CURSOR_FILTER_MISMATCH')
  if p['e']<time.time() or p['r']!=revision:raise RuntimeError('CURSOR_EXPIRED')
  return int(p['o'])
 except (ValueError,KeyError,json.JSONDecodeError,TypeError):raise ValueError('VALIDATION_ERROR')

def make_handler(config:dict, db_path:str, api_key:str|None, allow_private_hosts:bool, *, admin_secret:str|None=None, internal_secret:str|None=None):
 """`api_key` is a backwards-compatible admin secret alias, never an internal key."""
 if api_key=='':raise ValueError('api_key must be nonempty when supplied')
 admin_secret=admin_secret or api_key or os.getenv('CARD_ADMIN_BEARER')
 internal_secret=internal_secret or os.getenv('CARD_INTERNAL_BEARER')
 cursor_secret=os.getenv('CARD_CURSOR_HMAC_SECRET') or admin_secret or 'local-development-cursor-key'
 class Handler(BaseHTTPRequestHandler):
  server_version='CardCatalogue/1.0'
  def log_message(self,format:str,*args)->None:return
  def _rid(self)->str:
   incoming=self.headers.get('X-Request-ID','');return incoming if _uuid(incoming) else str(uuid.uuid4())
  def _send(self,status:int,payload:object|None,headers:dict|None=None)->None:
   raw=b'' if payload is None else _json(payload);self.send_response(status);self.send_header('X-Request-ID',self.request_id);self.send_header('Content-Length',str(len(raw)))
   if payload is not None:self.send_header('Content-Type','application/json; charset=utf-8')
   for k,v in (headers or {}).items():self.send_header(k,v)
   self.end_headers()
   if raw:self.wfile.write(raw)
  def _error(self,status:int,code:str,message:str,details:list|None=None)->None:self._send(status,{'error':{'code':code,'message':message,'details':details or [],'request_id':self.request_id}})
  def _auth(self,kind:str)->str|None:
   secret=admin_secret if kind=='admin' else internal_secret
   auth=self.headers.get('Authorization',''); token=auth[7:] if auth.startswith('Bearer ') else ''
   if not secret:self._error(503,'SERVICE_UNAVAILABLE','Required server identity is not configured.');return None
   if not hmac.compare_digest(token,secret):self._error(401,'UNAUTHENTICATED','Missing or invalid bearer token.');return None
   return kind
  def _body(self,required:set[str],optional:set[str]=set())->dict|None:
   typ=self.headers.get('Content-Type','').split(';',1)[0].lower()
   if typ!='application/json':self._error(415,'UNSUPPORTED_MEDIA_TYPE','JSON Content-Type is required.');return None
   try:
    length=int(self.headers.get('Content-Length','0'))
    if length<0 or length>MAX_BODY:raise ValueError
    value=json.loads(self.rfile.read(length).decode())
    if not isinstance(value,dict) or set(value)-required-optional or not required<=set(value):raise TypeError
    return value
   except (ValueError,TypeError,json.JSONDecodeError):self._error(400,'MALFORMED_REQUEST','Malformed JSON request.');return None
  def _page(self,items:list,filters:dict,revision:int,default:int=20)->dict|None:
   q=parse_qs(urlparse(self.path).query); raw=(q.get('limit') or [str(default)])[0]
   try:limit=int(raw);assert 1<=limit<=100
   except (ValueError,AssertionError):self._error(422,'VALIDATION_ERROR','limit must be an integer from 1 to 100.');return None
   try:offset=_decode_cursor(cursor_secret,(q.get('cursor') or [''])[0],filters,revision) if q.get('cursor') else 0
   except LookupError:self._error(422,'CURSOR_FILTER_MISMATCH','Cursor is bound to different filters.');return None
   except RuntimeError:self._error(409,'CURSOR_EXPIRED','Cursor expired or catalogue changed.');return None
   except ValueError:self._error(422,'VALIDATION_ERROR','Invalid cursor.');return None
   selected=items[offset:offset+limit];next_cursor=_cursor(cursor_secret,filters,revision,offset+limit) if offset+limit<len(items) else None;return {'items':selected,'next_cursor':next_cursor}
  def _conditional(self,revision:int,representation:str)->bool:
   tag=_etag(revision,representation)
   if self.headers.get('If-None-Match')==tag:self._send(304,None,{'ETag':tag});return True
   self.response_etag=tag;return False
  def do_GET(self)->None:
   self.request_id=self._rid();self.response_etag=None;p=urlparse(self.path);q=parse_qs(p.query)
   try:
    if p.path=='/health':db.initialize(db_path);self._send(200,{'status':'ok','database':'ready'});return
    if p.path=='/metrics':self._send(200,{'service':'card_catalogue','mode':'sqlite','note':'Prometheus text is available at /metrics/prometheus'});return
    if p.path=='/metrics/prometheus':self._send(200,None,{'Content-Type':'text/plain; version=0.0.4','Cache-Control':'no-store'});return
    # Legacy compatibility endpoints retain their original response shape.
    if p.path=='/v1/cards':self._send(200,{'cards':db.cards(db_path,(q.get('issuer')or[None])[0],(q.get('source_id')or[None])[0])});return
    if p.path.startswith('/v1/cards/'):
     item=db.card(db_path,unquote(p.path[10:]));self._send(200,item) if item else self._error(404,'NOT_FOUND','Card not found.');return
    if p.path=='/v1/scrape-runs':self._send(200,{'scrape_runs':db.runs(db_path,int((q.get('limit')or['50'])[0]))});return
    if p.path=='/api/v1/cards':
     filters={k:(q.get(k)or[None])[0] for k in ('q','bank_id','reward_type','simulation_support','freshness')};filters={k:v for k,v in filters.items() if v is not None}
     if filters.get('bank_id') and not _uuid(filters['bank_id']):self._error(422,'VALIDATION_ERROR','bank_id must be UUID.');return
     if filters.get('q') and len(filters['q'])>100:self._error(422,'VALIDATION_ERROR','q is too long.');return
     if any(filters.get(k) not in values for k,values in {'reward_type':{'cashback','miles','points','mixed'},'simulation_support':{'supported','unsupported'},'freshness':{'fresh','stale'}}.items() if filters.get(k)):self._error(422,'VALIDATION_ERROR','Invalid filter enum.');return
     rev,items=db.published_cards(db_path,filters)
     if self._conditional(rev,'cards:'+json.dumps(filters,sort_keys=True)):return
     page=self._page(items,filters,rev)
     if page:self._send(200,{'catalogue_revision':rev,**page},{'ETag':self.response_etag});return
    if p.path.startswith('/api/v1/cards/'):
     cid=unquote(p.path[len('/api/v1/cards/'):])
     if not _uuid(cid):self._error(422,'VALIDATION_ERROR','card_id must be UUID.');return
     item=db.current_detail(db_path,cid)
     if not item:self._error(404,'NOT_FOUND','Card is unavailable.');return
     if self._conditional(item['catalogue_revision'],'detail:'+cid):return
     self._send(200,{'catalogue_revision':item['catalogue_revision'],'card':item['card'],'terms':item['terms'],'effective_from':item['effective_from'],'effective_to':item['effective_to'],'evidence':item['evidence']},{'ETag':self.response_etag});return
    if p.path=='/api/v1/internal/catalogue-snapshots/current':
     if not self._auth('internal'):return
     schema=(q.get('schema_version')or['card_terms.v1'])[0];support=(q.get('simulation_support')or['supported'])[0];market=(q.get('market')or['SG'])[0];reward=(q.get('reward_type')or[None])[0]
     if schema!='card_terms.v1' or support not in {'supported','all'}:self._error(422,'UNSUPPORTED_SCHEMA_VERSION','Unsupported schema or support filter.');return
     rev,items=db.published_cards(db_path,{'reward_type':reward} if reward else {});details=[db.current_detail(db_path,x['card_id']) for x in items if support=='all' or x['simulation_support']=='supported'];payload={'schema_version':'card_catalogue_snapshot.v1','card_terms_schema_version':'card_terms.v1','catalogue_revision':rev,'generated_at':__import__('credit_card_service.util',fromlist=['utc_now']).utc_now(),'cards':[{'card':x['card'],'terms':x['terms'],'effective_from':x['effective_from'],'effective_to':x['effective_to'],'evidence':x['evidence']} for x in sorted((x for x in details if x and x['card']['market']==market),key=lambda x:x['card']['card_id'])]}
     if self._conditional(rev,'snapshot:'+schema+support+market+str(reward)):return
     self._send(200,payload,{'ETag':self.response_etag,'Cache-Control':'no-store'});return
    if p.path.startswith('/api/v1/internal/card-versions/'):
     if not self._auth('internal'):return
     vid=unquote(p.path.rsplit('/',1)[-1]);
     if not _uuid(vid):self._error(422,'VALIDATION_ERROR','card_version_id must be UUID.');return
     with db.connect(db_path) as c:r=c.execute("SELECT p.catalogue_revision,p.superseded_revision,v.* FROM card_versions v JOIN catalogue_publications p ON p.card_version_id=v.card_version_id WHERE v.card_version_id=?",(vid,)).fetchone();ev=[dict(x) for x in c.execute('SELECT evidence_id,field_path,source_url,document_type,locator,snippet,fetched_at FROM evidence_links WHERE card_version_id=?',(vid,))]
     if not r:self._error(404,'NOT_FOUND','Version unavailable.');return
     self._send(200,{'published_in_catalogue_revision':r['catalogue_revision'],'superseded_in_catalogue_revision':r['superseded_revision'],'card':json.loads(r['summary_json']),'terms':json.loads(r['terms_json']),'effective_from':r['effective_from'],'effective_to':r['effective_to'],'evidence':ev},{'Cache-Control':'no-store'});return
    if p.path=='/api/v1/admin/sources':
     if not self._auth('admin'):return
     rows=db.source_list(db_path);filters={k:(q.get(k)or[None])[0] for k in ('kind','access_review_status','freshness')};filters={k:v for k,v in filters.items() if v is not None};rows=[x for x in rows if all(str(x.get(k))==str(v) for k,v in filters.items())];page=self._page(rows,filters,0);self._send(200,page,{'Cache-Control':'no-store'});return
    if p.path.startswith('/api/v1/admin/scrape-runs/'):
     if not self._auth('admin'):return
     rid=unquote(p.path.rsplit('/',1)[-1]);item=db.run(db_path,rid)
     if not _uuid(rid):self._error(422,'VALIDATION_ERROR','run_id must be UUID.');return
     self._send(200,item,{'Cache-Control':'no-store'}) if item else self._error(404,'NOT_FOUND','Run unavailable.');return
    if p.path=='/api/v1/admin/extraction-candidates':
     if not self._auth('admin'):return
     filters={k:(q.get(k)or[None])[0] for k in ('review_status','source_id','scrape_run_id','bank_id','material_change')};filters={k:(v=='true' if k=='material_change' else v) for k,v in filters.items() if v is not None};items=db.candidate_list(db_path,filters);page=self._page(items,filters,0,10);self._send(200,page,{'Cache-Control':'no-store'});return
    self._error(404,'NOT_FOUND','Route not found.')
   except (ValueError,ConfigError):self._error(400,'MALFORMED_REQUEST','Invalid request.');
   except Exception:
    if p.path in {'/v1/cards','/v1/scrape-runs'} or p.path.startswith('/v1/cards/'):
     self._error(500,'INTERNAL_ERROR','database query failed')
    else:
     self._error(500,'INTERNAL_ERROR','Request could not be completed.')
  def do_POST(self)->None:
   self.request_id=self._rid();p=urlparse(self.path).path
   # Legacy endpoint remains synchronous for scripting compatibility.
   if p=='/v1/scrape':
    if api_key and not self._auth('admin'):return
    body=self._body(set(),{'source_ids'})
    if body is None:return
    try:
     ids=body.get('source_ids');
     if ids is not None and(not isinstance(ids,list) or not all(isinstance(x,str) for x in ids)):raise ValueError
     self._send(200,{'status':'completed','results':scrape_sources(selected_sources(config,ids),db_path,allow_private_hosts)})
    except Exception:self._error(400,'MALFORMED_REQUEST','Invalid scrape request.')
    return
   if p=='/api/v1/cards/compare':
    body=self._body({'card_ids'})
    if body is None:return
    ids=body['card_ids']
    if not isinstance(ids,list) or not 2<=len(ids)<=4 or len(set(ids))!=len(ids) or not all(_uuid(x) for x in ids):self._error(422,'VALIDATION_ERROR','card_ids must contain 2-4 distinct UUIDs.');return
    cards=[db.current_detail(db_path,x) for x in ids]
    if any(x is None for x in cards):self._error(404,'NOT_FOUND','A requested card is unavailable.');return
    rev=cards[0]['catalogue_revision'];fields={'annual_fee':lambda x:x['terms']['annual_fee'],'first_year_waiver':lambda x:x['terms']['first_year_waiver'],'eligibility':lambda x:x['terms']['eligibility'],'rewards':lambda x:x['terms']['rules'],'caps':lambda x:x['terms']['cap_groups'],'benefits':lambda x:x['terms']['benefits']};diff=[{'field':k,'values':[{'card_id':x['card']['card_id'],'display_value':json.dumps(fn(x),sort_keys=True)} for x in cards]} for k,fn in fields.items() if len({json.dumps(fn(x),sort_keys=True) for x in cards})>1];self._send(200,{'catalogue_revision':rev,'cards':[{'catalogue_revision':x['catalogue_revision'],'card':x['card'],'terms':x['terms'],'effective_from':x['effective_from'],'effective_to':x['effective_to'],'evidence':x['evidence']} for x in cards],'differences':diff});return
   if p=='/api/v1/admin/scrape-runs':
    if not self._auth('admin'):return
    body=self._body({'source_id','scope','reason'});key=self.headers.get('Idempotency-Key','')
    if body is None:return
    if not key or len(key)>255:self._error(422,'VALIDATION_ERROR','Idempotency-Key is required.');return
    try:
     if not _uuid(body['source_id']) or body['reason'] not in {'scheduled_refresh','admin_refresh','parser_recheck'} or not isinstance(body['scope'],dict) or body['scope'].get('type') not in {'all','documents'}:raise ValueError
     bh=hashlib.sha256(_json(body)).hexdigest();old=db.idempotency_get(db_path,'admin',p,key,bh)
     if old:self._send(old[0],old[2],old[1]);return
     result=db.queue_run(db_path,body['source_id'],body['scope'],body['reason']);headers={'Cache-Control':'no-store'};db.idempotency_put(db_path,'admin',p,key,bh,202,headers,result);self._send(202,result,headers)
    except RuntimeError as e:self._error(409,'IDEMPOTENCY_CONFLICT' if str(e)=='IDEMPOTENCY_CONFLICT' else 'SOURCE_ALREADY_RUNNING','Request conflicts with existing state.',[{'run_id':str(e).split(':')[-1]}])
    except ValueError:self._error(422,'VALIDATION_ERROR','Invalid registered source or scope.')
    return
   if p.endswith('/decisions') and p.startswith('/api/v1/admin/extraction-candidates/'):
    if not self._auth('admin'):return
    cid=p.split('/')[-2];body=self._body({'expected_review_revision','decision','reason','acknowledged_warning_codes'});key=self.headers.get('Idempotency-Key','')
    if body is None:return
    if not key or not _uuid(cid) or body.get('decision') not in {'approve','reject'} or not isinstance(body.get('expected_review_revision'),int) or not isinstance(body.get('reason'),str) or not 1<=len(body['reason'])<=1000 or not isinstance(body.get('acknowledged_warning_codes'),list):self._error(422,'VALIDATION_ERROR','Invalid decision request.');return
    try:
     bh=hashlib.sha256(_json(body)).hexdigest();old=db.idempotency_get(db_path,'admin',p,key,bh)
     if old:self._send(old[0],old[2],old[1]);return
     status,result=db.decide(db_path,cid,body['expected_review_revision'],body['decision'],body['reason'],body['acknowledged_warning_codes'],'admin');headers={'Cache-Control':'no-store'};db.idempotency_put(db_path,'admin',p,key,bh,status,headers,result);self._send(status,result,headers)
    except LookupError:self._error(404,'NOT_FOUND','Candidate unavailable.')
    except RuntimeError as e:self._error(409,str(e).split(':')[0],'Review state changed.')
    except ValueError:self._error(422,'PUBLICATION_VALIDATION_FAILED','Candidate cannot be published.')
    return
   self._error(404,'NOT_FOUND','Route not found.')
  def do_PATCH(self)->None:
   self.request_id=self._rid();p=urlparse(self.path).path
   if not(p.startswith('/api/v1/admin/extraction-candidates/') and p.count('/')==5):self._error(404,'NOT_FOUND','Route not found.');return
   if not self._auth('admin'):return
   cid=p.rsplit('/',1)[-1];body=self._body({'expected_review_revision','edits','reason'})
   if body is None:return
   if not _uuid(cid) or not isinstance(body['expected_review_revision'],int) or not isinstance(body['edits'],list) or not 1<=len(body['edits'])<=50 or not isinstance(body['reason'],str):self._error(422,'VALIDATION_ERROR','Invalid candidate patch.');return
   if any(not isinstance(x,dict) or set(x)!={'field_path','value'} for x in body['edits']):self._error(422,'VALIDATION_ERROR','Invalid candidate edits.');return
   try:self._send(200,db.patch_candidate(db_path,cid,body['expected_review_revision'],body['edits'],body['reason'],'admin'),{'Cache-Control':'no-store'})
   except LookupError:self._error(404,'NOT_FOUND','Candidate unavailable.')
   except RuntimeError as e:self._error(409,str(e).split(':')[0],'Review state changed.')
   except ValueError:self._error(422,'VALIDATION_ERROR','Invalid candidate edits.')
 def do_DELETE(self):self.request_id=self._rid();self._error(404,'NOT_FOUND','Route not found.')
 return Handler

def serve(config:dict,db_path:str,host:str='127.0.0.1',port:int=8080,api_key:str|None=None,allow_private_hosts:bool=False,*,admin_secret:str|None=None,internal_secret:str|None=None)->None:
 db.initialize(db_path);server=ThreadingHTTPServer((host,port),make_handler(config,db_path,api_key,allow_private_hosts,admin_secret=admin_secret,internal_secret=internal_secret))
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
