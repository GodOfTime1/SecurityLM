"""Administrator-curated model facts; recommendations never invent runtime targets."""
import json
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .auth import User, current_user, admin_user
from .db import connection

Task = Literal['document_qa','coding','summarization','general']
Short = Annotated[str, Field(min_length=1,max_length=40)]
ModelID = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
router = APIRouter(prefix='/models',tags=['model catalog'])


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: ModelID
    name: str = Field(min_length=1,max_length=100)
    runtime_model: str = Field(min_length=1,max_length=100)
    description: str = Field(default='',max_length=500)
    tasks: list[Task] = Field(min_length=1,max_length=4)
    deployment: Literal['local','remote'] = 'local'
    estimated_memory_gb: float | None = Field(default=None,gt=0,le=100000,allow_inf_nan=False)
    memory_notes: str = Field(default='',max_length=300)
    languages: list[Short] = Field(default_factory=lambda:['English'],min_length=1,max_length=10)
    supports_tools: bool = False
    license_notes: str = Field(default='',max_length=200)
    source_url: str = Field(default='',max_length=300,pattern=r'^(https://[^\s]+)?$')
    ready: bool = False
    enabled: bool = True


class CatalogUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1)
    model: CatalogModel


def catalog_rows(db):
    return [{**json.loads(r['data']), 'version':r['version']}
            for r in db.execute('SELECT * FROM model_catalog ORDER BY id')]


def public_model(row):
    return {k:v for k,v in row.items() if k not in {'runtime_model'}}


@router.get('')
def list_models(user: User = Depends(current_user)):
    with connection() as db:
        rows = catalog_rows(db)
    return {'models':[r if user.role=='admin' else public_model(r) for r in rows if r['enabled'] or user.role=='admin']}


@router.post('',status_code=201)
def add_model(body: CatalogModel,user: User = Depends(admin_user)):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM model_catalog WHERE id=?',(body.id,)).fetchone():
            raise HTTPException(409,'Model ID already exists')
        if db.execute('SELECT COUNT(*) FROM model_catalog').fetchone()[0]>=20:
            raise HTTPException(409,'Catalog is limited to 20 models; update an existing entry')
        db.execute('INSERT INTO model_catalog(id,data) VALUES (?,?)',(body.id,body.model_dump_json()))
    return {**body.model_dump(),'version':1}


@router.put('/{model_id}')
def update_model(model_id: str,body: CatalogUpdate,user: User = Depends(admin_user)):
    if model_id!=body.model.id:
        raise HTTPException(422,'Model ID must match the URL')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM model_catalog WHERE id=?',(model_id,)).fetchone()
        if not row:raise HTTPException(404,'Model not found')
        if row['version']!=body.expected_version:raise HTTPException(409,'Catalog entry changed; reload it')
        db.execute('UPDATE model_catalog SET data=?,version=version+1 WHERE id=?',(body.model.model_dump_json(),model_id))
    return {**body.model.model_dump(),'version':body.expected_version+1}
