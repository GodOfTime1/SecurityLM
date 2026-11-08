"""Explicit conversation notes carried on each turn through the existing message API."""
import json
PREFIX='Saved conversation context:\n'
MAX_NOTES=800
MAX_MESSAGE=4000


def unpack_prompt(content):
    if isinstance(content,str) and content.startswith(PREFIX):
        try:
            data=json.loads(content[len(PREFIX):])
            if (isinstance(data,dict) and set(data)=={'question','notes'}
                    and isinstance(data['question'],str) and isinstance(data['notes'],str)
                    and len(data['notes'])<=MAX_NOTES):return data['question'],data['notes']
        except (ValueError,RecursionError):pass
    return content,None


def pack_prompt(question,notes):
    if notes is not None and len(notes)>MAX_NOTES:raise ValueError('Keep conversation notes within 800 characters.')
    text=PREFIX+json.dumps({'question':question,'notes':notes},ensure_ascii=False) if notes is not None else question
    if len(text)>MAX_MESSAGE:raise ValueError('This message and its saved notes exceed the message limit. Shorten the question or notes.')
    return text


def restored_notes(messages):
    # Only the owner's explicit user context is restored, never assistant assertions.
    for message in reversed(messages):
        if message.get('role')=='user':
            _,notes=unpack_prompt(message.get('content',''))
            if notes is not None:return notes
    return None


def prompt_limit(notes):
    return MAX_MESSAGE if notes is None else max(1,(MAX_MESSAGE-len(pack_prompt('',notes)))//2)
