"""Per-session cache for setup metadata only. Never cache identity or document content."""
from copy import deepcopy
import hashlib
import json
from time import monotonic
import streamlit as st
from api_client import backend_url
from constants import METADATA_TTL_SECONDS

ALLOWED = frozenset({"/runtime/status", "/model-selector/status", "/model-presets"})


def invalidate_metadata():
    st.session_state.pop("_setup_metadata", None)


def metadata(api, path, *, params=None):
    if path not in ALLOWED:
        raise ValueError("Only setup metadata may be cached")
    scope = hashlib.sha256((backend_url() + "\0" + api.key).encode()).hexdigest()
    bucket = st.session_state.get("_setup_metadata", {})
    if bucket.get("scope") != scope:
        bucket = {"scope": scope, "entries": {}}
    key = json.dumps([path, params or {}], sort_keys=True)
    now = monotonic()
    entry = bucket["entries"].get(key)
    if entry is not None and now - entry["time"] < METADATA_TTL_SECONDS:
        return deepcopy(entry["value"])
    value = api.request("GET", path, params=params or {})
    bucket["entries"][key] = {"time": monotonic(), "value": deepcopy(value)}
    st.session_state["_setup_metadata"] = bucket
    return value
