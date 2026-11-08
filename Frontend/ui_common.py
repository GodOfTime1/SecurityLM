"""Small shared UI helpers; authorization and source content are always rechecked."""
from datetime import datetime, timezone
from pathlib import Path
from html import escape
from prompting import TASKS, build_instructions
from conversation_memory import unpack_prompt
import re
import string
import streamlit as st
from api_client import APIError, BackendAPI
from constants import PRESETS, PRESET_NAMES, TEAM_MODEL, active_reply_tokens
from session_cache import metadata, invalidate_metadata


def plain(value):
    """Escape all Markdown punctuation before rendering external text as a heading/label."""
    return re.sub('(['+re.escape(string.punctuation)+'])', r'\\\1', str(value).replace('\n',' '))


def heading(value, level=1):
    {1:st.title,2:st.header,3:st.subheader}[level](plain(value))


def setup(title):
    st.set_page_config(page_title=f'{title} · SecurityLM',page_icon=str(Path(__file__).with_name('assets')/'securitylm-logo.png'),layout='wide',initial_sidebar_state='expanded')
    st.markdown('<style>'+Path(__file__).with_name('styles.css').read_text()+'</style>',unsafe_allow_html=True)
    with st.sidebar:
        icon,name=st.columns([1,4],vertical_alignment='center',gap='small')
        icon.image(str(Path(__file__).with_name('assets')/'securitylm-logo.png'),width=46)
        name.markdown('<div class="sl-brand-name">SecurityLM</div><div class="sl-brand-sub">Knowledge into action</div>',unsafe_allow_html=True)
        st.caption('WORKSPACE')


def hero(title, description, eyebrow):
    with st.container(key='hero'):
        st.markdown('<p class="sl-eyebrow">'+escape(eyebrow)+'</p>',unsafe_allow_html=True)
        heading(title)
        st.write(description)


def navigation(user):
    with st.sidebar:
        st.page_link('home.py',label='Workspace',icon=':material/chat_bubble_outline:')
        st.page_link('pages/document_upload.py',label='Documents',icon=':material/folder_open:')
        st.page_link('pages/help.py',label='Help & advisor',icon=':material/explore:')
        if user['role']=='admin':
            st.page_link('pages/model_settings.py',label='Assistant setup',icon=':material/tune:')
        st.divider()
        st.caption(plain(f"Signed in: {user['id']} · {user['role']}"))
        if st.button('Sign out'):
            st.session_state.clear()
            st.rerun()


def connect():
    if 'api_key' not in st.session_state:
        hero('Your knowledge. A focused assistant.', 'Connect, choose a task, and turn your permitted documents into cited answers.', 'SECURITYLM / PRIVATE WORKSPACE')
        st.subheader('Connect to your workspace')
        with st.form('connect'):
            key=st.text_input('Access key',type='password')
            submitted=st.form_submit_button('Connect',type='primary')
        if submitted and key.strip():
            try:BackendAPI(key.strip()).request('GET','/me')
            except APIError as exc:st.error(str(exc))
            else:
                st.session_state.clear()
                st.session_state['api_key']=key.strip()
                st.rerun()
        elif submitted:
            st.warning('Enter your access key to connect.')
        st.caption('Ask your workspace administrator for a key. Your key is kept in this browser session’s server-side state.')
        st.stop()
    api=BackendAPI(st.session_state['api_key'])
    try:
        user=api.request('GET','/me')  # Never cache identity or authorization.
    except APIError as exc:
        invalidate_metadata()
        st.error(str(exc))
        if st.button('Disconnect'):
            st.session_state.clear();st.rerun()
        st.stop()
    api.on_mutation=invalidate_metadata
    # A credential can be reassigned a role while the browser session remains open.
    # Drop local drafts/advisor output when backend identity changes.
    identity = (user['id'], user['role'])
    if st.session_state.get('_identity', identity) != identity:
        key = st.session_state['api_key']
        st.session_state.clear()
        st.session_state['api_key'] = key
    st.session_state['_identity'] = identity
    navigation(user)
    return api,user


def get_assistants(api):
    return api.all_items('/assistants','assistants')


def pick_assistant(assistants,key='profile_picker'):
    if not assistants:
        return None
    ids=[a['id'] for a in assistants]
    names={a['id']:a['name'] for a in assistants}
    active=st.session_state.get('active_assistant_id')
    if active not in ids:active=ids[0]
    if st.session_state.get(key) not in ids or st.session_state.get('picker_sync_'+key)!=active:
        st.session_state[key]=active
    selected=st.selectbox('Assistant profile',ids,format_func=lambda i:names[i],key=key)
    st.session_state['active_assistant_id']=selected
    st.session_state['picker_sync_'+key]=selected
    return next(a for a in assistants if a['id']==selected)


def show_notice():
    if 'notice' in st.session_state:st.success(st.session_state.pop('notice'))


def profile_form(api,preset=None):
    with st.form('create_assistant_'+str(st.session_state.get('profile_form_revision',0)),clear_on_submit=False):
        name=st.text_input('Profile name',placeholder='For example, IT Helper',max_chars=100)
        purpose=st.text_area('What should it help with?',max_chars=2000)
        task=st.selectbox('Primary task',list(TASKS),format_func=lambda value:TASKS[value]['name'])
        st.caption(TASKS[task]['description'])
        create=st.form_submit_button('Create assistant profile',type='primary')
    if create:
        if not name.strip():st.warning('Give your assistant profile a name.')
        else:
            if preset:api.request('POST','/model-presets/initialize')
            assistant=api.request('POST','/assistants',json={'name':name.strip(),'purpose':purpose,
                'model':'preset:'+preset['id'] if preset else '', 'system_prompt':build_instructions(task)})
            st.session_state['active_assistant_id']=assistant['id']
            st.session_state['profile_form_revision']=st.session_state.get('profile_form_revision',0)+1
            st.session_state['notice']='Assistant profile created. Next, add your documents.'
            st.rerun()


def read_status(api,path,params=None):
    try:return metadata(api,path,params=params)
    except APIError as exc:
        return {'state':'unavailable','can_request_advice':False,'message':str(exc),
                'setup_message':'Status unavailable. Check the backend connection and configuration.'}


def status_badge(status,label='Document chat'):
    state=status.get('state','unknown')
    names={'demo':('Demo','orange'),'configured':('Configured · unchecked','blue'),
        'needs_configuration':('Needs setup','orange'),'disabled':('Disabled','gray'),
        'unavailable':('Unavailable','gray'),'unknown':('Unknown','gray'),
        'model_missing':('Model missing','orange'),'unreachable':('Unreachable','red')}
    if state=='available' and status.get('available') and status.get('connection_checked'):
        text,color='Available · checked','green'
    else:text,color=names.get(state,('Needs checking','gray'))
    st.badge(label+': '+text,color=color)


def runtime_banner(api,assistant_id=None,full=False):
    status=read_status(api,'/runtime/status',{'assistant_id':assistant_id} if assistant_id else {})
    with st.sidebar:status_badge(status)
    if status.get('state')=='demo':
        st.warning('DEMO MODE · No language model is running. Replies are retrieved excerpts.')
    elif status.get('state') in {'disabled','needs_configuration','unavailable'}:
        st.info('Document chat needs attention. Check Help & model advisor → Connection checks.')
    elif full:
        st.text(status.get('message','Runtime has not been verified.'))
    return status


def preset_cards(api,user,assistant=None):
    available=True
    try:
        result=metadata(api,'/model-presets')
        reported=result.get('presets') if isinstance(result,dict) else None
        if not isinstance(reported,list) or any(not isinstance(p,dict) or not isinstance(p.get('id'),str) for p in reported):
            raise APIError('Unexpected preset metadata.')
    except APIError:
        reported=[];available=False
        st.session_state.pop('draft_preset',None)
        st.caption('Preset selection is unavailable right now. You can continue using the saved assistant configuration.')
    by_id={p['id']:p for p in reported}
    for col,preset in zip(st.columns(3,border=True),PRESETS):
        info=by_id.get(preset['id'])
        with col:
            with st.container(height=300,border=False,key='model_card_'+preset['id']):
                st.caption(preset['icon']+'  '+TEAM_MODEL)
                st.subheader(preset['name'])
                st.markdown(f'<div class="sl-token">{preset["reply_tokens"]}<small> tokens</small></div>',unsafe_allow_html=True)
                current=active_reply_tokens(preset,info)
                st.caption('MAX REPLY TOKENS' if current==preset['reply_tokens'] else f'Requested · active limit {current}')
                st.write(preset['description'])
            st.caption(f"{preset['reply_tokens']//128}× reply space"+(" requested" if current!=preset['reply_tokens'] else ""))
            selected=bool(assistant and assistant['model']=='preset:'+preset['id']) if assistant else st.session_state.get('draft_preset',{}).get('id')==preset['id']
            if st.button('Selected' if selected else 'Choose '+preset['name'],key='choose_'+preset['id'],
                    disabled=selected or user['role']!='admin' or not available or info is None,
                    type='primary' if selected else 'secondary',width='stretch'):
                if assistant:
                    api.request('POST','/model-presets/initialize')
                    model=next((m for m in api.request('GET','/models')['models'] if m['id']==preset['id']), None)
                    if model is None:
                        raise APIError('This profile is no longer in the catalog. Refresh before choosing it.')
                    api.request('POST','/model-selector/apply',json={'assistant_id':assistant['id'],'model_id':model['id'],
                        'catalog_version':model['version'],'expected_current_model':assistant['model']})
                    st.session_state['notice']='Profile choice saved. Start a new conversation after switching.'
                else:st.session_state['draft_preset']=dict(preset)
                st.rerun()
    st.caption('One model. Three ways to answer.')
    if any(active_reply_tokens(p,by_id.get(p['id']))!=p['reply_tokens'] for p in PRESETS):
        st.info('New reply limits need the prepared runtime update.')
    if user['role'] != 'admin':st.caption('An administrator manages profile choices.')


def render_message(message):
    with st.chat_message(message['role']):
        # Fail closed even if a backend accidentally returns source content with this flag.
        if message.get('redacted'):
            st.warning('Source access changed; this answer is withheld.')
            return
        question,notes=unpack_prompt(message['content']) if message['role']=='user' else (message['content'],None)
        st.text(question)
        if message['role']=='assistant':
            referenced=set(re.findall(r'\[(\d+)\]',message['content']))
            available={str(c['n']) for c in message.get('citations',[]) if c.get('available')}
            if referenced-available:st.warning('Some references could not be matched to an available source.')
        if notes:
            with st.expander('Conversation notes used for this message'):st.text(notes)
        if message.get('pending'):st.caption('Awaiting confirmation…')
        if message.get('stale_sources'):st.warning('Historical answer: ask again for current source information.')
        for citation in message.get('citations',[]):
            if not citation.get('available'):
                st.caption(f"Source {citation['n']} is unavailable.");continue
            label=f"[{citation['n']}] {citation['filename']} · page {citation['page_number']}"
            with st.expander(plain(label)):st.text(citation['text'])


def conversation_labels(conversations):
    labels={};seen={};now=datetime.now(timezone.utc)
    for conv in conversations:
        text=conv['title'] or 'New conversation'
        try:
            when=datetime.fromisoformat(conv['updated_at'].replace('Z','+00:00'))
            if when.tzinfo is None:when=when.replace(tzinfo=timezone.utc)
            seconds=max(0,int((now-when).total_seconds()))
            age='just now' if seconds<60 else f'{seconds//60}m ago' if seconds<3600 else f'{seconds//3600}h ago' if seconds<86400 else f'{seconds//86400}d ago'
            text+=' · '+age
        except (ValueError,KeyError,TypeError):pass
        seen[text]=seen.get(text,0)+1
        labels[conv['id']]=text+(f" · {seen[text]}" if seen[text]>1 else '')
    return labels
