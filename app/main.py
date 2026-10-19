from contextlib import asynccontextmanager, closing
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse
import os
import asyncio
import threading
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, StrictInt
from typing import Annotated
from . import analytics, chat, predictor, store, npc_store, npc_predictor, system_activity, names, historical_predictor, battles

@asynccontextmanager
async def lifespan(app):
    store.init_db()
    npc_store.init_db()
    resolver = names.start() if names.enabled() else None
    try:
        yield
    finally:
        if resolver:
            resolver[0].set()
            await asyncio.to_thread(resolver[1].join, 15)

app = FastAPI(title='New Eden Intel',version='0.2.0',lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.getenv('ALLOWED_HOSTS','localhost,127.0.0.1,[::1],testserver').split(','))
train_lock = threading.Lock()
chat_lock = threading.Lock()
npc_train_lock = threading.Lock()

@app.middleware('http')
async def guard(request: Request, call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail':'Cross-origin writes are disabled.'},status_code=403)
        if not request.headers.get('content-type','').lower().startswith('application/json'):
            return JSONResponse({'detail':'application/json is required.'},status_code=415)
        # Bound body before parsing, including chunked requests.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>8192: return JSONResponse({'detail':'Request body too large.'},status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

class Kind(str,Enum):
    character='character'
    corporation='corporation'
    alliance='alliance'

class ChatRequest(BaseModel):
    question: str = Field(min_length=1,max_length=2000)
    entity_kind: Kind | None = None
    entity_id: int | None = Field(default=None,gt=0)

@app.get('/api/health')
def health(): return {'status':'ok'}

@app.get('/api/status')
def status():
    return {'data_mode':store.get_state('data_mode',store.get_state('npc_data_mode','empty')),'llm':chat.llm_status(),
            'collector':analytics.collector_status(),'model':predictor.model_status(),'names':names.status()}

@app.get('/api/overview')
def overview(days:int=Query(30,ge=1,le=365)): return analytics.overview(days)

@app.get('/api/entities')
def entities(q:str=Query('',max_length=100)):
    with closing(store.connect()) as conn:
        rows=conn.execute("SELECT id,name,category FROM entities WHERE name LIKE ? OR CAST(id AS TEXT)=? ORDER BY name LIMIT 30",('%'+q+'%',q)).fetchall()
    return {'items':[dict(r) for r in rows]}

@app.get('/api/profile/{kind}/{entity_id}')
def profile(kind:Kind,entity_id:int,days:int=Query(30,ge=1,le=365)):
    if entity_id<=0: raise HTTPException(422,'Entity ID must be positive.')
    return analytics.profile(kind.value,entity_id,days)

@app.get('/api/doctrines')
def doctrines(days:int=Query(30,ge=1,le=365)): return analytics.doctrines(days)

@app.get('/api/forecast')
def forecast(days:int=Query(90,ge=1,le=90)): return predictor.forecast(days)

class TrainingRequest(BaseModel):
    system_ids: list[Annotated[StrictInt, Field(gt=0)]] | None = Field(default=None, min_length=1, max_length=12)


@app.get('/api/training-scope')
def training_scope(mode:str=Query("live",pattern="^(live|historical)$"), days:int=Query(42,ge=21,le=90)):
    from .training import scope_preview
    try: return scope_preview(mode, days)
    except ValueError as e: raise HTTPException(400,str(e))


@app.post('/api/train')
def train(body:TrainingRequest = TrainingRequest(), mode:str=Query("live",pattern="^(live|historical)$"),days:int=Query(42,ge=21,le=90)):
    if not train_lock.acquire(blocking=False): raise HTTPException(409,'Training is already running.')
    try: return historical_predictor.train(days=days, system_ids=body.system_ids) if mode=="historical" else predictor.train(system_ids=body.system_ids)
    except ValueError as e: raise HTTPException(400,str(e))
    finally: train_lock.release()

@app.post('/api/chat')
def analyst(body:ChatRequest):
    if not chat_lock.acquire(blocking=False): raise HTTPException(429,'Analyst is busy. One local generation runs at a time.')
    try: return chat.answer(body.question, body.entity_kind.value if body.entity_kind else None,body.entity_id)
    except chat.BudgetExceeded as e: raise HTTPException(429,str(e))
    except ValueError as e: raise HTTPException(400,str(e))
    except httpx.HTTPError: raise HTTPException(503,'Ollama is unavailable or rejected the request. Check its address, model, and server logs. No cloud fallback was attempted.')
    finally: chat_lock.release()

@app.get('/api/system-activity')
def system_activity_view(hours:int=Query(6)):
    try: return system_activity.activity(hours)
    except ValueError as e: raise HTTPException(400,str(e))

@app.get('/api/battles')
def battle_reports(hours:int=Query(24)):
    try: return battles.recent(hours=hours)
    except ValueError as e: raise HTTPException(400,str(e))

@app.get('/api/npc/status')
def npc_status(): return npc_store.status()

@app.get('/api/npc/forecast')
def npc_forecast():
    try: return npc_predictor.forecast()
    except ValueError as e: raise HTTPException(400,str(e))

@app.post('/api/npc/train')
def npc_train():
    if not npc_train_lock.acquire(blocking=False): raise HTTPException(409,'NPC training is already running.')
    try: return npc_predictor.train()
    except ValueError as e: raise HTTPException(400,str(e))
    finally: npc_train_lock.release()

app.mount('/static',StaticFiles(directory=Path(__file__).parent/'static'),name='assets')
app.mount('/',StaticFiles(directory=Path(__file__).parent/'static',html=True),name='dashboard')
