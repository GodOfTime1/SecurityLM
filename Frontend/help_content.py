"""Reviewed product guide text. These answers are documentation, not model output."""
import re
from constants import PRESETS, TEAM_MODEL

PROFILE_LIST = ", ".join(p["name"] for p in PRESETS)

TOPICS = {
    'slm': ('What is an SLM?',
        'A small language model is a language model with a comparatively smaller resource footprint. '
        'Its actual speed, memory needs, and quality depend on the model, quantization, context length, and hardware. '
        'Small models can run locally or on a remote server. Neither size nor local hosting guarantees privacy, zero latency, or accuracy.'),
    'privacy': ('Privacy & storage',
        'The backend stores extracted document text, metadata, conversations, and action records in its SQLite database. '
        'The current document service does not retain original PDF bytes. It checks role access and assistant collections before retrieval, '
        'and rechecks source access when history is displayed. If inference uses a remote server, permitted context leaves the backend for that server. '
        'Configured sensitive-data checks are heuristic and can miss information. This prototype is not a privacy certification or an air-gap guarantee.'),
    'hardware': ('Answer styles & reply length',
        f'All three styles use {TEAM_MODEL}. Quick requests up to 128 reply tokens, Balanced 512, and In-depth 1,024. '
        'These are reply lengths, not bit widths or accuracy scores. New limits require the prepared runtime update. '
        'More tokens give room for detail; they do not establish speed or accuracy improvements. '
        'The supplied local adapter caps inputs at 8,192 tokens.'),
    'setup': ('Setup & troubleshooting',
        'Run the FastAPI backend and Streamlit frontend in separate terminals. Sign in with your individual backend access key. '
        'Create an assistant profile, choose its task in Assistant setup, upload a PDF/TXT/MD, link the document to that profile, then start a conversation. '
        'Echo mode only quotes retrieved text; it does not run a language model. An administrator can configure the real runtime and use Assistant setup to check it. '
        'Changing a boolean in frontend code cannot connect a model.'),
    'documents': ('Documents & citations',
        'In Documents, upload a readable PDF/TXT/MD up to 10 MiB. The optional Also link checkbox adds successful uploads to the selected assistant. '
        'Uploading and linking are separate backend operations; if linking fails, retry linking without reuploading. Manage linked documents changes the full collection. '
        'Scanned PDFs need OCR, which is not included. Previews show extracted text. Answers expose source passages through citations. '
        'Replacing a source can make old answers stale; removing access can cause dependent answers to be withheld. '
        'Use Conversation memory to pin important user details. Notes accompany each new message and are saved in its history. '
        'Check latest answer requests a fresh source review; self-review is not an independent fact check. '
        'Retrieval is bounded lexical search, so a summary may not cover every section of a large collection. '
        'The backend requires factual answers from retrieved passages; pretrained language and reasoning help explain those passages. '),
    'advisor': ('How the model advisor works',
        'The advisor interprets a use case and checks recommendations against the team-curated catalog. '
        'It receives your advisor messages, constraints, recent advisor history, and public catalog metadata; it does not retrieve uploaded company documents. '
        'The advisor is separate from document chat. In demo mode, enter explicit task, deployment, and local-memory constraints. '
        'Applying a recommendation saves configuration; it does not install or launch the model.'),
    'actions': ('Actions & approvals',
        'A document assistant can propose an enabled tool as a draft. The backend validates the arguments and requires approval of the exact draft version before execution. '
        'In the workspace, review a support-ticket draft, edit it if needed, and approve its exact version to create the ticket. '
        'Only enabled tools can be proposed. The model advisor cannot execute tools.'),
}

KEYWORDS = {
    'slm': {'slm','llm','small','language','latency'},
    'privacy': {'privacy','secure','security','cloud','leave','leaves','storage','stored','persist','persistence','air','gap','data'},
    'hardware': {'hardware','ram','vram','gpu','specs','requirements','snake2','llama4','megladon8','memory','context','contexts','tokens','llama','llama4'},
    'setup': {'setup','install','installation','inactive','connect','connection','terminal','key','login','echo','demo'},
    'documents': {'document','documents','upload','uploaded','pdf','ocr','citation','citations','link','linked','search','retrieval'},
    'advisor': {'advisor','recommend','recommendation','choose','selection','selector','catalog'},
    'actions': {'action','actions','tool','tools','approve','approval','ticket'},
}


def guide_answer(question):
    words=set(re.findall(r'\w+',question.lower()))
    scores={key:len(words & keywords) for key,keywords in KEYWORDS.items()}
    topic=max(scores,key=scores.get)
    if not scores[topic]:
        return 'Product guide', 'I do not have a reviewed guide answer for that question. Try one of the help topics, or use the Model advisor tab for model selection. Company-document questions belong in your assistant workspace.'
    return TOPICS[topic]


def bounded_selector_history(messages):
    """Keep display history separate from the selector's 10-message/4000-char limit."""
    output=[]
    remaining=4000
    for message in reversed(messages[-10:]):
        if message.get('role') not in {'user','assistant'} or not isinstance(message.get('content'),str):
            continue
        content=message['content'][:min(2000,remaining)]
        if not content:
            continue
        output.insert(0,{'role':message['role'],'content':content})
        remaining-=len(content)
        if not remaining:break
    return output
