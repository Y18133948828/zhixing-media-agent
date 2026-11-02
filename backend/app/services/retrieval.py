import re
from collections import Counter


def tokens(text):
    words = re.findall(r"[a-z0-9_]+", text.lower())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(phrase[i : i + 2] for i in range(len(phrase) - 1))
    return Counter(words)


def retrieve(documents, query, limit=5):
    """Local lexical retrieval for small course libraries; no fabricated vector similarity."""
    query_tokens = tokens(query)
    ranked = []
    for document in documents:
        bag = tokens(document.title + " " + document.body)
        score = sum(min(count, bag.get(term, 0)) for term, count in query_tokens.items())
        if score:
            ranked.append((score, document))
    return [document for _, document in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]
