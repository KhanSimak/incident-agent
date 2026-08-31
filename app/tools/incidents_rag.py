"""
tools/incidents_rag.py — "have we seen this before" search.

In your real Codebase Q&A stack this would be Qdrant + sentence
embeddings, same as retriever.py. This standalone version deliberately
uses simple keyword overlap instead — not because embeddings are wrong
for this, but so this whole project runs and is readable without first
standing up a vector DB just to study the code. If you wire this into
your real stack later, swap what's INSIDE search_similar_incidents for
an embed + Qdrant query — the calling code in agents/investigation.py
doesn't need to change at all, same principle as tools/metrics.py's
note about swapping in real Prometheus later.

PAST_POSTMORTEMS below are DIFFERENT incidents from the ones in
fixtures.py — using the same scenarios you're testing against as their
own "similar past incident" would leak the answer straight into the
search results, which would make the eval meaningless.
"""
PAST_POSTMORTEMS = [
    {
        "id": "PM-2024-11",
        "summary": "auth-service pool exhaustion: a retry loop on failed logins wasn't releasing DB connections on the error path, pool filled over ~15 minutes, requests started timing out.",
    },
    {
        "id": "PM-2025-03",
        "summary": "notification-service deploy broke a function signature — a caller wasn't updated to pass a new required argument, every call raised TypeError immediately after deploy.",
    },
    {
        "id": "PM-2025-07",
        "summary": "search-service outage traced back to its Elasticsearch cluster becoming unreachable — the reported symptom was on search-service, but the actual failure was two hops downstream.",
    },
]


def search_similar_incidents(query: str, top_k: int = 2) -> dict:
    """Jaccard-style keyword overlap between the query and each postmortem summary."""
    query_words = set(query.lower().split())

    scored = []
    for pm in PAST_POSTMORTEMS:
        pm_words = set(pm["summary"].lower().split())
        overlap = len(query_words & pm_words)
        union = len(query_words | pm_words)
        score = overlap / union if union else 0.0
        scored.append({**pm, "similarity": round(score, 3)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return {"query": query, "results": scored[:top_k]}
