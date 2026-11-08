import streamlit as st

# 1. Page Configuration & Memory
st.set_page_config(page_title="SecurityLM", layout="wide")

# Custom CSS for container background contrast, fixed card height, and sidebar page link sizes
st.markdown("""
    <style>
    /* Hides the default Streamlit page sidebar */
    [data-testid="stSidebarNav"] {display: none;}
    
    /* Forces the custom page links in the sidebar to be 20px */
    [data-testid="stPageLink-NavLink"] p {
        font-size: 20px !important;
    }
    
    /* Base model card style: lighter background than page, fixed height to keep boxes aligned */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background-color: #1e2638 !important;
        border-radius: 10px !important;
        min-height: 380px !important;
    }

    /* Style button in workspace header to match status box height and alignment */
    div[data-testid="stButton"] > button {
        height: 40px !important;
        font-size: 15px !important;
        margin: 0 !important;
    }
    </style>
""", unsafe_allow_html=True)

if "selected_model" not in st.session_state:
    st.session_state.selected_model = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "documents" not in st.session_state:
    st.session_state.documents = [] 

# Simulated Backend Connection Status (Change to True once your local model engine is linked)
ai_connected = False

# 2. Custom Sidebar
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

# 3. Main Page Logic
if st.session_state.selected_model is None:
    st.title("Select your SLM model")
    st.markdown('<div style="font-size: 15px; color: #a0a0a0; margin-bottom: 25px;">Choose your preferred small language model.</div>', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns(3)
    
    # Block 1: Snake_2
    with col1:
        with st.container(border=True):
            st.markdown('<div style="font-size: 30px; font-weight: bold; margin-bottom: 15px;">🐍 Snake_2</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Processing Power:</b> 2 GB</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Recommended Model:</b> Standard 8GB RAM laptops</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; color: #2e7d32; font-weight: bold; margin-bottom: 20px;">Useful for small amounts of documents</div>', unsafe_allow_html=True)
            
            if st.button("RUN", key="run_snake", type="primary", use_container_width=True):
                st.session_state.selected_model = "Snake_2"
                st.rerun()

    # Block 2: Llama_4
    with col2:
        with st.container(border=True):
            st.markdown('<div style="font-size: 30px; font-weight: bold; margin-bottom: 15px;">🦙 Llama_4</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Processing Power:</b> 4 GB</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Recommended Model:</b> 16GB RAM laptops</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; color: #2e7d32; font-weight: bold; margin-bottom: 20px;">Useful for medium amounts of documents</div>', unsafe_allow_html=True)
            
            if st.button("RUN", key="run_llama", type="primary", use_container_width=True):
                st.session_state.selected_model = "Llama_4"
                st.rerun()

    # Block 3: Megladon_8
    with col3:
        with st.container(border=True):
            st.markdown('<div style="font-size: 30px; font-weight: bold; margin-bottom: 15px;">🦈 Megladon_8</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Processing Power:</b> 8 GB</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; margin-bottom: 10px;"><b>Recommended Model:</b> Dedicated GPU or Apple Silicon</div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size: 20px; color: #2e7d32; font-weight: bold; margin-bottom: 20px;">Useful for large amounts of documents</div>', unsafe_allow_html=True)
            
            if st.button("RUN", key="run_meg", type="primary", use_container_width=True):
                st.session_state.selected_model = "Megladon_8"
                st.rerun()

else:
    # ---------------------------------------------------------
    # WORKSPACE AREA
    # ---------------------------------------------------------
    
    # Define status styling
    if ai_connected:
        status_bg = "#1b5e20"
        status_color = "#81c784"
        status_border = "#2e7d32"
        status_text = "🟢 Active"
    else:
        status_bg = "#5c1d1d"
        status_color = "#ef9a9a"
        status_border = "#c62828"
        status_text = "🔴 Inactive"

    # Perfect horizontal flex container alignment for Title + Status Badge + Change Button
    head_col1, head_col2 = st.columns([8, 2], vertical_alignment="center")
    
    with head_col1:
        st.markdown(
            f'''
            <div style="display: flex; align-items: center; gap: 15px;">
                <h1 style="margin: 0; padding: 0; font-size: 32px;">{st.session_state.selected_model} Workspace</h1>
                <div style="background-color: {status_bg}; color: {status_color}; height: 40px; padding: 0 16px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: bold; border: 1px solid {status_border}; font-size: 15px; box-sizing: border-box;">
                    {status_text}
                </div>
            </div>
            ''', 
            unsafe_allow_html=True
        )

    with head_col2:
        if st.button("🔄 Change Model", use_container_width=True):
            st.session_state.selected_model = None
            st.rerun()
        
    st.divider()
    
    # Render existing conversation thread
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # Chat Input Box
    if prompt := st.chat_input(f"Ask {st.session_state.selected_model} about your uploaded documents..."):
        with st.chat_message("user"):
            st.write(prompt)
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        with st.chat_message("assistant"):
            reply = f"🔒 [{st.session_state.selected_model}] Backend processing inactive. Uploaded documents in vault: {len(st.session_state.documents)}"
            st.write(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})