"""POST /conversations/{id}/messages: retrieve permitted passages, call the model, store the turn."""
import json
import time
from dataclasses import replace
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .actions import ToolRejected, create_draft
from .auth import User, current_user
from .conversations import owned_conversation
from .db import connection
from .model_client import (ModelRequest, ModelUnavailable, Passage, ToolSpec,
                           CitationError, extract_citations, get_model_client,
                           validate_response, validate_citations)
from .citation_utils import ABSTENTION, strip_history_citations
from .retrieval import search_documents, contextual_query
from .tools import TOOLS
from .inference import generate_reply
from .turns import reserve_turn, check_reservation, complete_turn, release_turn, LEASE_SECONDS
from .provenance import MAX_SOURCES, message_accessible, message_sources, sources_accessible

router = APIRouter(prefix="/conversations", tags=["chat"])
HISTORY_LIMIT = 10
PASSAGE_LIMIT = 5
MAX_PROPOSALS = 3


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    request_id: UUID = Field(default_factory=uuid4)
    expected_revision: int | None = Field(default=None, ge=0)


def _json_small(value, limit: int = 2000) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else json.dumps({"truncated": True})


@router.post("/{conversation_id}/messages", status_code=201)
def send_message(conversation_id: str, body: MessageIn, user: User = Depends(current_user)):
    reservation, replay = reserve_turn(conversation_id, str(body.request_id), body.content, body.expected_revision, user)
    if replay is not None:
        return replay
    try:
        return _generate_turn(conversation_id, body, user, reservation)
    finally:
        release_turn(reservation)


def _generate_turn(conversation_id, body, user, reservation):
    with connection() as db:
        conv = owned_conversation(db, conversation_id, user)
        assistant = db.execute("SELECT * FROM assistants WHERE id=?", (conv["assistant_id"],)).fetchone()
        tool_names = [r["tool"] for r in db.execute(
            "SELECT tool FROM assistant_tools WHERE assistant_id=? ORDER BY tool", (assistant["id"],)) if r["tool"] in TOOLS]
        rows = list(db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?",
            (conversation_id, HISTORY_LIMIT)))[::-1]
        history: list[dict[str, str]] = []
        inherited_sources: set[str] = set()
        remaining = 24000
        # Keep recent complete turns within a character budget.
        for row in reversed(rows):
            if not message_accessible(db, row, user, assistant["id"], require_current=True):
                continue
            row_sources: list[str] | None = message_sources(row) if row["role"] == "assistant" else []
            if row_sources is None:
                # Unknown/corrupt provenance stays excluded, rather than being
                # silently treated as an answer with no source dependencies.
                continue
            if len(row["content"]) > remaining:
                break
            remaining -= len(row["content"])
            history_content = (strip_history_citations(row['content'])
                               if row['role'] == 'assistant' else row['content'])
            history.insert(0, {"role": row["role"], "content": history_content})
            if row["role"] == "assistant":
                inherited_sources.update(row_sources)

    # Permissions are applied inside search_documents, before anything reaches the model.
    chunks = search_documents(user, contextual_query(body.content, history), PASSAGE_LIMIT, assistant_id=assistant["id"])
    sources = sorted(inherited_sources | {c["chunk_id"] for c in chunks})
    if len(sources) > MAX_SOURCES:
        raise HTTPException(409, "Conversation source limit reached; start a new conversation")
    passages = [Passage(i, c["chunk_id"], c["filename"], c["page_number"], c["text"]) for i, c in enumerate(chunks, 1)]
    request = ModelRequest(
        model=assistant["model"], purpose=assistant["purpose"], system_prompt=assistant["system_prompt"],
        history=history, question=body.content, passages=passages,
        tools=[ToolSpec(n, TOOLS[n].description, TOOLS[n].args_model.model_json_schema()) for n in tool_names])
    try:
        client = get_model_client()
        reply = validate_response(generate_reply(client, request))
        try:
            reply = validate_citations(reply, passages)
        except CitationError:
            # One correction attempt, with the same permitted passages. Renew the
            # fenced lease so two slow inference calls cannot outlive it silently.
            with connection() as db:
                db.execute('BEGIN IMMEDIATE')
                check_reservation(db, reservation, user)
                if not sources_accessible(db, sources, user, assistant['id'], require_current=True):
                    raise HTTPException(409, 'Document access changed; retry your message')
                db.execute('UPDATE chat_requests SET lease_until=? WHERE conversation_id=? AND request_id=? AND token=?',
                           (time.time() + LEASE_SECONDS, conversation_id, str(body.request_id), reservation.token))
            correction = ("\nYour previous response had missing or invalid source citations. "
                          "Answer the original question again using only the current passages. "
                          "Cite supporting passages with their exact allowed [n] markers. "
                          f"If unsupported, respond exactly: {ABSTENTION}")
            corrected = validate_response(generate_reply(client, replace(
                request, system_prompt=request.system_prompt + correction)))
            try:
                reply = validate_citations(corrected, passages)
            except CitationError:
                raise ModelUnavailable('The model could not produce valid source citations after one correction. Try again.') from None
    except ModelUnavailable as exc:
        # No messages are stored on failure; the request ID remains bound to its payload.
        raise HTTPException(503, str(exc)) from None

    citations = extract_citations(reply.text, passages)
    user_msg_id, bot_msg_id = str(uuid4()), str(uuid4())
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        check_reservation(db, reservation, user)
        if not sources_accessible(db, sources, user, assistant["id"], require_current=True):
            raise HTTPException(409, "Document access changed; retry your message")
        db.execute("INSERT INTO messages(id,conversation_id,role,content) VALUES (?,?,'user',?)",
                   (user_msg_id, conversation_id, body.content))
        db.execute("INSERT INTO messages(id,conversation_id,role,content,citations,source_chunks) VALUES (?,?,'assistant',?,?,?)",
                   (bot_msg_id, conversation_id, reply.text, json.dumps(citations), json.dumps(sources)))
        db.execute("INSERT INTO tool_calls(id,message_id,tool,arguments,status,result) VALUES (?,?,?,?,'ok',?)",
                   (str(uuid4()), bot_msg_id, "search_documents",
                    _json_small({"query": body.content[:500], "assistant_id": assistant["id"]}),
                    _json_small({"chunk_ids": [c["chunk_id"] for c in chunks]})))
        for proposal in reply.actions[:MAX_PROPOSALS]:
            tool = proposal.get("tool") if isinstance(proposal, dict) else None
            args = proposal.get("arguments") if isinstance(proposal, dict) else None
            try:
                action_id = create_draft(db, conversation_id=conversation_id, message_id=bot_msg_id,
                                         assistant_id=assistant["id"], tool=tool, arguments=args)
                outcome = ("ok", {"action_id": action_id, "status": "draft"})
            except ToolRejected as exc:
                outcome = ("rejected", {"reason": str(exc)})
            db.execute("INSERT INTO tool_calls(id,message_id,tool,arguments,status,result) VALUES (?,?,?,?,?,?)",
                       (str(uuid4()), bot_msg_id, tool if isinstance(tool, str) and len(tool) <= 50 else "invalid",
                        _json_small(args if isinstance(args, dict) else {}), outcome[0], _json_small(outcome[1])))
        if not conv["title"]:
            db.execute("UPDATE conversations SET title=? WHERE id=?", (body.content.strip()[:60], conversation_id))
        return complete_turn(db, reservation, user_msg_id, bot_msg_id, user)
