"""This is the model teammate's integration point. No LLM required here."""
import re
from .auth import User
from .db import connection

STOPWORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
             "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "to", "what",
             "when", "where", "which", "who", "why", "with", "you", "your"}

SQL = """
    SELECT c.id AS chunk_id, d.id AS document_id, d.filename, c.page_number, c.text
    FROM chunk_search
    JOIN chunks c ON c.id = chunk_search.chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE chunk_search MATCH ?
      AND (d.visibility = 'employee' OR ? = 'admin')
      AND (? IS NULL OR d.id IN (SELECT document_id FROM assistant_documents WHERE assistant_id = ?))
    ORDER BY bm25(chunk_search)
    LIMIT ?
"""


def search_documents(user: User, query: str, limit: int = 5, assistant_id: str | None = None) -> list[dict]:
    """Permission-filtered keyword search. If assistant_id is given, only that assistant's collection."""
    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:30]
    meaningful = [t for t in terms if t.lower() not in STOPWORDS] or terms
    if not meaningful:
        return []
    limit = max(1, min(limit, 10))
    quoted = ['"' + t + '"' for t in meaningful]  # quoting stops input becoming FTS operators
    with connection() as db:
        # Try all terms first (AND); if nothing matches, fall back to any term (OR).
        for match in (" ".join(quoted), " OR ".join(quoted)):
            rows = db.execute(SQL, (match, user.role, assistant_id, assistant_id, limit)).fetchall()
            if rows:
                break
    return [dict(row) for row in rows]
