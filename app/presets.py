"""Product presets are aliases; server-side mappings choose actual runtime models."""
import json
import os
import re
import asyncio
import httpx
from threading import BoundedSemaphore
from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Depends, HTTPException
from .auth import User, admin_user, current_user
from .db import connection
from .model_catalog import CatalogModel
from .model_client import ModelUnavailable, get_model_client

router = APIRouter(tags=['runtime and presets'])
probe_slots=BoundedSemaphore(2)
PRESETS = (
    {'id':'snake2','name':'Snake2','target_memory_gb':2,'icon':'🐍','env':'SNAKE2_MODEL'},
    {'id':'llama4','name':'Llama4','target_memory_gb':4,'icon':'🦙','env':'LLAMA4_MODEL'},
    {'id':'megladon8','name':'Megladon8','target_memory_gb':8,'icon':'🦈','env':'MEGLADON8_MODEL'},
)


def resolve_runtime_model(model, backend=None):
    if not model.startswith('preset:'):
        return model

    preset = next((p for p in PRESETS if model == 'preset:' + p['id']), None)
    if preset is None:
        raise ModelUnavailable('Unknown model preset')

    backend = backend or os.environ.get('MODEL_BACKEND', 'echo')

    # Transformers uses the preset ID to select its token budget.
    # All presets share the same locally configured model directory.
    if backend == 'transformers':
        return model

    # Keep the existing Ollama mapping behavior.
    tag = os.environ.get(preset['env'], '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', tag) or tag.startswith('preset:'):
        raise ModelUnavailable(
            f"{preset['name']} needs a real runtime mapping. "
            f"Configure {preset['env']} on the backend."
        )
    return tag


@router.get('/model-presets')
def list_presets(user: User = Depends(current_user)):
    with connection() as db:
        existing = {r['id']: json.loads(r['data']) for r in db.execute('SELECT id,data FROM model_catalog')}
    backend = os.environ.get('MODEL_BACKEND', 'echo')
    items=[]
    for preset in PRESETS:
        try:
            resolve_runtime_model('preset:' + preset['id'], backend)
            mapped=True
        except ModelUnavailable:
            mapped=False
        record=existing.get(preset['id'])
        available=bool(record and record['enabled'] and record['runtime_model']=='preset:'+preset['id'])
        items.append({k:v for k,v in preset.items() if k!='env'} | {'catalog_available':available,'runtime_mapped':mapped})
    return {'presets':items, 'memory_note':'2/4/8 GB are target budgets, not verified RAM/VRAM requirements or model parameter counts.'}


@router.post('/model-presets/initialize')
def initialize_presets(user: User = Depends(admin_user)):
    # Explicit and idempotent; never overwrite an administrator's catalog facts.
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        rows={r['id']:json.loads(r['data']) for r in db.execute('SELECT id,data FROM model_catalog')}
        for p in PRESETS:
            if p['id'] in rows and rows[p['id']]['runtime_model']!='preset:'+p['id']:
                raise HTTPException(409,'A preset catalog ID is already used by another entry; ask an administrator to resolve it')
        missing=[p for p in PRESETS if p['id'] not in rows]
        if len(rows)+len(missing)>20:
            raise HTTPException(409,'The catalog has no room for the preset entries')
        for p in missing:
            model=CatalogModel(id=p['id'],name=p['name'],runtime_model='preset:'+p['id'],
                description=f"{p['name']} product preset. {p['target_memory_gb']} GB target budget; underlying model and memory use need verification.",
                tasks=['document_qa','summarization','general'],estimated_memory_gb=None,
                memory_notes=f"Target budget {p['target_memory_gb']} GB only. Actual runtime footprint has not been measured.",
                ready=False,enabled=True)
            db.execute('INSERT INTO model_catalog(id,data) VALUES (?,?)',(model.id,model.model_dump_json()))
    return {'initialized':True,'created_count':len(missing)}


@router.get('/runtime/status')
def runtime_status(assistant_id: str | None = None, user: User = Depends(current_user)):
    backend=os.environ.get('MODEL_BACKEND','echo')
    selected=''
    if assistant_id:
        if len(assistant_id)>100:
            raise HTTPException(422,'Invalid assistant ID')
        with connection() as db:
            assistant=db.execute('SELECT model FROM assistants WHERE id=?',(assistant_id,)).fetchone()
        if not assistant:
            raise HTTPException(404,'Assistant not found')
        selected=assistant['model']
    if backend=='echo':
        return {'state':'demo','demo':True,'configured':True,'connection_checked':False,
                'message':'Retrieval demo only. No language model is connected.'}
    try:
        client = get_model_client()  # Validates local configuration; no inference.
        if backend == 'transformers':
            if selected:
                resolve_runtime_model(selected, backend)
            # The Transformers adapter validates TRANSFORMERS_MODEL_PATH.
            # No Ollama tag or network connection is required.
        else:
            effective = resolve_runtime_model(
                selected or os.environ.get('OLLAMA_DEFAULT_MODEL', ''),
                backend
            )
            if not effective:
                raise ModelUnavailable(
                    'Select a model and configure its runtime mapping.'
                )
    except ModelUnavailable as exc:
        return {'state':'needs_configuration','demo':False,'configured':False,'connection_checked':False,'message':str(exc)}
    return {'state':'configured','demo':False,'configured':True,'connection_checked':False,
            'message':'Live adapter configured. Model availability will be checked when you send a message.'}


class RuntimeCheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    assistant_id: str = Field(min_length=1,max_length=100)


async def _installed_models(base_url):
    # No document text, prompts, or user credentials are sent to this endpoint.
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=3) as client:
        async with client.stream('GET',base_url+'/api/tags',headers={'Accept-Encoding':'identity'}) as response:
            if response.status_code!=200 or response.headers.get('content-encoding','identity')!='identity':
                raise ValueError
            data=bytearray()
            async for block in response.aiter_raw():
                if len(data)+len(block)>256*1024:raise ValueError
                data.extend(block)
            records=json.loads(data)['models']
            if not isinstance(records,list):raise ValueError
            return {m.get('name',m.get('model')) for m in records if isinstance(m,dict) and isinstance(m.get('name',m.get('model')),str)}


@router.post('/runtime/check')
def check_runtime(body: RuntimeCheck,user: User = Depends(admin_user)):
    status=runtime_status(body.assistant_id,user)
    if status['state']!='configured':
        return {**status,'available':False}
    with connection() as db:
        row=db.execute('SELECT model FROM assistants WHERE id=?',(body.assistant_id,)).fetchone()
    if not row:
        raise HTTPException(404,'Assistant not found')
    client = get_model_client()
    backend = os.environ.get('MODEL_BACKEND', 'echo')

    if backend == 'transformers':
        if row['model']:
            resolve_runtime_model(row['model'], backend)
        return {
            'state': 'configured',
            'available': True,
            'demo': False,
            'connection_checked': False,
            'message': (
                'The local model directory is configured. '
                'GPU loading and inference still need to be tested.'
            )
        }

    model = resolve_runtime_model(
        row['model'] or os.environ.get('OLLAMA_DEFAULT_MODEL', ''),
        backend
    )
    if not probe_slots.acquire(blocking=False):
        raise HTTPException(503,'Runtime check is busy; retry shortly')
    async def bounded():
        return await asyncio.wait_for(_installed_models(client.base_url),timeout=8)
    try:
        installed=asyncio.run(bounded())
        canonical=model if ':' in model.rsplit('/',1)[-1] else model+':latest'
        available=model in installed or canonical in installed
        return {'state':'available' if available else 'model_missing','available':available,'demo':False,
            'connection_checked':True,'message':'Runtime responded and the selected model is installed. Answer quality and memory use still require testing.' if available
            else 'Runtime responded, but the selected model tag is not installed.'}
    except (httpx.HTTPError,ValueError,KeyError,TypeError,TimeoutError,RecursionError):
        return {'state':'unreachable','available':False,'demo':False,'connection_checked':True,
                'message':'The runtime could not be verified. Check its server, address, and installed model tag.'}
    finally:
        probe_slots.release()
