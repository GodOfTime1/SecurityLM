"""Assistant task instructions and runtime selection through existing backend APIs."""
import streamlit as st
from api_client import APIError,resource
from constants import PRESET_IDS
from prompting import TASKS,build_instructions,task_id,extra_guidance
from ui_common import (setup,connect,show_notice,get_assistants,pick_assistant,runtime_banner,
                       preset_cards,status_badge,heading,hero)


def render_behavior(api,assistant):
    aid=assistant['id'];snapshot_key='behavior_snapshot_'+aid
    fields=('name','purpose','system_prompt')
    snapshot=st.session_state.setdefault(snapshot_key,{k:assistant.get(k,'') for k in fields})
    revision=st.session_state.get('behavior_revision_'+aid,0)
    st.subheader('Give the assistant a job')
    st.write('Choose a task and add your team’s guidance.')
    if st.button('Reload saved behavior'):
        st.session_state.pop(snapshot_key,None)
        st.session_state['behavior_revision_'+aid]=revision+1;st.rerun()
    with st.form('behavior_'+aid+'_'+str(revision)):
        name=st.text_input('Assistant name',value=snapshot['name'],max_chars=100)
        purpose=st.text_area('Purpose & audience',value=snapshot['purpose'],max_chars=2000,
            help='Describe the problem, audience, and desired result. This is sent to the model as the assistant purpose.')
        current=task_id(snapshot['system_prompt']) or 'document_qa'
        task=st.selectbox('Primary task',list(TASKS),index=list(TASKS).index(current),format_func=lambda x:TASKS[x]['name'])
        extra=st.text_area('Additional guidance',value=extra_guidance(snapshot['system_prompt']),height=170,max_chars=6500,
            help='For example: explain acronyms, use numbered steps, and ask which operating system the employee uses.')
        st.caption('Answers use your linked sources and review claims before responding.')
        save=st.form_submit_button('Save assistant behavior',type='primary')
    if save:
        if not name.strip():st.warning('Give the assistant a name.');return
        try:prompt=build_instructions(task,extra)
        except ValueError as exc:st.warning(str(exc));return
        current=api.request('GET','/assistants/'+resource(aid))
        if any(current.get(k,'')!=snapshot[k] for k in fields):
            st.warning('Assistant behavior changed while you were editing. Reload saved behavior and review before saving.');return
        api.request('PATCH','/assistants/'+resource(aid),json={'name':name.strip(),'purpose':purpose.strip(),'system_prompt':prompt})
        st.session_state.pop(snapshot_key,None);st.session_state['behavior_revision_'+aid]=revision+1
        st.session_state['notice']='Assistant behavior saved. New replies will use the updated task instructions.';st.rerun()
    with st.expander('Inspect the instructions sent to the model'):
        st.caption('Saved assistant instructions. Backend citation and access rules are added separately.')
        st.code(assistant.get('system_prompt') or 'No assistant-specific instructions saved.',language=None)


def render_tools(api,assistant):
    st.subheader('Let the assistant propose useful work')
    st.write('Let your assistant propose tasks. You review and approve each one.')
    tools=api.request('GET','/tools')['tools']
    path='/assistants/'+resource(assistant['id'])+'/tools'
    saved=api.request('GET',path)['tools']
    snapshot_key='tools_snapshot_'+assistant['id']
    baseline=st.session_state.setdefault(snapshot_key,list(saved))
    if st.button('Reload tool selection'):
        st.session_state.pop(snapshot_key,None);st.session_state.pop('tool_selection_'+assistant['id'],None);st.rerun()
    labels={t['name']:t['description'] for t in tools}
    with st.form('tools_'+assistant['id']):
        selected=st.multiselect('Enabled tools',list(labels),default=[x for x in baseline if x in labels],
            format_func=lambda x:labels[x],key='tool_selection_'+assistant['id'])
        save=st.form_submit_button('Save tool access')
    if save:
        if set(api.request('GET',path)['tools'])!=set(baseline):
            st.warning('Tool access changed while you were editing. Reload tool selection and review.');return
        api.request('PUT',path,json={'tools':selected})
        st.session_state.pop(snapshot_key,None)
        st.session_state['notice']='Tool access saved. Proposals still require user approval.';st.rerun()


def render_settings(api,user,assistant):
    behavior,models,tools,runtime=st.tabs(['Task & instructions','Model & reply budget','Tools','Runtime checks'])
    with behavior:render_behavior(api,assistant)
    with models:
        st.subheader('Choose a reply length')
        st.write('Short answers or more detail—the choice is yours.')
        preset_cards(api,user,assistant)
        with st.expander('Token limits & compatibility'):
            st.caption('Input cap: 8,192 tokens. Requested reply limits: 128, 512, and 1,024 tokens.')
        models=[model for model in api.request('GET','/models')['models'] if model['enabled'] and model['id'] not in PRESET_IDS]
        if models:st.subheader('Other configured catalog entries')
        for model in models:
            with st.container(border=True):
                heading(model['name'],3);st.text(model.get('description',''))
                if model.get('estimated_memory_gb') is not None:st.caption(f"Estimated memory: {model['estimated_memory_gb']:g} GB")
                st.caption('Marked ready by admin' if model.get('ready') else 'Needs runtime setup')
                if st.button('Save model choice',key='model_'+model['id'],width='stretch'):
                    api.request('POST','/model-selector/apply',json={'assistant_id':assistant['id'],'model_id':model['id'],
                        'catalog_version':model['version'],'expected_current_model':assistant['model']})
                    st.session_state['notice']='Model choice saved.';st.rerun()
    with tools:render_tools(api,assistant)
    with runtime:
        st.subheader('Verify the runtime')
        runtime_banner(api,assistant['id'],full=True)
        st.caption('Saved backend selection: '+(assistant['model'] or 'Backend default'))
        if st.button('Check runtime connection'):
            with st.spinner('Checking model availability…'):check=api.request('POST','/runtime/check',json={'assistant_id':assistant['id']})
            status_badge(check,'Runtime probe');st.text(check.get('message','No status returned.'))


def main():
    setup('Assistant setup');api,user=connect()
    if user['role']!='admin':st.info('Assistant setup is managed by an administrator.');st.stop()
    show_notice();hero('An assistant with a purpose', 'Define its job, connect the right model profile, and check the runtime.', 'CONFIGURE / ASSISTANT SETUP')
    try:
        assistant=pick_assistant(get_assistants(api),key='settings_profile')
        if assistant:render_settings(api,user,assistant)
        else:st.page_link('home.py',label='Create an assistant profile',icon='🏠')
    except APIError as exc:st.error(str(exc))
    st.page_link('home.py',label='Return to workspace',icon='💬')


main()
