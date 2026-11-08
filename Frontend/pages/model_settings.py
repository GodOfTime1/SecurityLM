"""Admin-only model configuration, separate from the chat workspace."""
import streamlit as st
from api_client import APIError
from ui_common import connect, get_assistants, pick_assistant, setup, show_notice, runtime_banner, preset_cards

setup("Model settings")
api, user = connect()
if user["role"] != "admin":
    st.info("Model settings are managed by an administrator.")
    st.stop()
show_notice()
st.title("Model settings")
st.write("Choose a supported model for an assistant profile.")

try:
    assistant = pick_assistant(get_assistants(api), key="settings_profile")
    if assistant is None:
        st.page_link("home.py", label="Create an assistant profile", icon="🏠")
        st.stop()
    runtime_banner(api,assistant['id'])
    if st.button("Check runtime connection"):
        with st.spinner("Checking model availability…"):
            check=api.request('POST','/runtime/check',json={'assistant_id':assistant['id']})
        if check.get('available'):
            st.success(check['message'])
        else:
            st.warning(check['message'])
    preset_cards(api,user,assistant)
    with st.container(border=True):
        st.caption("CURRENT CHOICE")
        st.text(assistant["model"] or "No model selected")
        st.caption("Runtime connection has not been verified. Saving a choice does not install or start a model.")
    models = [m for m in api.request("GET", "/models")["models"] if m["enabled"] and m['id'] not in {'snake2','llama4','megladon8'}]
    if not models:
        st.caption("Choose a preset above to initialize the three catalog entries. Your model teammate can map each preset to a real runtime.")
    with st.expander("Connect a real language model"):
        st.write("On the backend, choose the Ollama adapter and map each preset to an installed runtime tag. The advisor has its own independent settings.")
        st.code('MODEL_BACKEND=ollama\nSNAKE2_MODEL=<installed tag>\nLLAMA4_MODEL=<installed tag>\nMEGLADON8_MODEL=<installed tag>',language='text')
        st.caption("The names above are server configuration keys. A saved preset is not proof that its underlying model fits the target budget or is available.")
    for start in range(0, len(models), 3):
        for col, model in zip(st.columns(3), models[start:start + 3]):
            with col, st.container(border=True):
                st.subheader(model["name"])
                st.text(model["description"])
                estimate = model["estimated_memory_gb"]
                st.caption(f"Estimated memory: {estimate:g} GB" if estimate is not None else "Memory requirement: not verified")
                st.caption("Deployment: " + model["deployment"])
                st.caption("Marked ready by admin" if model["ready"] else "Setup required")
                if st.button("Save model choice", key="model_" + model["id"], use_container_width=True):
                    api.request("POST", "/model-selector/apply", json={
                        "assistant_id": assistant["id"], "model_id": model["id"],
                        "catalog_version": model["version"], "expected_current_model": assistant["model"]})
                    st.session_state["notice"] = "Model choice saved. Start a new conversation to use clean context. Runtime connection still needs verification."
                    st.rerun()
    st.page_link("home.py", label="Return to workspace", icon="💬")
except APIError as exc:
    st.error(str(exc))
