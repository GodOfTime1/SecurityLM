"""Workspace presentation only. The backend owns messages, revisions and access checks."""
import streamlit as st
from api_client import APIError,new_turn,resource
from constants import PRESET_NAMES,PRESET_BY_ID,TEAM_MODEL,active_reply_tokens
from session_cache import invalidate_metadata,metadata
from action_ui import render_actions
from conversation_memory import pack_prompt,unpack_prompt,restored_notes,prompt_limit,MAX_NOTES
from ui_common import (setup,connect,show_notice,get_assistants,pick_assistant,profile_form,
    preset_cards,runtime_banner,render_message,heading,conversation_labels,hero,plain)


def render_onboarding(api,user):
    hero('Build your document assistant', 'Choose its task. Connect your knowledge. Get answers you can check.', 'YOUR KNOWLEDGE, PUT TO WORK')
    runtime_banner(api)
    with st.container(border=True):
        st.subheader('From source to answer')
        for col, title, body in zip(st.columns(3),
                ['1 · Create a profile','2 · Add knowledge','3 · Ask with sources'],
                ['Name the assistant and describe its job.', 'Upload documents, then link them to the assistant.',
                 'Open a conversation and inspect the cited passages.']):
            col.markdown('**'+title+'**');col.write(body)
    preset_cards(api,user)
    if user['role']=='admin':
        with st.container(border=True):
            preset=st.session_state.get('draft_preset')
            st.subheader('Name your assistant')
            profile_form(api,preset=preset)
    else:st.info('An administrator needs to create an assistant profile before you can chat.')


def render_checklist(assistant,has_messages):
    steps=[('Choose a profile',bool(assistant['model'])),('Add documents',bool(assistant['document_ids'])),('Ask a question',has_messages)]
    st.caption('   →   '.join(('✅ ' if done else '⬜ ')+label for label,done in steps))


def render_delete(api,cid,pending):
    with st.expander('Manage conversation'):
        confirm=st.checkbox('Delete this conversation and its messages',key='delete_confirm_'+cid)
        if pending:st.caption('Resolve or discard the pending attempt before deleting this conversation.')
        if st.button('Delete conversation',disabled=not confirm or bool(pending),key='delete_conversation_'+cid):
            api.request('DELETE','/conversations/'+resource(cid))
            st.session_state.pop('pending_'+cid,None)
            st.session_state.pop('chat_error_'+cid,None)
            st.session_state['notice']='Conversation deleted.'
            st.rerun()


def render_chat(api,conv):
    cid=conv['id'];key='pending_'+cid
    pending=st.session_state.get(key)
    notes_key='conversation_notes_'+cid
    if notes_key not in st.session_state:st.session_state[notes_key]=restored_notes(conv['messages'])
    notes=st.session_state[notes_key]
    with st.expander('Conversation memory',expanded=False):
        st.caption('Pin details to include in every message. Notes stay within this conversation.')
        with st.form('memory_'+cid):
            draft=st.text_area('Pinned notes',value=notes or '',max_chars=MAX_NOTES,disabled=bool(pending),
                help='For example: I use Windows 11. Prefer numbered instructions. My budget is $500.')
            save_notes=st.form_submit_button('Save conversation notes',disabled=bool(pending))
        if save_notes:
            st.session_state[notes_key]=draft.strip()
            st.session_state['notice']='Notes updated for your next message. They are saved to history when you send it.';st.rerun()
        if notes:st.text(notes)
        st.caption('Recent chat history is also used. Pin important details for longer conversations.')
    for message in conv['messages']:
        render_message(message)
        render_actions(api,message)
    if st.session_state.get('chat_error_'+cid):st.error(st.session_state['chat_error_'+cid])
    if pending and conv['revision']==pending['expected_revision']:
        render_message({'role':'user','content':pending['content'],'pending':True})
    send=False
    if pending:
        st.info('Reply not confirmed. Retry safely or refresh to check your history.')
        retry,discard=st.columns(2)
        send=retry.button('Retry pending message')
        if discard.button('Discard pending attempt'):
            st.session_state.pop(key,None);st.session_state.pop('chat_error_'+cid,None)
            st.session_state['notice']='Attempt cleared locally. Check history before resending; this does not cancel backend work.'
            st.rerun()
    starter = None
    if not conv['messages'] and not pending:
        st.caption('Start with a source-focused question')
        for col, label, question in zip(st.columns(3),
                ['Key points','Find instructions','Compare sources'],
                ['What are the key points in the available source passages? Cite your sources.',
                 'What step-by-step instructions appear in the available source passages? Cite your sources.',
                 'Do the available source passages disagree on any points? Cite the relevant passages.']):
            if col.button(label,key='starter_'+label,width='stretch'):starter=question
    last_answer=next((m for m in reversed(conv['messages']) if m['role']=='assistant'),None)
    if last_answer and not last_answer.get('redacted') and not pending:
        if st.button('Check latest answer',help='Review claims against currently available sources.'):
            last_question=next((unpack_prompt(m['content'])[0] for m in reversed(conv['messages']) if m['role']=='user'),'')
            starter='Review your previous answer to this question: '+last_question[:700]+'. Check factual claims, numbers, and citations against the currently available passages. Return a corrected answer or say what cannot be supported. Do not claim independent verification.'
    prompt=st.chat_input('Ask about your documents…',disabled=bool(pending),max_chars=prompt_limit(notes))
    prompt=prompt or starter
    if prompt and prompt.strip():
        try:content=pack_prompt(prompt,notes)
        except ValueError as exc:st.warning(str(exc));return
        pending=new_turn(content,conv['revision']);st.session_state[key]=pending
        render_message({'role':'user','content':prompt,'pending':True})  # Before the blocking model call.
        send=True
    if send:
        try:
            with st.spinner('Preparing your reply…'):
                api.request('POST','/conversations/'+resource(cid)+'/messages',json=pending)
        except APIError as exc:st.session_state['chat_error_'+cid]=str(exc)
        else:
            st.session_state.pop(key,None);st.session_state.pop('chat_error_'+cid,None)
        st.rerun()  # A failure shows Retry immediately; it never resends automatically.
    render_delete(api,cid,pending)


def render_workspace(api,user,assistants):
    with st.sidebar:
        assistant=pick_assistant(assistants,key='workspace_profile')
        if user['role']=='admin':
            with st.expander('New assistant profile'):profile_form(api)
    aid=assistant['id']
    conversations=[c for c in api.all_items('/conversations','conversations') if c['assistant_id']==aid]
    conversations.sort(key=lambda c: c.get('updated_at') or '',reverse=True)
    hero(assistant['name'], assistant['purpose'] or 'Ask questions, inspect sources, and work with your linked knowledge.', 'WORKSPACE / DOCUMENT ASSISTANT')
    runtime_banner(api,aid)
    preset_id=assistant['model'].removeprefix('preset:')
    preset=PRESET_BY_ID.get(preset_id)
    active_budget=None
    if preset:
        try:reported=next((p for p in metadata(api,'/model-presets')['presets'] if p['id']==preset_id),{})
        except APIError:reported={}
        active_budget=active_reply_tokens(preset,reported)
    for col,label,value in zip(st.columns(4),['Answer style','Reply budget','Linked sources','Conversations'],
            [PRESET_NAMES.get(preset_id,'Custom' if preset_id else 'Backend default'),
             (f"{active_budget} tokens" if active_budget==preset['reply_tokens'] else f"{preset['reply_tokens']} requested") if preset else 'Runtime default',
             str(len(assistant['document_ids'])),str(len(conversations))]):
        with col,st.container(border=True):st.metric(label,value)
    st.caption('Llama4 · Answers with sources')
    with st.expander('Assistant knowledge & behavior'):
        st.write('Search your linked documents or check the assistant’s settings.')
        left,right=st.columns(2)
        left.page_link('pages/document_upload.py',label='Manage knowledge',icon=':material/folder_open:')
        if user['role']=='admin':right.page_link('pages/model_settings.py',label='Change task & reply budget',icon=':material/tune:')
        with st.form('source_search_'+aid):
            query=st.text_input('Search linked knowledge',max_chars=500,placeholder='Find a policy, procedure, or topic')
            search=st.form_submit_button('Find source passages')
        if search:
            if not query.strip():st.warning('Enter a topic to search.')
            else:
                results=api.request('POST','/assistants/'+resource(aid)+'/search',json={'query':query.strip(),'limit':5})['results']
                if not results:st.info('No matching passages in the documents available to this assistant.')
                for item in results:
                    with st.container(border=True):
                        st.caption(plain(f"{item['filename']} · page {item['page_number']}"))
                        st.text(item['text'])
    if not assistant['document_ids']:
        st.info('Add documents to give this assistant knowledge.')
        st.page_link('pages/document_upload.py',label='Add documents to this assistant',icon='📄')
    title,new=st.columns([4,1]);title.subheader('Conversation')
    if new.button('New conversation',type='primary',width='stretch'):
        conv=api.request('POST','/conversations',json={'assistant_id':aid})
        st.session_state['conversation_'+aid]=conv['id']
        st.session_state['notice']='Conversation started. Ask your first question below.'
        st.rerun()
    if not conversations:
        render_checklist(assistant,False)
        st.info('Start a conversation to ask questions about your linked documents.')
        return
    key='conversation_'+aid
    with st.sidebar:
        query=st.text_input('Find a conversation',key='conversation_search_'+aid,placeholder='Search titles…') if len(conversations)>5 else ''
    filtered=[c for c in conversations if query.casefold() in (c.get('title') or 'New conversation').casefold()]
    if not filtered:
        st.info('No conversations match that title. Clear the search in the sidebar to see all conversations.')
        return
    choices=[c['id'] for c in filtered];labels=conversation_labels(filtered)
    if st.session_state.get(key) not in choices:st.session_state.pop(key,None)
    with st.sidebar:cid=st.selectbox('Conversation',choices,format_func=lambda value:labels[value],key=key)
    conv=api.conversation(cid)  # Always fresh: no client cache of sensitive answers/citations.
    render_checklist(assistant,bool(conv['messages']))
    if st.button('Refresh conversation'):
        invalidate_metadata();st.rerun()
    if not conv['messages']:st.caption('Try a specific question, or ask for a summary of your documents.')
    render_chat(api,conv)


def main():
    setup('Workspace');api,user=connect();show_notice()
    try:
        assistants=get_assistants(api)
        if assistants:render_workspace(api,user,assistants)
        else:render_onboarding(api,user)
    except APIError as exc:st.error(str(exc))


main()
