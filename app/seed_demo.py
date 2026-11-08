"""Explicit, repeatable synthetic demo setup. Never runs automatically in production."""
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from .db import initialize, connection
from .documents import _chunks, extract_pages
from .model_catalog import CatalogModel


def seed_demo():
    if os.environ.get('APP_ENV','development')!='development':
        raise RuntimeError('Synthetic seeding is only permitted in development')
    if os.environ.get('MODEL_BACKEND','echo')!='echo' or os.environ.get('ADVISOR_BACKEND','echo')!='echo':
        raise RuntimeError('Use echo document/advisor backends for synthetic seeding')
    fixtures=Path(__file__).resolve().parent.parent/'fixtures'
    docs=[(name,visibility,extract_pages(name,(fixtures/name).read_bytes()))
          for name,visibility in [('vpn-guide.md','employee'),('admin-guide.md','admin')]]
    initialize()
    ids={name:str(uuid5(NAMESPACE_URL,'hackathon-synthetic:'+name)) for name,_,_ in docs}
    assistant_id=str(uuid5(NAMESPACE_URL,'hackathon-synthetic:assistant'))
    model=CatalogModel(id='demo-placeholder',name='Synthetic selector fixture (not a real SLM)',
        runtime_model='demo-placeholder',description='Synthetic fixture only. Demo matching, not a real model or hardware claim.',
        tasks=['document_qa'],deployment='local',estimated_memory_gb=None,ready=False,enabled=True)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for name,visibility,pages in docs:
            did=ids[name]
            if not db.execute('SELECT 1 FROM documents WHERE id=?',(did,)).fetchone():
                db.execute('INSERT INTO documents(id,filename,visibility,page_count) VALUES (?,?,?,?)',(did,name,visibility,len(pages)))
                chunks=_chunks(did,pages)
                db.executemany('INSERT INTO chunks(id,document_id,page_number,text) VALUES (?,?,?,?)',chunks)
                db.executemany('INSERT INTO chunk_search(chunk_id,text) VALUES (?,?)',[(c[0],c[3]) for c in chunks])
        if not db.execute('SELECT 1 FROM assistants WHERE id=?',(assistant_id,)).fetchone():
            db.execute("INSERT INTO assistants(id,name,purpose,created_by) VALUES (?,'IT Helper — synthetic demo','VPN help','demo-seed')",(assistant_id,))
            db.executemany('INSERT INTO assistant_documents VALUES (?,?)',[(assistant_id,did) for did in ids.values()])
            db.execute("INSERT INTO assistant_tools VALUES (?,'create_ticket')",(assistant_id,))
        if not db.execute('SELECT 1 FROM model_catalog WHERE id=?',(model.id,)).fetchone():
            db.execute('INSERT INTO model_catalog(id,data) VALUES (?,?)',(model.id,model.model_dump_json()))
    return {'assistant_id':assistant_id,'document_ids':ids,'catalog_id':model.id,'synthetic_only':True}


if __name__=='__main__':
    print(json.dumps(seed_demo(),indent=2))
