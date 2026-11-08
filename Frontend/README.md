# SecurityLM — Llama4 answer styles and conversation memory

## Run

Use Python 3.11+. Install the complete frontend, including `assets/`, `.streamlit/`, `conversation_memory.py`, and the other supporting modules. Keep your backend running separately.

```sh
python -m pip install -r requirements-frontend.txt
python -m streamlit run home.py
```

Run from the frontend directory so the dark theme loads. Restart an existing Streamlit process after replacing files. The default backend origin is `http://127.0.0.1:8000`; configure `BACKEND_URL` in the frontend environment for another origin. Connect with your individual access key.

## Answer styles

All three styles are presented under your model name, **Llama4**:

| Style | Requested maximum reply tokens | Framing |
| --- | ---: | --- |
| Quick | 128 | Brief answers. Get to the point. |
| Balanced | 512 | Clear answers with useful detail. |
| In-depth | 1,024 | More room for thorough explanations. |

The API IDs remain `snake2`, `llama4`, and `megladon8` for compatibility. These are token budgets, not bits or model parameter sizes. The 4× and 8× comparisons refer only to requested reply space relative to Quick, not speed, accuracy, or a measured increase in depth.

**Runtime update is staged, not applied.** The supplied backend still enforces 128 / 256 / 512. The frontend identifies requested limits and active limits separately until the runtime reports the new values. `proposed_runtime_budgets.patch` changes two backend files: the generation-budget mapping and the metadata returned to the frontend. It is included for review because the earlier instruction prohibited backend edits. No original backend files were changed.

## Reliability and memory

- For an existing assistant, open **Assistant setup → Task & instructions** and save its behavior once to apply the updated source-review and continuity instructions. Existing custom guidance is preserved in the editor. New assistants receive the updated instructions automatically.
- The review instructions prioritize source support, citations, numbers/units, contradictions, uncertainty, and the user's latest correction. They explicitly prohibit treating an earlier assistant answer as evidence.
- Use **Conversation memory** to pin up to 800 characters of important details. Notes are included with every subsequent message in that conversation. This works through the existing message API, even after the original note falls outside the backend's recent-history window.
- Saving notes updates the current session immediately. They become persistent conversation history when you send the next message. A new browser session restores them from that history. Clear the notes and send a new message to persist the cleared state. Earlier historical messages are retained; clearing pins is not deletion of those messages.
- Use **Check latest answer** to request a visible follow-up review using currently available sources. It does not execute a hidden chain of model calls or claim independent verification.
- Citation numbers with no available matching source are flagged. That checks reference availability, not whether a passage logically proves a claim.

Pinned notes use part of the existing 4,000-character message limit; the question box adjusts accordingly. Notes are user context, not document evidence or higher-priority system instructions. No automated memory summary is invented, and notes are not shared across conversations or sent to model-advisor chat.

## Tests

Standalone frontend checks:

```sh
python -m unittest discover -s tests -v
```

Full backend integration checks require backend dependencies and the directory containing `app/` on the import path:

```sh
PYTHONPATH=".:../Backend" python -m unittest discover -s tests -v
```

PowerShell:

```powershell
$env:PYTHONPATH = ".;../Backend"
python -m unittest discover -s tests -v
```

Adjust the backend path to your repository layout. Tests use temporary SQLite databases, development keys, and echo adapters. They do not load or benchmark your actual Llama4 model.
