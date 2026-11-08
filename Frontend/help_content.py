"""Reviewed product guide text. These answers are documentation, not model output."""
import re

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
    'hardware': ('Hardware budgets',
        'Snake2, Llama4, and Megladon8 are product presets with 2, 4, and 8 GB target budgets. '
        'They are not verified downloadable model names or measured RAM/VRAM requirements. '
        'The model teammate must map each preset to a real runtime tag and verify its memory use and quality. '
        'The advisor will not claim that an unmeasured preset fits a hard local memory constraint.'),
    'setup': ('Setup & troubleshooting',
        'Run the FastAPI backend and Streamlit frontend in separate terminals. Sign in with your individual backend access key. '
        'Create an assistant profile, upload a PDF/TXT/MD, link the document to that profile, then start a conversation. '
        'Echo mode only quotes retrieved text; it does not run a language model. An administrator can configure the real runtime and use Model settings to check it. '
        'Changing a boolean in frontend code cannot connect a model.'),
    'documents': ('Documents & citations',
        'Uploading and linking are separate steps. In Documents, upload a readable PDF/TXT/MD up to 10 MiB, then save the selected assistant collection. '
        'Scanned PDFs need OCR, which is not included. Previews show extracted text. Answers expose source passages through citations. '
        'Replacing a source can make old answers stale; removing access can cause dependent answers to be withheld. '
        'Retrieval is bounded lexical search, so a summary may not cover every section of a large collection.'),
    'advisor': ('How the model advisor works',
        'The advisor interprets a use case and checks recommendations against the team-curated catalog. '
        'It receives your advisor messages, constraints, recent advisor history, and public catalog metadata; it does not retrieve uploaded company documents. '
        'Its runtime is configured independently from document chat. In demo mode, enter explicit task, deployment, and local-memory constraints. '
        'Applying a recommendation saves configuration; it does not install or launch the model.'),
    'actions': ('Actions & approvals',
        'A document assistant can propose an enabled tool as a draft. The backend validates the arguments and requires approval of the exact draft version before execution. '
        'The current frontend displays draft suggestions but does not provide the action approval screen yet. The model advisor cannot execute tools.'),
}

KEYWORDS = {
    'slm': {'slm','llm','small','language','latency'},
    'privacy': {'privacy','secure','security','cloud','leave','leaves','storage','stored','persist','persistence','air','gap','data'},
    'hardware': {'hardware','ram','vram','gpu','specs','requirements','snake2','llama4','megladon8','memory'},
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
