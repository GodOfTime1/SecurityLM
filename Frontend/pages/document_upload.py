"""Document library with optional upload-and-link using the existing API contract."""
import streamlit as st
from api_client import APIError,resource,validate_file
from constants import FILE_TYPES,MAX_UPLOAD_MB,PREVIEW_BATCH_SIZE
from document_service import upload_batch,link_documents
from session_cache import invalidate_metadata
from ui_common import setup,connect,show_notice,get_assistants,pick_assistant,heading,plain,hero


def render_upload_result(api):
    result=st.session_state.get('upload_result')
    if result:
        succeeded=sum(item['uploaded'] for item in result['files'])
        st.subheader(f"Last upload · {succeeded} of {len(result['files'])} confirmed")
        for item in result['files']:
            (st.success if item['uploaded'] else st.error)('Document uploaded.' if item['uploaded'] else 'Upload not confirmed.')
            st.text(item['name'])
            for warning in item['warnings']:st.text(warning)
        if result['linked']:st.success('Uploaded documents are linked to your assistant. You can start chatting.')
        elif result['link_error'] and not result.get('link_resolved'):
            st.warning('Upload succeeded, but linking was not confirmed. Retry linking below; do not upload the files again.')
            st.session_state['pending_document_links']={'assistant_id':result['assistant_id'],'ids':result['uploaded_ids']}
        if any(not item['uploaded'] for item in result['files']):
            st.warning('Refresh the library before retrying unconfirmed files. A lost response can mean the upload already exists.')
    pending=st.session_state.get('pending_document_links')
    if pending:
        retry,leave=st.columns(2)
        if retry.button('Retry linking uploaded documents'):
            try:link_documents(api,pending['assistant_id'],pending['ids'])
            except APIError as exc:st.error(str(exc))
            else:
                st.session_state.pop('pending_document_links',None)
                st.session_state['upload_result']['link_resolved']=True
                st.session_state['upload_result']['linked']=True
                st.session_state.pop('collection_snapshot_'+pending['assistant_id'],None)
                st.session_state.pop('collection_selection_'+pending['assistant_id'],None)
                st.session_state['notice']='Uploaded documents linked successfully.'
                st.rerun()
        if leave.button('Leave documents unlinked'):
            st.session_state.pop('pending_document_links',None)
            st.session_state['upload_result']['link_resolved']=True
            st.rerun()
    if result and not st.session_state.get('pending_document_links'):
        if st.button('Dismiss upload summary'):
            st.session_state.pop('upload_result',None);st.rerun()


def render_upload(api,assistant,documents):
    if st.session_state.get('pending_document_links'):
        st.info('Resolve the pending document links above before uploading another batch.')
        return
    with st.expander('Upload documents',expanded=not documents):
        with st.form('upload',clear_on_submit=True):
            uploads=st.file_uploader(f'PDF, TXT, or MD · maximum {MAX_UPLOAD_MB} MiB each',
                type=list(FILE_TYPES),accept_multiple_files=True,max_upload_size=MAX_UPLOAD_MB)
            visibility=st.selectbox('Who can read these documents?',['admin','employee'],
                format_func=lambda role: 'Administrators only' if role=='admin' else 'Administrators and employees')
            link=st.checkbox(plain('Also link to '+assistant['name']) if assistant else 'Also link to an assistant',
                value=bool(assistant),disabled=not bool(assistant))
            st.caption('Admin is the restricted default. Linked documents become available to this assistant according to each reader’s permissions.')
            submit=st.form_submit_button('Upload documents',type='primary')
        if submit:
            if not uploads:st.warning('Select at least one file.')
            else:
                progress=st.progress(0,text='Validating selected files…')
                with st.spinner('Uploading your documents…'):
                    result=upload_batch(api,uploads,visibility,assistant['id'] if assistant and link else None,
                        on_progress=lambda done,total:progress.progress(done/total,text=f'Processed {done} of {total} files'))
                st.session_state['upload_result']=result
                if result['linked']:
                    st.session_state.pop('collection_snapshot_'+result['assistant_id'],None)
                    st.session_state.pop('collection_selection_'+result['assistant_id'],None)
                st.rerun()
        st.caption('If a response is lost, refresh the library before retrying an upload.')


def render_collection(api,assistant,documents):
    with st.expander('Manage linked documents',expanded=bool(assistant) and not assistant['document_ids']):
        if not assistant:
            st.page_link('home.py',label='Create an assistant profile first',icon='🏠');return
        labels={d['id']:f"{d['filename']} · {d['visibility']}" for d in documents}
        snapshot_key='collection_snapshot_'+assistant['id']
        selection_key='collection_selection_'+assistant['id']
        baseline=st.session_state.setdefault(snapshot_key,list(assistant['document_ids']))
        hidden=[did for did in baseline if did not in labels]
        if hidden:st.caption(f'{len(hidden)} linked document(s) are unavailable in this view. Their links will be preserved.')
        if st.button('Reload document selection',key='reload_'+assistant['id']):
            st.session_state.pop(snapshot_key,None);st.session_state.pop(selection_key,None);st.rerun()
        with st.form('collection_'+assistant['id']):
            selected=st.multiselect('Attached documents',list(labels),default=[i for i in baseline if i in labels],
                format_func=lambda i:labels[i],key=selection_key)
            st.caption('Choose the knowledge this assistant can use. Unchecking a document removes its link; the document stays in the library.')
            save=st.form_submit_button('Save document collection')
        if save:
            current=api.request('GET','/assistants/'+resource(assistant['id']))
            if set(current['document_ids']) != set(baseline):
                st.warning('The assistant’s document collection changed while you were editing. Reload document selection, review the current links, and save again.')
                return
            linked=list(dict.fromkeys(selected+hidden))
            api.request('PUT','/assistants/'+resource(assistant['id'])+'/documents',json={'document_ids':linked})
            st.session_state.pop(snapshot_key,None)
            st.session_state['notice']=f'Document links saved: {len(linked)} linked. Return to the workspace to chat.'
            st.rerun()


def render_preview(api,user,doc):
    did=doc['id'];heading(doc['filename'],2)
    st.caption('Extracted text · original PDF layout is not retained by this service.')
    batch=st.number_input(f'Preview batch ({PREVIEW_BATCH_SIZE} passages each)',min_value=1,value=1,step=1,key='batch_'+did)
    chunks=api.request('GET','/documents/'+resource(did)+'/chunks',params={'limit':PREVIEW_BATCH_SIZE,'offset':(batch-1)*PREVIEW_BATCH_SIZE})['chunks']
    if not chunks:st.info('No passages in this batch.')
    for index,chunk in enumerate(chunks):
        with st.expander(f"Page {chunk['page_number']} · passage {(batch-1)*PREVIEW_BATCH_SIZE+index+1}",expanded=index==0):
            st.text(chunk['text'])
    if user['role']!='admin':return
    with st.expander('Replace document'):
        with st.form('replace_'+did,clear_on_submit=True):
            replacement=st.file_uploader('Replacement PDF, TXT, or MD',type=list(FILE_TYPES),max_upload_size=MAX_UPLOAD_MB)
            st.caption('Keeps existing access and assistant links. Earlier answers may become stale.')
            replace=st.form_submit_button('Replace this version')
        if replace:
            if replacement is None:st.warning('Select a replacement file.')
            else:
                data=replacement.getvalue();validate_file(replacement.name,data)
                api.request('PUT','/documents/'+resource(did),files={'file':(replacement.name,data)},data={'expected_version':str(doc['version'])})
                st.session_state['notice']='Document replaced successfully.';st.rerun()
    with st.expander('Delete document'):
        with st.form('delete_'+did):
            confirmed=st.checkbox('Delete this document and withdraw its source-dependent answers')
            delete=st.form_submit_button('Delete permanently')
        if delete:
            if not confirmed:st.warning('Confirm deletion first.')
            else:
                api.request('DELETE','/documents/'+resource(did))
                st.session_state.pop('preview_document',None)
                st.session_state['notice']='Document deleted.';st.rerun()


def render_library(api,user,documents):
    st.subheader('Document library')
    if st.button('Refresh documents'):invalidate_metadata();st.rerun()
    if not documents:st.info('No documents are available to your account.');return
    search,access=st.columns([3,1])
    query=search.text_input('Find a document',placeholder='Search filenames…',key='document_search')
    scope=access.selectbox('Access filter',['All documents','Administrators only','Employees too'])
    filtered=[doc for doc in documents if query.casefold() in doc['filename'].casefold() and
              (scope=='All documents' or doc['visibility']==('admin' if scope=='Administrators only' else 'employee'))]
    st.caption(f'{len(filtered)} of {len(documents)} documents match · open a document to inspect extracted text')
    if filtered:
        pages=max(1,(len(filtered)+11)//12)
        signature=(query,scope,tuple(d['id'] for d in filtered))
        if st.session_state.get('_document_filter') != signature:
            st.session_state['document_page']=1;st.session_state['_document_filter']=signature
        page=st.number_input('Library page',min_value=1,max_value=pages,step=1,key='document_page')
        visible=filtered[(page-1)*12:page*12]
    else:
        st.info('No filenames match these filters. Clear the search or change the access filter.')
        visible=[]
    for start in range(0,len(visible),3):
        for col,doc in zip(st.columns(3),visible[start:start+3]):
            with col,st.container(border=True):
                heading(doc['filename'],3)
                access_label='Administrators only' if doc['visibility']=='admin' else 'Employees too'
                st.caption(f"{access_label} · v{doc['version']} · {doc['page_count']} pages")
                if st.button('Open',key='open_'+doc['id']):
                    st.session_state['preview_document']=doc['id'];st.rerun()
    selected=next((doc for doc in documents if doc['id']==st.session_state.get('preview_document')),None)
    if selected:
        st.divider()
        if st.button('Close preview'):
            st.session_state.pop('preview_document',None);st.rerun()
        render_preview(api,user,selected)
    else:st.session_state.pop('preview_document',None)


def main():
    setup('Documents');api,user=connect();show_notice()
    hero('Your document library', 'Connect the knowledge your assistant needs. Control access and verify the extracted text.', 'KNOWLEDGE / SOURCES')
    st.page_link('home.py',label='Return to workspace',icon='💬')
    try:
        documents=api.all_items('/documents','documents')  # Always permission-filtered afresh.
        if user['role']=='admin':
            assistant=pick_assistant(get_assistants(api),key='documents_profile')
            render_upload_result(api);render_upload(api,assistant,documents);render_collection(api,assistant,documents)
        else:
            st.session_state.pop('upload_result',None);st.session_state.pop('pending_document_links',None)
            st.caption('You can read permitted documents. Document changes require an administrator.')
        st.divider();render_library(api,user,documents)
    except APIError as exc:st.error(str(exc))


main()
