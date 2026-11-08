"""Guided assistant setup and a focused, persisted chat workspace."""
import streamlit as st
from api_client import APIError, new_turn, resource
from ui_common import (connect, get_assistants, pick_assistant, profile_form,
                       render_message, setup, show_notice, runtime_banner, preset_cards, existing_page)

setup("Workspace")
api, user = connect()
show_notice()

try:
    assistants = get_assistants(api)
    if not assistants:
        st.caption("WELCOME TO SECURITYLM")
        st.title("Build your document assistant")
        st.write("Choose a preset. Add your knowledge. Ask better questions.")
        runtime_banner(api)
        preset_cards(api,user)
        if user["role"] == "admin":
            with st.container(border=True):
                preset=st.session_state.get('draft_preset')
                st.subheader("Name your "+preset['name']+" assistant" if preset else "Create your assistant profile")
                profile_form(api,preset=preset)
            st.caption("Creating a profile saves its configuration. It does not train or launch a model.")
        else:
            st.info("An administrator needs to create an assistant profile before you can chat.")
        st.stop()

    with st.sidebar:
        assistant = pick_assistant(assistants, key="workspace_profile")
        if user["role"] == "admin":
            with st.expander("New assistant profile"):
                profile_form(api)
    aid = assistant["id"]
    conversations = [c for c in api.all_items("/conversations", "conversations") if c["assistant_id"] == aid]

    st.caption("SECURITYLM / WORKSPACE")
    st.title(assistant["name"])
    if assistant["purpose"]:
        st.text(assistant["purpose"])
    st.page_link("pages/help.py",label="Need help choosing a model?",icon="❓")
    runtime=runtime_banner(api,aid)
    preset_name=assistant['model'].replace('preset:','')
    preset_label={'snake2':'Snake2','llama4':'Llama4','megladon8':'Megladon8'}.get(preset_name,'Custom model' if preset_name else 'Choose a preset')
    cols=st.columns(3)
    for col,label,value in zip(cols,['Assistant preset','Linked documents','Conversations'],
                               [preset_label,str(len(assistant['document_ids'])),str(len(conversations))]):
        with col,st.container(border=True):
            st.metric(label,value)
    with st.expander('Your three preset options',expanded=not bool(assistant['model'])):
        preset_cards(api,user,assistant)

    if not assistant["document_ids"]:
        with st.container(border=True):
            st.subheader("Next: add knowledge to your assistant")
            st.write("Upload a guide or policy, then link it to this profile so it can be retrieved during chat.")
            if user["role"] == "admin":
                documents=existing_page('pages/document_upload.py','pages/Document_Upload.py')
                if documents:
                    st.page_link(documents, label="Add and link documents", icon="📄")
                else:
                    st.info('Add your document page under pages/ to manage assistant knowledge.')
            else:
                st.caption("Ask an administrator to link documents you are permitted to read.")

    st.divider()
    title_col, new_col = st.columns([4, 1])
    title_col.subheader("Conversation")
    if new_col.button("New conversation", type="primary", use_container_width=True):
        conv = api.request("POST", "/conversations", json={"assistant_id": aid})
        st.session_state["conversation_" + aid] = conv["id"]
        st.session_state["notice"] = "Conversation started. Ask your first question below."
        st.rerun()
    if not conversations:
        with st.container(border=True):
            st.subheader("Your documents, in conversation")
            st.write("Start a conversation to find a policy, compare details, or summarize your linked documents.")
            st.caption("Answers include source passages so you can inspect the evidence.")
        st.stop()

    choices = [c["id"] for c in conversations]
    labels = {c["id"]: (c["title"] or "New conversation") + " · " + c["id"][:8] for c in conversations}
    conv_key = "conversation_" + aid
    if st.session_state.get(conv_key) not in choices:
        st.session_state.pop(conv_key, None)
    with st.sidebar:
        cid = st.selectbox("Conversation", choices, format_func=lambda i: labels[i], key=conv_key)
    conv = api.conversation(cid)
    if st.button("Refresh conversation"):
        st.rerun()
    if not conv["messages"]:
        st.write("What would you like to know?")
        st.caption("Ask a specific question, or ask for a summary of your linked documents.")
    for message in conv["messages"]:
        render_message(message)

    pending_key = "pending_" + cid
    pending = st.session_state.get(pending_key)
    send = False
    if pending:
        st.warning("This message has not been confirmed. Retry safely sends the same request.")
        st.text(pending["content"])
        retry_col, reset_col = st.columns(2)
        send = retry_col.button("Retry pending message")
        if reset_col.button("Discard pending attempt"):
            st.session_state.pop(pending_key, None)
            st.session_state["notice"] = "Pending attempt discarded locally. Check refreshed history before sending it again."
            st.rerun()
    prompt = st.chat_input("Ask about your documents…", disabled=bool(pending), max_chars=4000)
    if prompt and prompt.strip():
        pending = new_turn(prompt, conv["revision"])
        st.session_state[pending_key] = pending
        send = True
    if send:
        with st.spinner("Preparing your reply…"):
            api.request("POST", f"/conversations/{resource(cid)}/messages", json=pending)
        st.session_state.pop(pending_key, None)
        st.rerun()
except APIError as exc:
    st.error(str(exc))
    if any(k.startswith("pending_") for k in st.session_state):
        if st.button("Show pending-message controls"):
            st.rerun()
