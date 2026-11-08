"""Deterministic HTTP response fixtures for frontend tests, not a backend implementation."""
from copy import deepcopy
import json
import httpx


class FrontendFixture:
    def __init__(self):
        self.role = 'admin'
        self.calls = []
        self.lost_reply = False
        self.hide_presets = False
        self.preset_status = 200
        self.assistants = [{'id':'a1','name':'IT Helper','purpose':'Answers grounded in your team’s documentation.',
                            'model':'preset:snake2','document_ids':['d1'],'system_prompt':''}]
        self.documents = [{'id':'d1','filename':'Employee handbook.md','visibility':'employee',
                           'version':1,'page_count':3}]
        self.conversations = [{'id':'c1','assistant_id':'a1','title':'Getting started',
                               'updated_at':'2026-11-08T01:00:00Z','revision':0,'messages':[]}]
        self.requests = {}

    def handler(self, request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.headers.get('content-type','').startswith('application/json') else None
        self.calls.append((method,path,deepcopy(body)))
        def ok(data, status=200): return httpx.Response(status,json=deepcopy(data))
        if path == '/me': return ok({'id':'test-user','role':self.role})
        if path == '/runtime/status': return ok({'state':'demo'})
        if path == '/model-selector/status': return ok({'state':'disabled','can_request_advice':False})
        if path == '/model-presets':
            return ok({} if self.hide_presets else {'presets':[{'id':x} for x in ['snake2','llama4','megladon8']]},404 if self.hide_presets else self.preset_status)
        if path == '/models': return ok({'models':[]})
        if path == '/assistants':
            if method == 'POST':
                value={'id':'a2','name':body['name'],'purpose':body['purpose'],'model':body['model'],'document_ids':[]}
                self.assistants.append(value);return ok(value,201)
            return ok({'assistants':self.assistants})
        if path=='/tools':return ok({'tools':[]})
        if path.endswith('/tools'):return ok({'tools':[]})
        if path.endswith('/search'):return ok({'results':[{'filename':'Employee handbook.md','page_number':1,'text':'Permitted source passage'}]})
        if path.startswith('/assistants/'):
            assistant=next(a for a in self.assistants if a['id']==path.split('/')[2])
            if method=='PUT': assistant['document_ids']=body['document_ids']
            if method=='PATCH':assistant.update(body)
            return ok(assistant)
        if path == '/documents': return ok({'documents':self.documents})
        if path.endswith('/chunks'): return ok({'chunks':[{'page_number':1,'text':'Ask your manager for onboarding access.'}]})
        if path == '/conversations':
            if method == 'POST':
                conv={'id':'c2','assistant_id':body['assistant_id'],'title':'New conversation','updated_at':'2026-11-08T02:00:00Z','revision':0,'messages':[]}
                self.conversations.append(conv);return ok(conv,201)
            return ok({'conversations':[{k:v for k,v in c.items() if k!='messages'} for c in self.conversations]})
        if path.startswith('/conversations/'):
            cid=path.split('/')[2];conv=next(c for c in self.conversations if c['id']==cid)
            if method=='DELETE':
                self.conversations.remove(conv);return httpx.Response(204)
            if method=='POST':
                if body['request_id'] not in self.requests:
                    conv['messages'] += [{'role':'user','content':body['content']},
                        {'role':'assistant','content':'Ask your manager for onboarding access.',
                         'citations':[{'n':1,'filename':'Employee handbook.md','page_number':1,'text':'Ask your manager for onboarding access.','available':True}]}]
                    conv['revision']+=1;self.requests[body['request_id']]=True
                if self.lost_reply:
                    self.lost_reply=False;raise httpx.ReadTimeout('simulated dropped response')
                return ok({'replayed':True})
            return ok(conv)
        return ok({'detail':'Fixture has no response for this route'},404)
