"""Review versioned ticket drafts; only an explicit click approves and executes."""
import streamlit as st
from api_client import APIError,resource
from ui_common import plain


def render_actions(api,message):
    if message.get('redacted') or message.get('stale_sources'):return
    for proposal in message.get('actions',[]):
        try:
            action=api.request('GET','/actions/'+resource(proposal['id']))
            render_action(api,action)
        except APIError as exc:st.warning(str(exc))


def render_action(api,action):
    aid=action['id'];version=action['version'];prefix='/actions/'+resource(aid)
    with st.container(border=True):
        st.caption('ACTION REVIEW · '+plain(action['status']).upper())
        st.subheader('Create a support ticket' if action['tool']=='create_ticket' else 'Proposed action')
        st.caption(f'Version {version} · approval executes this saved version')
        st.json(action['arguments'])
        if action.get('result') is not None:st.json(action['result'])
        if action['status']!='draft':return
        if action['tool']!='create_ticket':
            st.info('This tool needs a dedicated review interface before it can be approved here.');return
        with st.expander('Edit ticket draft'):
            args=action['arguments']
            with st.form('action_edit_'+aid+'_'+str(version)):
                title=st.text_input('Ticket title',value=args.get('title',''),max_chars=120)
                description=st.text_area('Ticket description',value=args.get('description',''),max_chars=2000)
                severities=['low','medium','high'];severity=st.selectbox('Severity',severities,index=severities.index(args.get('severity','medium')))
                save=st.form_submit_button('Save ticket draft')
            if save:
                if not title.strip() or not description.strip():st.warning('Enter a title and description.');return
                api.request('PATCH',prefix,json={'version':version,'arguments':{'title':title.strip(),'description':description.strip(),'severity':severity}})
                st.session_state['notice']='Ticket draft updated. Review the new version before approval.';st.rerun()
        confirm=st.checkbox('I reviewed this version and want to create the ticket',key=f'approve_{aid}_{version}')
        approve,cancel=st.columns(2)
        if approve.button('Approve & create ticket',key='execute_'+aid,disabled=not confirm,type='primary',width='stretch'):
            latest=api.request('GET',prefix)
            if latest['status']!='draft' or latest['version']!=version:
                st.warning('This draft changed. Refresh the conversation and review the current version.');return
            api.request('POST',prefix+'/approve',json={'version':version})
            st.session_state['notice']='Approval processed. Check the action status and result below.';st.rerun()
        if cancel.button('Cancel ticket draft',key='cancel_'+aid,width='stretch'):
            api.request('POST',prefix+'/cancel')
            st.session_state['notice']='Ticket draft cancelled.';st.rerun()
