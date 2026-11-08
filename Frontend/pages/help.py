"""Product help, bounded catalog advice, and cached setup diagnostics."""
import streamlit as st
from api_client import APIError
from constants import PRESET_BY_ID, PRESET_NAMES
from prompting import task_id
from help_content import TOPICS,guide_answer,bounded_selector_history
from session_cache import invalidate_metadata
from ui_common import (setup,connect,show_notice,get_assistants,pick_assistant,read_status,
                       status_badge,heading,plain,hero)


def render_guide():
    st.subheader('A quick answer, without the guesswork')
    st.caption('Reviewed product documentation · not model-generated advice.')
    topics=list(TOPICS.items());selected=None
    for start in range(0,len(topics),4):
        for col,(key,(title,_)) in zip(st.columns(4),topics[start:start+4]):
            if col.button(title,key='faq_'+key,width='stretch'):selected=key
    with st.form('guide_question',clear_on_submit=True):
        question=st.text_input('Ask about setup, privacy, documents, or hardware',max_chars=2000)
        ask=st.form_submit_button('Find a guide answer')
    if selected or (ask and question.strip()):
        title,answer=TOPICS[selected] if selected else guide_answer(question)
        user_text=title if selected else question
        messages=st.session_state.get('help_guide_messages',[])
        st.session_state['help_guide_messages']=(messages+[{'role':'user','content':user_text},
            {'role':'assistant','content':title+'\n\n'+answer}])[-12:]
    for message in st.session_state.get('help_guide_messages',[]):
        with st.chat_message(message['role']):st.text(message['content'])
    if st.button('Clear guide conversation'):
        st.session_state.pop('help_guide_messages',None);st.rerun()


def render_advisor_form(req,revision,can_ask,state):
    def index(options,value):return options.index(value) if value in options else 0
    with st.form('advisor_question_'+str(revision)):
        message=st.text_area('Describe what you need',value=st.session_state.get('advisor_draft',''),placeholder='Describe the task, users, and constraints.',max_chars=2000)
        with st.expander('Your requirements',expanded=state=='demo'):
            tasks=[None,'document_qa','coding','summarization','general']
            labels={None:'Let the advisor interpret','document_qa':'Document Q&A','coding':'Coding','summarization':'Summarization','general':'General help'}
            left,right=st.columns(2)
            task=left.selectbox('Task',tasks,index=index(tasks,req.get('task')),format_func=lambda value:labels[value])
            deployments=[None,'local','remote','either']
            deployment=right.selectbox('Deployment',deployments,index=index(deployments,req.get('deployment')),
                format_func=lambda value:'Let the advisor interpret' if value is None else value.title())
            memory=st.number_input('Usable model memory (GB)',min_value=0.1,max_value=100000.0,
                value=float(req['available_memory_gb']) if req.get('available_memory_gb') else None,
                placeholder='Leave blank if unknown')
            st.caption('Enter the memory available for the model.')
            tools=[None,True,False]
            needs_tools=st.selectbox('Needs tool support',tools,index=index(tools,req.get('needs_tools')),
                format_func=lambda value:'No preference' if value is None else 'Yes' if value else 'No')
            language=st.text_input('Language (optional)',value=req.get('language') or '',max_chars=40)
            priorities=['balanced','speed','quality']
            priority=st.selectbox('Priority',priorities,index=index(priorities,req.get('priority')))
            ready=st.checkbox('Only models marked ready',value=bool(req.get('ready_only')))
        submit=st.form_submit_button('Ask model advisor',type='primary',disabled=not can_ask)
    requirements={'task':task,'deployment':deployment,'available_memory_gb':memory,'needs_tools':needs_tools,
        'language':language.strip() or None,'priority':priority,'ready_only':ready}
    return submit,message,{key:value for key,value in requirements.items() if value is not None}


def submit_advice(api,message,requirements,revision):
    if not message.strip():st.warning('Describe your use case first.');return
    try:
        with st.spinner('Checking the supported catalog…'):
            response=api.request('POST','/model-selector/chat',json={'message':message,'requirements':requirements,
                'history':bounded_selector_history(st.session_state.get('advisor_messages',[]))})
        if not isinstance(response,dict) or not isinstance(response.get('answer'),str) or not isinstance(response.get('requirements'),dict) or not isinstance(response.get('recommendations'),list):
            raise APIError('The advisor returned an unexpected response.')
    except APIError as exc:
        st.session_state['advisor_error']=str(exc);st.error(str(exc))
    else:
        messages=st.session_state.get('advisor_messages',[])
        st.session_state['advisor_messages']=(messages+[{'role':'user','content':message},{'role':'assistant','content':response['answer']}])[-12:]
        st.session_state['advisor_last']=response;st.session_state['advisor_requirements']=response['requirements']
        st.session_state.pop('advisor_error',None);st.session_state.pop('advisor_draft',None);st.session_state['advisor_revision']=revision+1
        st.rerun()


def render_recommendations(api,user,assistant):
    result=st.session_state.get('advisor_last')
    if not result:return
    mode={'demo':'Demo filters','rules':'Catalog rules','llm':'Language-model advice'}.get(result.get('mode'),'Unknown')
    st.caption('Last advice mode: '+mode)
    for warning in result.get('warnings',[]):st.text(warning)
    for question in result.get('follow_up_questions',[]):st.text('Follow-up: '+question)
    with st.expander('Requirements used for this advice — review before applying'):st.json(result['requirements'])
    if result.get('status')=='no_match':
        st.info('No supported model matches these requirements. Adjust the filters and try again.')
    for model in result['recommendations']:
        with st.container(border=True):
            heading(PRESET_NAMES.get(model['id'],model['name']),3);st.text(PRESET_BY_ID[model['id']]['description'] if model['id'] in PRESET_BY_ID else model.get('description',''))
            st.caption('Marked ready by admin' if model.get('ready') else 'Needs runtime setup')
            if model['id'] in PRESET_BY_ID:st.caption(f"Requested reply budget: {PRESET_BY_ID[model['id']]['reply_tokens']} tokens")
            if model.get('tasks'):st.caption('Supported tasks: '+', '.join(model['tasks']))
            if model.get('estimated_memory_gb') is not None:st.caption(f"Estimated memory: {model['estimated_memory_gb']:g} GB")
            if user['role']=='admin' and assistant:
                if st.button(plain('Apply to '+assistant['name']),key='advisor_apply_'+model['id']):
                    try:
                        api.request('POST','/model-selector/apply',json={'assistant_id':assistant['id'],'model_id':model['id'],
                            'catalog_version':model['version'],'expected_current_model':assistant['model']})
                    except APIError as exc:st.error(str(exc))
                    else:
                        st.session_state['notice']='Choice saved to the selected profile. This does not install or start a model. Context behavior comes from the backend.'
                        st.rerun()
            else:st.caption('An administrator can apply this choice after selecting an assistant profile.')


def render_catalog(api):
    try:models=api.request('GET','/models')['models']
    except APIError as exc:st.info(str(exc));return
    rows=[]
    for model in models:
        if not model.get('enabled',True):continue
        row={'Model':PRESET_NAMES.get(model['id'],model['name']),'Tasks':', '.join(model.get('tasks',[])),
             'Deployment':model.get('deployment',''), 'Readiness':'Marked ready' if model.get('ready') else 'Needs setup'}
        if model.get('estimated_memory_gb') is not None:row['Memory estimate (GB)']=model['estimated_memory_gb']
        rows.append(row)
    if rows:st.dataframe(rows,hide_index=True,width='stretch')
    else:st.info('No active catalog entries. An administrator can initialize the three preset entries below.')
    if not rows and st.session_state.get('_identity',('', ''))[1]=='admin':
        if st.button('Initialize preset catalog'):
            api.request('POST','/model-presets/initialize');st.rerun()


def render_advisor(api,user,assistant,status):
    st.subheader('Choose a profile for your use case')
    state=status.get('state','unavailable');status_badge(status,'Advisor')
    if state=='demo':st.warning('DEMO ADVISOR · Uses explicit filters, not an LLM. Fill in the requirements below.')
    elif state not in {'configured'}:st.text(status.get('setup_message','The advisor is not ready.'))
    st.caption('Recommendations use the supported catalog and your requirements. Your documents stay out of advisor chat.')
    if assistant and assistant.get('purpose'):
        if st.button('Use this assistant’s purpose'):
            prompt_task=task_id(assistant.get('system_prompt'))
            task_map={'troubleshooting':'document_qa','writing':'general'}
            st.session_state['advisor_draft']='Recommend a supported model for this assistant: '+assistant['purpose'][:1700]
            requirements=dict(st.session_state.get('advisor_requirements',{}))
            requirements['task']=task_map.get(prompt_task,prompt_task) or 'document_qa'
            st.session_state['advisor_requirements']=requirements
            st.session_state['advisor_revision']=st.session_state.get('advisor_revision',0)+1;st.rerun()
    with st.expander('Compare the supported catalog'):
        render_catalog(api)
    if status.get('active_catalog_count')==0:st.info('No active catalog entries. An administrator needs to configure them.')
    if st.button('Start new advisor conversation'):
        for key in ['advisor_messages','advisor_last','advisor_requirements','advisor_error','advisor_draft']:st.session_state.pop(key,None)
        st.session_state['advisor_revision']=st.session_state.get('advisor_revision',0)+1;st.rerun()
    for message in st.session_state.get('advisor_messages',[]):
        with st.chat_message(message['role']):st.text(message['content'])
    if st.session_state.get('advisor_error'):st.error(st.session_state['advisor_error'])
    revision=st.session_state.get('advisor_revision',0)
    submit,message,requirements=render_advisor_form(st.session_state.get('advisor_requirements',{}),revision,bool(status.get('can_request_advice')),state)
    if submit:submit_advice(api,message,requirements,revision)
    render_recommendations(api,user,assistant)


def render_diagnostics(api,user,assistant,runtime,advisor):
    st.subheader('See which part is ready')
    for col,title,status in zip(st.columns(3),['Backend access','Document chat','Model advisor'],
            [{'state':'available','available':True,'connection_checked':True},runtime,advisor]):
        with col,st.container(border=True):
            st.subheader(title);status_badge(status,title)
    st.caption('Configured means settings exist; it is not a connectivity or model-quality test. Setup status may be up to 30 seconds old.')
    with st.expander('Setup details'):
        st.text(runtime.get('message','Runtime status is unavailable.'))
        st.text(advisor.get('setup_message','Advisor status is unavailable.'))
    if st.button('Refresh connection status'):invalidate_metadata();st.rerun()
    if user['role']=='admin' and assistant:
        if st.button('Check document runtime'):
            try:
                with st.spinner('Checking the selected runtime…'):check=api.request('POST','/runtime/check',json={'assistant_id':assistant['id']})
            except APIError as exc:st.error(str(exc))
            else:
                status_badge(check,'Runtime probe');st.text(check.get('message','No status returned.'))
        st.page_link('pages/model_settings.py',label='Open profile settings',icon='⚙️')


def main():
    setup('Help & model advisor');api,user=connect();show_notice()
    hero('Find the right fit', 'Compare supported models, get advice for your task, and understand how your workspace works.', 'GUIDANCE / MODEL ADVISOR')
    try:assistants=get_assistants(api)
    except APIError:assistants=[]
    assistant=pick_assistant(assistants,key='help_profile') if assistants else None
    advisor=read_status(api,'/model-selector/status')
    runtime=read_status(api,'/runtime/status',{'assistant_id':assistant['id']} if assistant else {})
    guide,advice,checks=st.tabs(['Quick help','Model advisor','Connection checks'])
    with guide:render_guide()
    with advice:render_advisor(api,user,assistant,advisor)
    with checks:render_diagnostics(api,user,assistant,runtime,advisor)
    st.page_link('home.py',label='Return to workspace',icon='💬')


main()
