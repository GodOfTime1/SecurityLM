import streamlit as st
from datetime import datetime
import time
import base64

# 1. Page Configuration
st.set_page_config(page_title="Document Upload", layout="wide")

# 2. Custom CSS
st.markdown("""
    <style>
    /* Hide default Streamlit sidebar navigation */
    [data-testid="stSidebarNav"] {display: none;}
    
    /* 20px font for sidebar navigation links */
    [data-testid="stPageLink-NavLink"] p {
        font-size: 20px !important;
    }
    
    /* Circular Plus Button Styling */
    div[data-testid="stButton"] > button[key="open_upload_dialog"] {
        border-radius: 50% !important;
        width: 45px !important;
        height: 45px !important;
        font-size: 24px !important;
        font-weight: bold !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        padding: 0 !important;
        line-height: 1 !important;
    }

    /* Styling for the card options (three-dot) button */
    div[data-testid="stButton"] > button[key^="options_"] {
        border: none !important;
        background: transparent !important;
        font-size: 20px !important;
        padding: 0 5px !important;
        float: right !important;
    }
    </style>
""", unsafe_allow_html=True)

# Shared memory initialization
if "documents" not in st.session_state:
    st.session_state.documents = [] 

# 3. Sidebar UI (Identical to home.py)
with st.sidebar:
    st.markdown("""
        <div style="text-align: left; margin-bottom: 10px;">
            <div style="font-size: 30px; font-weight: bold;">🛡️ SecurityLM</div>
            <div style="font-size: 20px; color: gray;">Secure Local AI Auditor</div>
        </div>
    """, unsafe_allow_html=True)
    st.divider()
    
    st.page_link("home.py", label="SLM", icon="🏠")
    st.page_link("pages/Document_Upload.py", label="Document Upload", icon="📄")
    st.page_link("pages/Help.py", label="Help", icon="❓")

# 4. Upload Dialog (Pop-up Window with Full-Width Warning Banner)
@st.dialog("Upload Documents")
def upload_modal():
    st.write("Click to browse files or drag and drop files")
    
    uploaded_files = st.file_uploader(
        "Upload files", 
        accept_multiple_files=True, 
        label_visibility="collapsed"
    )
    
    progress_container = st.container()
    
    col1, col2 = st.columns([3, 1])
    with col2:
        # Capture button click event inside column 2
        upload_clicked = st.button("Upload", type="primary", use_container_width=True)
        
    # Validation logic outside the column layout so warnings span 100% modal width
    if upload_clicked:
        if uploaded_files:
            with progress_container:
                st.divider()
                for file in uploaded_files:
                    if not any(d['original_name'] == file.name for d in st.session_state.documents):
                        st.markdown(f"**Uploading:** `{file.name}`")
                        file_progress_bar = st.progress(0)
                        
                        for percent in range(100):
                            time.sleep(0.005)
                            file_progress_bar.progress(percent + 1)
                        
                        file_bytes = file.getvalue()
                        st.session_state.documents.append({
                            "original_name": file.name,
                            "display_name": file.name,
                            "date": datetime.now().strftime("%B %d, %Y"),
                            "bytes": file_bytes,
                            "type": file.type
                        })
            st.rerun()
        else:
            # Full-width warning banner
            st.warning("Please select at least one file first.")

# 5. Browser-Native PDF / Document Viewer Pop-up Dialog
@st.dialog("Visual Document Reader", width="large")
def view_document_modal(doc):
    st.subheader(f"📖 {doc['display_name']}")
    st.caption(f"Added on: {doc['date']}")
    
    if doc['display_name'].lower().endswith(".pdf"):
        base64_pdf = base64.b64encode(doc["bytes"]).decode("utf-8")
        pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="650px" style="border: none;"></iframe>'
        st.markdown(pdf_display, unsafe_allow_html=True)
    else:
        try:
            text_content = doc["bytes"].decode("utf-8", errors="ignore")
            st.text_area("File Content", text_content, height=450)
        except Exception:
            st.info("Visual preview unavailable for this file format.")

# 6. Document Options Modal Dialog (Auto-closing Dialog)
@st.dialog("Manage Document")
def document_options_modal(index):
    doc = st.session_state.documents[index]
    st.markdown(f"**Original Name:** `{doc['original_name']}`")
    
    new_name = st.text_input("Rename Document", value=doc['display_name'])
    
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Save Name", type="primary", use_container_width=True):
            if new_name.strip():
                st.session_state.documents[index]['display_name'] = new_name.strip()
            st.rerun()  # Instantly updates state & closes modal
            
    with col2:
        if st.button("🗑️ Delete", use_container_width=True):
            st.session_state.documents.pop(index)
            st.rerun()  # Instantly deletes document & closes modal

# 7. Header Area with Title & Circular Plus Button
head_col1, head_col2 = st.columns([8, 1], vertical_alignment="center")

with head_col1:
    st.markdown('<h1 style="margin: 0; padding: 0; font-size: 32px;">📁 Document Upload</h1>', unsafe_allow_html=True)

with head_col2:
    if st.button("＋", key="open_upload_dialog"):
        upload_modal()

st.write("Add files here for the SLM to analyze.")
st.divider()

# 8. Document Display Vault Grid
if not st.session_state.documents:
    st.info("Your vault is currently empty. Click the ＋ button above to upload documents.")
else:
    cols = st.columns(4)
    
    for i, doc in enumerate(st.session_state.documents):
        with cols[i % 4]:
            with st.container(border=True):
                p_col1, p_col2 = st.columns([3, 1], vertical_alignment="top")
                with p_col1:
                    st.markdown("<h1 style='margin: 0; padding: 0; font-size: 40px;'>📄</h1>", unsafe_allow_html=True)
                with p_col2:
                    if st.button("⋮", key=f"options_{i}"):
                        document_options_modal(i)

                st.markdown(
                    f'''
                    <div style="margin-top: 5px; margin-bottom: 2px; font-weight: bold; font-size: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                        {doc['display_name']}
                    </div>
                    <div style="margin-top: 0px; margin-bottom: 12px; color: #a0a0a0; font-size: 13px;">
                        Added: {doc['date']}
                    </div>
                    ''', 
                    unsafe_allow_html=True
                )
                
                if st.button("Open", key=f"open_{i}", use_container_width=True):
                    view_document_modal(doc)