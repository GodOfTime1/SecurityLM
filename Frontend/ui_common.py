"""Navigation, per-session authentication, and access-aware rendering."""
import streamlit as st
from pathlib import Path
from api_client import APIError, BackendAPI


def existing_page(*paths):
    root=Path(__file__).resolve().parent
    return next((path for path in paths if (root/path).is_file()),None)


def setup(title):
    st.set_page_config(page_title=f"{title} · SecurityLM", layout="wide")
    st.markdown("""<style>
    [data-testid="stSidebarNav"] {display:none;}
    [data-testid="stPageLink-NavLink"] p {font-size:16px;}
    .block-container {max-width:1180px;padding-top:2rem;}
    [data-testid="stVerticalBlockBorderWrapper"] {border-radius:16px;}
    [data-testid="stSidebar"] {border-right:1px solid #293245;}
    h1 {letter-spacing:-0.035em;}
    [data-testid="stButton"] button {border-radius:10px;}
    [data-testid="stMetric"] {padding:10px 4px;}
    [data-testid="stMetricValue"] {font-size:1.35rem;}
    [data-testid="stChatMessage"] {border-radius:14px;}
    </style>""", unsafe_allow_html=True)
    with st.sidebar:
        st.title("🛡️ SecurityLM")
        st.caption("Your documents. Your assistant.")
        st.page_link("home.py", label="Workspace", icon="🏠")
        documents=existing_page('pages/document_upload.py','pages/Document_Upload.py')
        if documents:
            st.page_link(documents, label="Documents", icon="📄")
        st.page_link("pages/help.py", label="Help & model advisor", icon="❓")
        if st.session_state.get("is_admin") and existing_page('pages/model_settings.py'):
            st.page_link("pages/model_settings.py", label="Model settings", icon="⚙️")
        st.divider()


def connect():
    if "api_key" not in st.session_state:
        st.title("Connect to your workspace")
        st.write("Enter your individual backend access key. It stays in this Streamlit session.")
        with st.form("connect"):
            key = st.text_input("Access key", type="password")
            submitted = st.form_submit_button("Connect", type="primary")
        if submitted and key.strip():
            try:
                BackendAPI(key.strip()).request("GET", "/me")
            except APIError as exc:
                st.error(str(exc))
            else:
                st.session_state.clear()
                st.session_state.api_key = key.strip()
                st.rerun()
        st.stop()
    api = BackendAPI(st.session_state.api_key)
    try:
        user = api.request("GET", "/me")
    except APIError as exc:
        st.error(str(exc))
        if st.button("Disconnect"):
            st.session_state.clear()
            st.rerun()
        st.stop()
    with st.sidebar:
        # UI visibility only; the backend enforces actual permissions.
        if st.session_state.get("is_admin") != (user["role"] == "admin"):
            st.session_state["is_admin"] = user["role"] == "admin"
            st.rerun()
        st.caption(f"Signed in: {user['id']} · {user['role']}")
        if st.button("Sign out"):
            st.session_state.clear()
            st.rerun()
    return api, user


def get_assistants(api):
    return api.all_items("/assistants", "assistants")


def pick_assistant(assistants, key="profile_picker"):
    if not assistants:
        st.info("An administrator needs to create an assistant first.")
        return None
    ids = [a["id"] for a in assistants]
    names = {a["id"]: a["name"] for a in assistants}
    # Stable selection survives multipage widget cleanup.
    active = st.session_state.get("active_assistant_id")
    if active not in ids:
        active = ids[0]
    if st.session_state.get(key) not in ids or st.session_state.get("picker_sync_" + key) != active:
        st.session_state[key] = active
    selected = st.selectbox("Assistant profile", ids, format_func=lambda i: names[i], key=key)
    st.session_state["active_assistant_id"] = selected
    st.session_state["picker_sync_" + key] = selected
    return next(a for a in assistants if a["id"] == selected)


def show_notice():
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))


def profile_form(api, preset=None):
    with st.form("create_assistant", clear_on_submit=True):
        name = st.text_input("Profile name", placeholder="For example, IT Helper", max_chars=100)
        purpose = st.text_area("What should it help with?", placeholder="Answer questions from our IT guides.", max_chars=2000)
        create = st.form_submit_button("Create assistant profile", type="primary")
    if create:
        if not name.strip():
            st.warning("Give your assistant profile a name.")
        else:
            if preset:
                api.request("POST", "/model-presets/initialize")
            assistant = api.request("POST", "/assistants", json={"name": name.strip(), "purpose": purpose,
                                    "model": 'preset:'+preset['id'] if preset else ''})
            st.session_state["active_assistant_id"] = assistant["id"]
            st.session_state["notice"] = "Assistant profile created. Next, add and link your documents."
            st.rerun()


def runtime_banner(api, assistant_id=None):
    params={"assistant_id":assistant_id} if assistant_id else {}
    try:
        status=api.request("GET", "/runtime/status", params=params)
    except APIError as exc:
        if exc.status==404:
            st.warning("This backend does not expose document-runtime status. Availability cannot be verified here.")
            return {"state":"unknown","demo":False}
        raise
    if status['state']=='demo':
        st.warning("DEMO MODE · Document retrieval works, but no language model is running. Replies are quoted excerpts.")
    elif status['state']=='configured':
        st.info("MODEL CONFIGURED · Runtime availability is checked when you send a message.")
    else:
        st.warning(status['message'])
    return status


def preset_cards(api, user, assistant=None):
    available=True
    try:
        presets=api.request("GET", "/model-presets")['presets']
    except APIError as exc:
        if exc.status!=404:
            raise
        available=False
        st.session_state.pop('draft_preset',None)
        presets=[{'id':key,'name':name,'target_memory_gb':budget,'icon':icon,'runtime_mapped':False}
            for key,name,budget,icon in [('snake2','Snake2',2,'🐍'),('llama4','Llama4',4,'🦙'),('megladon8','Megladon8',8,'🦈')]]
        st.info('This backend does not expose preset setup. Existing profiles and conversations can still be used; preset changes are unavailable.')
    for col,preset in zip(st.columns(3),presets):
        with col,st.container(border=True):
            st.caption(preset['icon'] + "  PRESET PROFILE")
            st.subheader(preset['name'])
            st.metric("Target budget",f"{preset['target_memory_gb']} GB")
            st.caption("Runtime mapping configured" if preset['runtime_mapped'] else "Real model mapping pending")
            selected=bool(assistant and assistant['model']=='preset:'+preset['id']) if assistant else st.session_state.get('draft_preset',{}).get('id')==preset['id']
            label="Selected" if selected else "Choose "+preset['name']
            if st.button(label,key="choose_"+preset['id'],disabled=selected or user['role']!='admin' or not available,
                         type="primary" if selected else "secondary",use_container_width=True):
                if assistant:
                    api.request("POST", "/model-presets/initialize")
                    catalog=api.request("GET", "/models")['models']
                    model=next(m for m in catalog if m['id']==preset['id'])
                    api.request("POST", "/model-selector/apply", json={"assistant_id":assistant['id'],
                        "model_id":model['id'],"catalog_version":model['version'],"expected_current_model":assistant['model']})
                    st.session_state['notice']=preset['name']+" profile selected. Start a new conversation after changing models."
                else:
                    st.session_state['draft_preset']=preset
                st.rerun()
    st.caption("2/4/8 GB are target budgets, not measured memory requirements. These are product preset names, not verified model families.")


def render_message(message):
    with st.chat_message(message["role"]):
        # Plain text prevents model-generated HTML and external Markdown images.
        st.text(message["content"])
        if message.get("redacted"):
            st.warning("Source access changed; this answer is withheld.")
        elif message.get("stale_sources"):
            st.warning("Historical answer: sources were replaced. Ask again for current information.")
        for citation in message.get("citations", []):
            if not citation.get("available"):
                st.caption(f"Source {citation['n']} is unavailable.")
                continue
            with st.expander(f"[{citation['n']}] {citation['filename']} · page {citation['page_number']}"):
                st.text(citation["text"])
        for action in message.get("actions", []):
            st.caption(f"Suggested action: {action['tool']} · {action['status']} (approval UI pending)")
