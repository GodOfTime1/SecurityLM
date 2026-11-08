"""Frontend display constants. Runtime identity and token limits remain backend-owned."""
TEAM_MODEL = "Llama4"
MAX_UPLOAD_MB = 10
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
FILE_TYPES = ("pdf", "txt", "md")
METADATA_TTL_SECONDS = 30
PREVIEW_BATCH_SIZE = 6
# IDs stay stable for the existing API; labels describe the answer styles.
PRESETS = (
    {"id": "snake2", "name": "Quick", "icon": "⚡", "reply_tokens": 128, "tagline": "Quick", "description": "Brief answers. Get to the point."},
    {"id": "llama4", "name": "Balanced", "icon": "◈", "reply_tokens": 512, "tagline": "Balanced", "description": "Clear answers with useful detail."},
    {"id": "megladon8", "name": "In-depth", "icon": "◎", "reply_tokens": 1024, "tagline": "In-depth", "description": "More room for thorough explanations."},
)
# Fallback for the supplied backend until it reports its active token budgets.
LEGACY_REPLY_TOKENS = {'snake2':128,'llama4':256,'megladon8':512}
PRESET_IDS = frozenset(p["id"] for p in PRESETS)
PRESET_NAMES = {p["id"]: p["name"] for p in PRESETS}

PRESET_BY_ID = {p["id"]: p for p in PRESETS}


def active_reply_tokens(preset,reported=None):
    value=(reported or {}).get('reply_tokens')
    return value if isinstance(value,int) and not isinstance(value,bool) and value>0 else LEGACY_REPLY_TOKENS[preset['id']]
