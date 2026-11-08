"""Persisted documents, explicit assistant collections, and extracted text previews."""
import streamlit as st
from api_client import APIError, resource
from ui_common import connect, get_assistants, pick_assistant, setup, show_notice

setup("Documents")
api, user = connect()
show_notice()
st.caption("ASSISTANT KNOWLEDGE")
st.title("Add your documents")
st.write("Upload your guides, then link the ones your assistant should use.")
st.page_link("home.py", label="Return to workspace", icon="💬")

try:
    documents = api.all_items("/documents", "documents")
    if user["role"] == "admin":
        assistant = pick_assistant(get_assistants(api), key="documents_profile")
        st.subheader("1. Upload documents")
        with st.expander("Choose files to upload", expanded=not documents):
            with st.form("upload", clear_on_submit=True):
                uploads = st.file_uploader("PDF, TXT, or MD · maximum 10 MiB each",
                                           type=["pdf", "txt", "md"], accept_multiple_files=True)
                visibility = st.selectbox("Who can read these documents?", ["admin", "employee"])
                st.caption("Admin is the restricted default. Employee documents are also visible to admins.")
                submit = st.form_submit_button("Upload to backend", type="primary")
            if submit:
                if not uploads:
                    st.warning("Select at least one file.")
                else:
                    results = []
                    with st.spinner("Uploading and extracting text…"):
                        for upload in uploads:
                            try:
                                result = api.upload(upload.name, upload.getvalue(), visibility)
                                results.append((upload.name, True, result.get("warnings", [])))
                            except APIError as exc:
                                results.append((upload.name, False, [str(exc)]))
                    # Preserve partial successes; never blindly retry successful uploads.
                    st.session_state["upload_results"] = results
                    st.rerun()
        for name, success, warnings in st.session_state.pop("upload_results", []):
            if success:
                st.success("Document uploaded. Link it to your assistant in step 2.")
                st.text(name)
            else:
                st.error("Not confirmed: " + name)
            for warning in warnings:
                st.warning(warning)
        with st.expander("If an upload is interrupted"):
            st.caption("Refresh the document list before trying again so you do not upload a duplicate.")

        st.subheader("2. Link documents to your assistant")
        with st.expander("Choose linked documents", expanded=bool(assistant) and not assistant["document_ids"]):
            if assistant:
                labels = {d["id"]: f"{d['filename']} · {d['visibility']} · {d['id'][:8]}" for d in documents}
                with st.form("collection_" + assistant["id"]):
                    selected = st.multiselect("Attached documents", list(labels),
                        default=[i for i in assistant["document_ids"] if i in labels], format_func=lambda i: labels[i])
                    st.caption("Saving replaces this shared assistant's collection with exactly this selection. Coordinate edits with other admins.")
                    save = st.form_submit_button("Save document collection", type="primary")
                if save:
                    api.request("PUT", f"/assistants/{resource(assistant['id'])}/documents", json={"document_ids": selected})
                    st.session_state["notice"] = f"Document links saved: {len(selected)} linked to this assistant. Return to the workspace to chat."
                    st.rerun()
    else:
        st.info("You can read permitted documents. Uploading, replacing, and deleting require an administrator.")

    st.divider()
    st.subheader("Document library")
    st.caption("Previews show extracted text. Access is checked by the backend.")
    if st.button("Refresh documents"):
        st.rerun()
    if not documents:
        st.info("No documents are available to your account.")
        st.stop()
    for start in range(0, len(documents), 4):
        for col, doc in zip(st.columns(4), documents[start:start + 4]):
            with col, st.container(border=True):
                st.subheader("📄 " + doc["filename"])
                st.caption(f"{doc['visibility']} · version {doc['version']} · {doc['page_count']} pages")
                if st.button("Open", key="open_" + doc["id"]):
                    st.session_state["preview_document"] = doc["id"]
                    st.rerun()

    did = st.session_state.get("preview_document")
    doc = next((d for d in documents if d["id"] == did), None)
    if doc:
        st.divider()
        st.subheader(doc["filename"])
        page = st.number_input("Preview batch (20 passages each)", min_value=1, value=1, step=1, key="batch_" + did)
        chunks = api.request("GET", f"/documents/{resource(did)}/chunks", params={"limit": 20, "offset": (page - 1) * 20})["chunks"]
        if not chunks:
            st.info("No passages in this batch.")
        for chunk in chunks:
            st.caption(f"Page {chunk['page_number']} · passage {chunk['chunk_id'][:8]}")
            st.text(chunk["text"])
        if user["role"] == "admin":
            with st.expander("Replace document"):
                with st.form("replace_" + did, clear_on_submit=True):
                    replacement = st.file_uploader("Replacement PDF, TXT, or MD", type=["pdf", "txt", "md"])
                    st.caption("Keeps document access and assistant links. Historical answers become stale; old action drafts cannot execute.")
                    replace = st.form_submit_button("Replace this version")
                if replace and replacement:
                    data = replacement.getvalue()
                    if not data or len(data) > 10 * 1024 * 1024:
                        st.error("Choose a nonempty file no larger than 10 MiB.")
                    else:
                        api.request("PUT", f"/documents/{resource(did)}", files={"file": (replacement.name, data)},
                                    data={"expected_version": str(doc["version"])})
                        st.rerun()
            with st.expander("Delete document"):
                confirmed = st.checkbox("Delete this document and withdraw its source-dependent answers", key="confirm_" + did)
                if st.button("Delete permanently", disabled=not confirmed, key="delete_" + did):
                    api.request("DELETE", f"/documents/{resource(did)}")
                    st.session_state.pop("preview_document", None)
                    st.rerun()
except APIError as exc:
    st.error(str(exc))
