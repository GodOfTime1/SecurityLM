"""Bounded lexical retrieval: access filtering precedes candidate ranking."""
import re
from .auth import User
from .db import connection

STOPWORDS = set('a an and are as at be by can do does for from how i in is it me my of on or the to what when where which who why with you your please tell about could would should need know give explain find document documents company this that these those doc docs'.split())
# Small, inspectable vocabulary expansion; this is not embedding/semantic search.
CONCEPTS = (
    {'expense', 'expenses', 'reimbursement', 'reimburse', 'refund'},
    {'lost', 'missing', 'stolen'},
    {'laptop', 'notebook'},
    {'remote', 'remotely', 'home'},
    {'support', 'helpdesk', 'help'},
    {'hours', 'schedule', 'opening'},
    {'approve', 'approval', 'authorization'},
)

SQL = '''SELECT c.id AS chunk_id, d.id AS document_id, d.filename, c.page_number, c.text,
                bm25(chunk_search) AS lexical_rank
    FROM chunk_search JOIN chunks c ON c.id=chunk_search.chunk_id
    JOIN documents d ON d.id=c.document_id
    WHERE chunk_search MATCH ?
      AND (d.visibility='employee' OR ?='admin')
      AND (? IS NULL OR d.id IN (SELECT document_id FROM assistant_documents WHERE assistant_id=?))
    ORDER BY lexical_rank, c.id LIMIT ?'''


def query_groups(query):
    query=re.sub(r'\bvirtual\s+private\s+network\b','vpn',query,flags=re.I)
    tokens = list(dict.fromkeys(re.findall(r'\w+', query.lower(), flags=re.UNICODE)))[:30]
    meaningful = [t for t in tokens if t not in STOPWORDS]
    groups = []
    for token in meaningful[:24]:
        variants = {token}
        if len(token)>4 and token.endswith('ies'):
            variants.add(token[:-3]+'y')
        elif len(token)>4 and token.endswith('s') and not token.endswith('ss'):
            variants.add(token[:-1])
        if len(token)>6 and token.endswith('ing'):
            variants.update((token[:-3], token[:-3]+'e'))
        for concept in CONCEPTS:
            if variants & concept:
                variants.update(concept)
        groups.append((token,*sorted(variants-{token})))
    return groups


def relevance(text, groups):
    words = re.findall(r'\w+', text.lower())
    found = []
    for variants in groups:
        found.append([i for i,w in enumerate(words) if any(w==v or (len(v)>=4 and w.startswith(v)) for v in variants)])
    generic={'policy','policies','instruction','instructions','detail','details','rule','rules','information','process'}
    weights=[0.4 if set(variants).issubset(generic) else 1.0 for variants in groups]
    hits = sum(weight for weight,positions in zip(weights,found) if positions)
    if not hits:
        return 0
    numbers = sum(bool(positions) for variants,positions in zip(groups,found) if any(v.isdigit() for v in variants))
    # Coverage matters more than a frequent incidental term; numbers preserve error-code specificity.
    exact=sum(weight for variants,weight in zip(groups,weights) if variants[0] in words)
    return hits * 10 + exact * 5 + numbers * 5 + sum(min(len(p),3) for p in found) / 10


def contextual_query(question, history):
    # A narrow follow-up heuristic, using only this owner's recent user question.
    if re.match(r'^\s*(?:and\b|what about\b|how about\b|also\b|what if\b|does (?:it|that)\b)', question, re.I):
        previous = next((m['content'] for m in reversed(history) if m['role']=='user'), '')
        return question + ' ' + previous[:500]
    return question


def best_excerpt(text, query, limit=650):
    """Select a verbatim window near the most relevant paragraph, rather than the first 400 characters."""
    groups = query_groups(query)
    paragraphs = list(re.finditer(r'\S[^\n]*(?:\n(?!\s*\n)[^\n]+)*', text))
    if not paragraphs:
        return text[:limit]
    best = max(paragraphs, key=lambda p: relevance(p.group(), groups))
    start = best.start()
    # Include the heading immediately above a matched body, if it fits.
    before = text[:start].rstrip()
    heading_start = before.rfind('\n') + 1
    if before and len(before)-heading_start < 90:
        start = heading_start
    end = min(len(text), start + limit)
    return text[start:end].strip()


def search_documents(user: User, query: str, limit: int = 5, assistant_id: str | None = None) -> list[dict]:
    limit = max(1,min(limit,10))
    overview=bool(re.search(r'\b(summari[sz]e|overview)\b|\bmain points\b',query,re.I))
    summary_noise=STOPWORDS | set('all main points key summary summarize summarise overview uploaded available guide guides'.split())
    topic=[t for t in re.findall(r'\w+',query.lower()) if t not in summary_noise]
    if overview and not topic:
        # Overview requests need representative context, not literal hits on "summarize".
        with connection() as db:
            rows=db.execute('''SELECT * FROM (
                SELECT c.id AS chunk_id,d.id AS document_id,d.filename,c.page_number,c.text,
                  ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY c.page_number,c.rowid) AS position
                FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE (d.visibility='employee' OR ?='admin')
                  AND (? IS NULL OR d.id IN (SELECT document_id FROM assistant_documents WHERE assistant_id=?)))
                ORDER BY position,document_id LIMIT ?''',(user.role,assistant_id,assistant_id,limit)).fetchall()
        return [{k:row[k] for k in ('chunk_id','document_id','filename','page_number','text')} for row in rows]
    groups = query_groups(query)
    if not groups:
        return []
    # Every term is tokenized and quoted. Caller text never becomes FTS syntax.
    match = ' OR '.join('("' + v + '"' + ('*' if len(v)>=4 else '') + ')' for group in groups for v in group)
    with connection() as db:
        rows = db.execute(SQL,(match,user.role,assistant_id,assistant_id,min(120,limit*16))).fetchall()
    ranked = sorted(rows,key=lambda r:(-relevance(r['text'],groups),r['lexical_rank'],r['chunk_id']))
    return [{k:row[k] for k in ('chunk_id','document_id','filename','page_number','text')} for row in ranked[:limit]]
