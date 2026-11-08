import streamlit as st
import time

# 1. Page Configuration
st.set_page_config(page_title="Help & FAQ - SecurityLM", layout="wide")

# 2. Custom CSS (Consistent with home.py and Document_Upload.py)
st.markdown("""
    <style>
    /* Hide default Streamlit sidebar navigation */
    [data-testid="stSidebarNav"] {display: none;}
    
    /* 20px font for sidebar navigation links */
    [data-testid="stPageLink-NavLink"] p {
        font-size: 20px !important;
    }
    
    /* Quick question chips styling */
    div[data-testid="stButton"] > button[key^="faq_"] {
        border-radius: 20px !important;
        font-size: 14px !important;
        border: 1px solid #3b4252 !important;
        background-color: #1e2638 !important;
        color: #e5e9f0 !important;
    }
    div[data-testid="stButton"] > button[key^="faq_"]:hover {
        border-color: #88c0d0 !important;
        color: #88c0d0 !important;
    }
    </style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# AI BACKEND PLACEHOLDER SETUP
# Set `ai_connected = True` when you connect your local inference engine.
# -----------------------------------------------------------------------------
ai_connected = False 

def get_ai_help_response(user_query: str) -> str:
    """
    AI Response Generator & Backend Hook.
    - If `ai_connected == True`: Replace the placeholder below with your local LLM call.
    - If `ai_connected == False`: Uses built-in FAQ retrieval rules.
    """
    if ai_connected:
        # =====================================================================
        # TODO: PLACEHOLDER FOR LIVE LLM / SLM BACKEND CALL
        # Example for Ollama:
        # response = ollama.chat(model='llama3', messages=[{'role': 'user', 'content': user_query}])
        # return response['message']['content']
        # =====================================================================
        return f"🤖 [Live SLM Backend]: Responding to query: '{user_query}'"

    # --- FALLBACK INSTANT KNOWLEDGE BASE (OFFLINE / SIMULATED MODE) ---
    query_lower = user_query.lower()
    
    if any(k in query_lower for k in ["what is an slm", "what is a small language model", "slm vs llm"]):
        return (
            "### 🤖 What is a Small Language Model (SLM)?\n\n"
            "A **Small Language Model (SLM)** is a lightweight AI model designed to run directly on consumer-grade hardware (laptops and desktops) rather than cloud servers.\n\n"
            "* **Privacy:** All prompt data stays local to your device.\n"
            "* **Efficiency:** Parameter sizes (typically 1B–8B) run smoothly on standard RAM/GPUs.\n"
            "* **Zero Latency:** No internet connection or API keys required."
        )
    elif any(k in query_lower for k in ["secure", "privacy", "data leave", "cloud"]):
        return (
            "### 🛡️ Security & Data Privacy Architecture\n\n"
            "SecurityLM is built **air-gapped and local-first**:\n\n"
            "1. **Zero External API Calls:** Document analysis stays inside system RAM.\n"
            "2. **No Unsanctioned Persistence:** Files uploaded in the Document Vault are held locally in active memory.\n"
            "3. **Audit Readiness:** Safe for sensitive financial, medical, and legal records."
        )
    elif any(k in query_lower for k in ["hardware", "requirements", "ram", "gpu", "specs"]):
        return (
            "### ⚙️ Hardware Specifications\n\n"
            "* 🐍 **Snake_2 (2 GB footprint):** 8 GB RAM, standard dual-core CPU.\n"
            "* 🦙 **Llama_4 (4 GB footprint):** 16 GB RAM or Apple M-Series CPU.\n"
            "* 🦈 **Megladon_8 (8 GB footprint):** 16 GB+ RAM + dedicated NVIDIA GPU or Apple Silicon."
        )
    elif any(k in query_lower for k in ["automatic", "setup", "on my end", "install"]):
        return (
            "### 🚀 Automated Local Setup\n\n"
            "You can run the backend locally using **Ollama** or `llama-cpp-python`:\n"
            "1. Install Ollama from [ollama.com](https://ollama.com).\n"
            "2. Run `ollama run llama3` in your terminal.\n"
            "3. Change `ai_connected = True` in code to automatically route chat prompts to your local engine!"
        )
    else:
        return (
            f"**SecurityLM Assistant:** I received your query regarding *'{user_query}'*.\n\n"
            "You can choose from **Snake_2**, **Llama_4**, or **Megladon_8** on the main dashboard depending on your machine's hardware specs. "
            "All document analysis is handled locally on your device without transmitting data outside your network."
        )

# Session state initialization for support chat log
if "help_messages" not in st.session_state:
    st.session_state.help_messages = [
        {
            "role": "assistant",
            "content": "👋 Welcome to **SecurityLM Support**! Ask me anything about Small Language Models, security architecture, system requirements, or setting up your local backend."
        }
    ]

# 3. Custom Sidebar
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

# 4. Header Section
head_col1, head_col2 = st.columns([8, 2], vertical_alignment="center")
with head_col1:
    st.markdown('<h1 style="margin: 0; padding: 0; font-size: 32px;">❓ Help & AI Support</h1>', unsafe_allow_html=True)
with head_col2:
    status_text = "🟢 AI Engine Active" if ai_connected else "🔴 Simulation Mode"
    status_bg = "#1b5e20" if ai_connected else "#5c1d1d"
    st.markdown(
        f'<div style="background-color: {status_bg}; color: white; padding: 8px 12px; border-radius: 6px; text-align: center; font-weight: bold; font-size: 13px;">{status_text}</div>',
        unsafe_allow_html=True
    )

st.write("Get instant answers about SecurityLM architecture, privacy assurances, and hardware setups.")
st.divider()

# Quick Prompt Suggestion Chips
st.markdown("**Common Questions:**")
q_col1, q_col2, q_col3, q_col4 = st.columns(4)

selected_prompt = None
with q_col1:
    if st.button("💡 What is an SLM?", key="faq_slm", use_container_width=True):
        selected_prompt = "What is a Small Language Model (SLM)?"
with q_col2:
    if st.button("🔒 How secure is local data?", key="faq_security", use_container_width=True):
        selected_prompt = "How secure is SecurityLM and does data leave my computer?"
with q_col3:
    if st.button("⚡ Hardware Requirements", key="faq_hardware", use_container_width=True):
        selected_prompt = "What hardware specs do I need to run Snake_2, Llama_4, or Megladon_8?"
with q_col4:
    if st.button("🤖 Automatic Setup Guide", key="faq_auto", use_container_width=True):
        selected_prompt = "Can I install and run the AI backend automatically on my end?"

# 5. Render Interactive Chat History
for msg in st.session_state.help_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ALWAYS render st.chat_input so the chat input bar remains visible on screen at all times
chat_prompt = st.chat_input("Ask a question about SecurityLM, local models, privacy, or hardware...")

# Determine active user input (prioritizes button chip click if clicked this rerun)
user_input = selected_prompt if selected_prompt else chat_prompt

if user_input:
    # Render User Query
    st.session_state.help_messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)
        
    # Generate & Stream AI Response
    response_text = get_ai_help_response(user_input)
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        
        # Smooth streaming animation effect
        for chunk in response_text.split(" "):
            full_response += chunk + " "
            time.sleep(0.015)
            message_placeholder.markdown(full_response + "▌")
        message_placeholder.markdown(full_response)
        
    st.session_state.help_messages.append({"role": "assistant", "content": full_response})