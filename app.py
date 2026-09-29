"""Tamil–English Document Intelligence: single-owner local-first service."""
import asyncio, hashlib, json, os, secrets, sqlite3, threading, uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from engine import extract, retrieve, summary
from model import generate, MODEL

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', str(ROOT/'storage')))
DATA.mkdir(parents=True,exist_ok=True)
DB = DATA/'documents.db'
TOKEN_FILE = DATA/'access-token'
if os.getenv('APP_TOKEN'):
    TOKEN = os.environ['APP_TOKEN']
else:
    if not TOKEN_FILE.exists():
        fd = os.open(TOKEN_FILE, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w') as f: f.write(secrets.token_urlsafe(32))
    TOKEN = TOKEN_FILE.read_text().strip()
if len(TOKEN) < 24: raise RuntimeError('APP_TOKEN must contain at least 24 characters')
ORIGIN = os.getenv('APP_ORIGIN','http://localhost:8000')
LIMIT = 16*1024*1024
pool = ThreadPoolExecutor(max_workers=1)
job_slots = threading.BoundedSemaphore(8)
ai_slot = threading.BoundedSemaphore(1)

@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback(); raise
    finally: con.close()

with db() as con:
    con.executescript('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, name TEXT, digest TEXT, language TEXT, force INTEGER, status TEXT, error TEXT, pages TEXT, created TEXT, archived INTEGER DEFAULT 0);')

def process(doc_id):
    try:
        with db() as con:
            row = con.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
            con.execute("UPDATE documents SET status='processing',error=NULL WHERE id=?",(doc_id,))
        pages = extract(DATA/(doc_id+'.bin'), row['language'], bool(row['force']))
        with db() as con: con.execute("UPDATE documents SET status='ready',pages=? WHERE id=?",(json.dumps(pages,ensure_ascii=False),doc_id))
    except Exception as exc:
        message = str(exc) if isinstance(exc,ValueError) else 'Extraction failed. Check the file format, OCR installation and scan quality.'
        with db() as con: con.execute("UPDATE documents SET status='failed',error=? WHERE id=?",(message[:500],doc_id))
    finally: job_slots.release()

@asynccontextmanager
async def lifespan(app):
    # Interrupted tasks remain visible and can be explicitly retried.
    with db() as con: con.execute("UPDATE documents SET status='failed',error='Processing interrupted by restart. Retry this document.' WHERE status IN ('queued','processing')")
    yield
    pool.shutdown(wait=True)

app = FastAPI(title='Tamil–English Document Intelligence',version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.middleware('http')
async def guard(request, call_next):
    if request.url.path.startswith('/api/') and request.url.path != '/api/health':
        provided = request.headers.get('authorization','').removeprefix('Bearer ')
        if not secrets.compare_digest(provided,TOKEN):
            return Response(json.dumps({'detail':'Enter your workspace access token'}),status_code=401,media_type='application/json')
    if request.method not in {'GET','HEAD','OPTIONS'}:
        origin = request.headers.get('origin')
        if origin and origin != ORIGIN: return Response('Origin not allowed',status_code=403)
    if request.headers.get('sec-fetch-site') == 'cross-site': return Response('Cross-site request rejected',status_code=403)
    # Reject oversized multipart bodies before parsing, including chunked bodies.
    if request.url.path == '/api/documents' and request.method == 'POST':
        size = 0
        parts = []
        async for part in request.stream():
            size += len(part)
            if size > LIMIT+1024*1024: return Response('Upload exceeds 16 MB',status_code=413)
            parts.append(part)
        body = b''.join(parts)
        sent = False
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type':'http.request','body':body,'more_body':False}
            return {'type':'http.disconnect'}
        request._receive = receive
        request._body = body
    response = await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
      'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",'Cache-Control':'no-store'})
    return response

def owner(request:Request):
    provided = request.headers.get('authorization','').removeprefix('Bearer ')
    if not secrets.compare_digest(provided,TOKEN): raise HTTPException(401,'Enter your workspace access token')

def document(doc_id):
    with db() as con: row = con.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
    if not row: raise HTTPException(404,'Document not found')
    return dict(row)

def ready(doc_id):
    row = document(doc_id)
    if row['status'] != 'ready': raise HTTPException(409,'Document is not ready')
    return json.loads(row['pages'])

@app.get('/')
def home(): return FileResponse(ROOT/'index.html')
@app.get('/app.js')
def js(): return FileResponse(ROOT/'app.js',media_type='text/javascript')
@app.get('/style.css')
def css(): return FileResponse(ROOT/'style.css',media_type='text/css')
@app.get('/api/health')
def health(): return {'status':'ok'}
@app.get('/api/status',dependencies=[Depends(owner)])
def status():
    import pytesseract
    try: languages = pytesseract.get_languages(config='')
    except Exception: languages=[]
    return {'ocrLanguages':languages,'modelConfigured':bool(MODEL),'model':MODEL or None,'maxMB':16,'maxPages':32}

@app.get('/api/documents',dependencies=[Depends(owner)])
def listing(archived:bool=False):
    with db() as con: rows = con.execute('SELECT id,name,language,status,error,created,archived FROM documents WHERE archived=? ORDER BY created DESC',(int(archived),)).fetchall()
    return [dict(r) for r in rows]

@app.post('/api/documents',status_code=202,dependencies=[Depends(owner)])
async def upload(file:UploadFile=File(...), language:Literal['tam+eng','tam','eng']=Form('tam+eng'), force:bool=Form(False)):
    if not job_slots.acquire(blocking=False): raise HTTPException(429,'Queue full. Wait for existing documents to finish')
    doc_id = uuid.uuid4().hex
    path = DATA/(doc_id+'.bin')
    submitted = False
    try:
        data = await file.read(LIMIT+1)
        if len(data)>LIMIT: raise HTTPException(413,'Maximum file size is 16 MB')
        if not data: raise HTTPException(400,'File is empty')
        name = Path((file.filename or 'document').replace('\\','/')).name[:160]
        if Path(name).suffix.lower() not in {'.pdf','.png','.jpg','.jpeg','.tif','.tiff','.webp'}: raise HTTPException(415,'Use PDF, PNG, JPEG, TIFF or WebP')
        path.write_bytes(data)
        with db() as con:
            con.execute('INSERT INTO documents(id,name,digest,language,force,status,created) VALUES(?,?,?,?,?,?,?)',
              (doc_id,name,hashlib.sha256(data).hexdigest(),language,int(force),'queued',datetime.now(timezone.utc).isoformat()))
        pool.submit(process,doc_id); submitted=True
        return {'id':doc_id,'status':'queued'}
    finally:
        await file.close()
        if not submitted:
            path.unlink(missing_ok=True); job_slots.release()

@app.get('/api/documents/{doc_id}',dependencies=[Depends(owner)])
def detail(doc_id:str):
    row = document(doc_id); row['pages'] = json.loads(row['pages'] or '[]'); return row

class Archive(BaseModel): archived:bool
@app.patch('/api/documents/{doc_id}',dependencies=[Depends(owner)])
def archive(doc_id:str, body:Archive):
    document(doc_id)
    with db() as con: con.execute('UPDATE documents SET archived=? WHERE id=?',(int(body.archived),doc_id))
    return {'ok':True}

@app.post('/api/documents/{doc_id}/retry',dependencies=[Depends(owner)])
def retry(doc_id:str):
    row = document(doc_id)
    if row['status'] != 'failed': raise HTTPException(409,'Only failed documents can be retried')
    if not job_slots.acquire(blocking=False): raise HTTPException(429,'Queue full')
    with db() as con:
        changed = con.execute("UPDATE documents SET status='queued' WHERE id=? AND status='failed'",(doc_id,)).rowcount
    if not changed:
        job_slots.release(); raise HTTPException(409,'Already queued')
    pool.submit(process,doc_id)
    return {'status':'queued'}

@app.get('/api/documents/{doc_id}/export',dependencies=[Depends(owner)])
def export(doc_id:str, format:Literal['txt','json']='txt'):
    pages=ready(doc_id)
    text = json.dumps(pages,ensure_ascii=False,indent=2) if format=='json' else '\n\n'.join(f"--- Page {p['number']} ---\n{p['text']}" for p in pages)
    return Response(text,media_type='application/json' if format=='json' else 'text/plain',headers={'Content-Disposition':f'attachment; filename="document.{format}"'})

class Query(BaseModel):
    query:str=Field(min_length=2,max_length=1000)
@app.post('/api/documents/{doc_id}/search',dependencies=[Depends(owner)])
def search(doc_id:str,body:Query): return {'results':retrieve(ready(doc_id),body.query)}
@app.get('/api/documents/{doc_id}/summary',dependencies=[Depends(owner)])
def extractive(doc_id:str): return {'mode':'extractive','sentences':summary(ready(doc_id))}

class AIRequest(BaseModel):
    action:Literal['question','summary','translate']
    language:Literal['Tamil','English']='English'
    question:str=Field(default='',max_length=1000)
    page:int|None=Field(default=None,ge=1)

@app.post('/api/documents/{doc_id}/ai',dependencies=[Depends(owner)])
def ai(doc_id:str,body:AIRequest):
    pages=ready(doc_id)
    if body.action=='question':
        if len(body.question.strip())<2: raise HTTPException(422,'Enter a question')
        evidence=retrieve(pages,body.question)
        if not evidence: return {'text':'No matching evidence found. Try keywords in the document language.','pages':[],'notice':'Lexical retrieval does not automatically translate your query.'}
        task='Answer using only evidence: '+body.question
    elif body.action=='translate':
        chosen=next((p for p in pages if p['number']==body.page),None)
        if not chosen: raise HTTPException(422,'Choose an existing page')
        if len(chosen['text'])>12000: raise HTTPException(422,'Page is too long for translation (12,000 character limit)')
        evidence=[{'page':chosen['number'],'text':chosen['text']}]; task='Translate the complete supplied page'
    else:
        evidence=summary(pages,12); task='Summarize these selected document excerpts. Do not imply full document coverage.'
    if not ai_slot.acquire(blocking=False): raise HTTPException(429,'An AI request is already running. Please wait')
    try: return generate(task,evidence,body.language)
    except ValueError as exc: raise HTTPException(503,str(exc)) from exc
    finally: ai_slot.release()
