# Llama4 framing, reliability, and memory update

## Implemented

The dark theme, custom logo, unified navigation, and equal-height cards remain. Model presentation now uses **Llama4** as the base name and **Quick / Balanced / In-depth** as answer styles. Obsolete model and runtime branding has been removed from frontend product copy.

Requested token budgets are 128, 512, and 1,024. Cards compare **reply space**, not unmeasured speed, accuracy, or intelligence. The existing backend IDs remain unchanged for compatibility. The frontend distinguishes requested and active budgets instead of implying that changing a label changes generation settings.

### Source review

Task instructions now tell the model to check factual claims against their cited passages before finalizing, recheck quantities/units/dates/conditions, identify contradictions, and remove or qualify unsupported claims. Earlier assistant output is explicitly not evidence. The latest user correction takes precedence over older user context.

These are behavioral instructions, not a verified factuality guarantee or an independent model judge. Existing assistants pick them up when an administrator saves behavior; new assistants receive them on creation. Actual quality still needs evaluation with your running model and representative questions.

**Check latest answer** creates a visible user follow-up asking for a source-based correction or a statement of unsupported claims. It uses the normal pending-message, revision, and idempotent-retry path. No duplicate automatic generation or hidden approval action is introduced. Unmatched citation numbers are flagged without falsely certifying semantic support.

### Conversation memory

Pinned notes provide explicit working context instead of relying on “do not forget” alone. Up to 800 characters of user-specified details accompany every subsequent question. They travel through the existing backend's normal message content and are saved in the conversation history. Notes are reconstructed from fresh user messages on a new session; assistant claims are never promoted into pinned memory.

This carries important details beyond the existing recent-history window. It does not claim unlimited recall of every earlier chat message. Notes remain scoped to one conversation. An explicit empty-note marker prevents older pins from being restored after clearing them. Saving a draft note takes effect on the next message; persistence across sign-out starts once that message is sent. Historical content is not erased when pins are cleared.

The combined question and notes stay within the backend's message limit. User notes are labeled as context rather than authoritative document facts. Using the existing content field can affect lexical retrieval terms; a future structured backend context field would separate these concerns more cleanly. The current implementation does not change backend authorization, provenance, source filtering, or role checks.

## Runtime change prepared for review

The earlier instruction was to leave backend code untouched. Therefore **the original backend remains unchanged**.

`proposed_runtime_budgets.patch` contains the minimal runtime change:

1. `app/model_client.py`: change the preset output caps to 128 / 512 / 1,024.
2. `app/presets.py`: report each actual output cap through the existing preset metadata endpoint.

The patch has been checked and applied only to a disposable test copy. Once approved and applied to the real backend, the frontend recognizes the reported values and removes the pending-budget labels automatically. This patch does not download, replace, or identify a model artifact; Llama4 is the model name supplied by you.

## Verification

**86 tests passed with the original backend and 86 passed with the staged patch applied to a disposable copy; zero failures or skips.** This comprises 50 frontend/helper checks and 36 backend integration cases. The patch passed `git apply --check` against an unchanged copy of the supplied source. Python compilation passed.

The source-review tests establish that instructions and context reach the model request. Echo adapters do not demonstrate that a real model reliably follows those instructions. No factuality benchmark, speed benchmark, or accuracy percentage is claimed.

Original backend attachment hashes remain unchanged. The staged patch is tested separately. Browser visual verification remains unavailable in this environment; no screenshot or visual sign-off is claimed.
