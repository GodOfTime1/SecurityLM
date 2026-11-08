"""Heuristic sensitive-data checks. This is not a comprehensive PII/DLP detector."""
import re
from collections import Counter
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from .auth import User, current_user


class SensitiveDataError(ValueError):
    pass


PATTERNS = {
    'email':re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b',re.I),
    'ssn':re.compile(r'\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b'),
    'phone':re.compile(r'(?<!\w)(?:\+?1[-. ]?)?(?:\([2-9]\d{2}\)|[2-9]\d{2})[-. ]?[2-9]\d{2}[-. ]?\d{4}(?!\w)'),
    'credit_card':re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)'),
    'secret':re.compile(r'(?:\bsk-[A-Za-z0-9_-]{16,}|\bAIza[A-Za-z0-9_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:api[_-]?key|access[_-]?token|secret[_-]?key)[\'\"]?\s*[:=]\s*[\'\"]?[A-Za-z0-9_./+=-]{12,})',re.I),
}


def _luhn(text):
    digits=[int(c) for c in text if c.isdigit()]
    if not 13<=len(digits)<=19 or len(set(digits))==1:return False
    total=0
    for i,n in enumerate(reversed(digits)):
        if i%2:n=n*2-9 if n*2>9 else n*2
        total+=n
    return total%10==0


def matches(text):
    return [(m.start(),m.end(),kind) for kind,pattern in PATTERNS.items() for m in pattern.finditer(text)
            if kind!='credit_card' or _luhn(m.group())]


def preview(text):
    found=matches(text)
    counts=dict(Counter(kind for _,_,kind in found))
    if counts.get('secret'):
        # Never return the unmatched tail of a private key or partial secret.
        redacted='[REDACTED: SECRET MATERIAL]'
    else:
        pieces=[]
        end=0
        for start,stop,kind in sorted(found,key=lambda x:(x[0],-x[1])):
            if start<end:continue
            pieces.extend([text[end:start],f'[REDACTED:{kind.upper()}]'])
            end=stop
        pieces.append(text[end:])
        redacted=''.join(pieces)
    return {'detected':bool(found),'categories':counts,'redacted_text':redacted,
            'coverage':'heuristic; absence of a match is not proof that text contains no sensitive data'}


def protect_messages(messages,policy):
    if policy not in {'allow','block_sensitive','redact'}:
        raise ValueError('Unsupported sensitive-data policy')
    if policy=='allow':return messages
    output=[]
    for message in messages:
        scan=preview(message['content'])
        if scan['detected'] and (policy=='block_sensitive' or 'secret' in scan['categories']):
            raise SensitiveDataError('Sensitive data detected; model request was blocked by the configured egress policy')
        output.append({**message,'content':scan['redacted_text'] if policy=='redact' else message['content']})
    return output


class CheckIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(max_length=10000)


router=APIRouter(prefix='/privacy',tags=['privacy'])


@router.post('/check')
def check_text(body:CheckIn,user:User=Depends(current_user)):
    return preview(body.text)
