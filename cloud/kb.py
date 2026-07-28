#!/usr/bin/env python3
"""
kb.py -- knowledge base retrieval.

Picks the handful of articles most likely to answer a question and hands them to
the assistant, so it can say "here is how you connect to the VPN" instead of
shrugging at anything that is not an AD action.

WHY LEXICAL AND NOT EMBEDDINGS
------------------------------
The obvious build is vector search. It is the wrong first move for THIS product,
for a reason specific to it: there is no embedding provider that covers every
tenant.

  * A tenant on Cloud is talking to Anthropic, and Anthropic has no embeddings
    API. Their own recommendation is a third-party vendor. So a cloud tenant
    cannot embed with the provider they have already configured and paid for.
  * Adding a third-party embeddings vendor means another key, another bill and
    another egress path off the network, which is the exact thing a self-hosted
    buyer chose this product to avoid.
  * Embedding locally instead means sentence-transformers, which means torch:
    roughly two gigabytes of dependency on a domain controller. No.
  * A tenant on Local usually CAN embed (Ollama serves /api/embeddings), but
    building the feature so it only works for some tenants makes it undemoable
    and untrustworthy.

So retrieval is BM25 over the article text: no new dependencies, identical on
SQLite and Postgres, works for every tenant on day one, and is genuinely strong
for this corpus. A knowledge base is a few hundred short documents written by
admins who use their organisation's own vocabulary, and the questions arrive in
that same vocabulary. That is close to the best case for lexical search and the
worst case for the "semantic similarity beats keywords" argument.

The scoring is isolated behind search() so an embedding re-rank can be layered on
later for tenants whose provider supports it, without touching callers.

BEING WRONG IS WORSE THAN BEING SILENT
--------------------------------------
Injecting an irrelevant article makes the assistant confidently wrong, which is
worse than it admitting it does not know. Retrieval is therefore deliberately
conservative: a result must clear a score floor AND match a real share of the
query's meaningful words, or nothing is returned at all.
"""

import math
import re

# Words too common to carry meaning, plus helpdesk filler that appears in
# practically every question ("how do I...", "I need to...", "please can you").
STOPWORDS = {
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "been", "but",
    "by", "can", "cannot", "cant", "could", "did", "do", "does", "doesnt", "dont",
    "for", "from", "get", "getting", "got", "had", "has", "have", "he", "her",
    "here", "him", "his", "how", "i", "if", "im", "in", "into", "is", "it", "its",
    "ive", "just", "me", "my", "need", "needs", "no", "not", "of", "on", "one",
    "or", "our", "out", "please", "she", "should", "so", "some", "still", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "to", "try", "trying", "up", "us", "use", "using", "want", "was",
    "we", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your", "thanks", "hi", "hello", "help",
}

# BM25 knobs. Standard defaults; k1 controls how fast repeated terms stop
# helping, b how hard long documents are penalised.
K1 = 1.5
B  = 0.75

# Title and keyword hits say more about what an article is *about* than a
# passing mention in the body, so those fields count for more.
TITLE_WEIGHT    = 3
KEYWORD_WEIGHT  = 2

# Textbook BM25 drives IDF to nearly zero for a term that appears in most
# documents, which is right for a web-scale corpus and wrong for this one. A
# knowledge base is small and topical: if a tenant has written five VPN articles
# then "vpn" is in every document, IDF collapses, and a search for "vpn" returns
# nothing at all. The floor keeps a ubiquitous term meaningful.
IDF_FLOOR = 0.3

# Relevance is BM25 score multiplied by the share of the query's meaningful words
# that matched, and that product is what gets thresholded.
#
# Neither half works alone, which took a few tries to accept:
#
#   * Raw score alone cannot separate a one-word question from a rambling one.
#     "password" against an article all about passwords scores about 1.4, while
#     "order new laptops for the sales team" glancing off the word "laptop" in
#     the Wi-Fi article scores 1.14. Any cutoff between them is a coin flip, and
#     raising it to be safe silences the legitimate one-word question entirely.
#   * Coverage alone is worse, and as a hard gate it is brittle: "I forgot my
#     password and I'm locked out" reduces to three meaningful words, so one
#     solid hit on "password" is 1-in-3 and missed a 0.34 cutoff by a hundredth,
#     while the identical hit in a two-word question sailed through.
#
# Multiplied, they say something sensible: how strong the match is, weighted by
# how much of the question it actually addresses. The incidental laptop hit
# collapses to 0.23 (one word of five) while the one-word password question
# holds at 1.4, and they stop being adjacent.
MIN_RELEVANCE   = 0.4
RELATIVE_CUTOFF = 0.4    # drop results below this fraction of the best relevance

# Budget for what gets injected into the prompt. Deliberately small: a 7B local
# model may only have 8k of context, and burning it on knowledge base text is
# what makes a small model start ignoring its instructions.
MAX_ARTICLES        = 3
MAX_CHARS_PER_ARTICLE = 1200
MAX_CHARS_TOTAL     = 3500

_WORD_RE = re.compile(r"[a-z0-9]+")


def _stem(w: str) -> str:
    """Fold common English endings so "expiring", "expired" and "expire" all
    meet at one token.

    Not a real stemmer, on purpose. This corpus is full of short technical terms
    ("dns", "gpo", "nps", "vpn") that a full Porter implementation is as likely
    to mangle as to help, so nothing under five characters is touched at all.
    Over-stemming is fairly harmless here because query and document text go
    through this same function: as long as both sides fold identically, an
    aggressive stem costs precision only when two genuinely different words
    collide, which is rare in a vocabulary this small.
    """
    if len(w) <= 4:
        return w
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    # Trailing 'e' last, so "expire" lands on the same stem as "expiring".
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def tokenize(text: str) -> list:
    """Lowercase, split on non-alphanumerics, drop stopwords and single chars,
    then fold word endings with _stem."""
    out = []
    for w in _WORD_RE.findall((text or "").lower()):
        if len(w) < 2 or w in STOPWORDS:
            continue
        out.append(_stem(w))
    return out


def _doc_tokens(article: dict) -> list:
    """Field-weighted token list for one article, by repeating the stronger
    fields. Cheap, and it keeps a single BM25 pass rather than one per field."""
    toks = []
    toks += tokenize(article.get("title") or "") * TITLE_WEIGHT
    toks += tokenize(article.get("keywords") or "") * KEYWORD_WEIGHT
    toks += tokenize(article.get("body") or "")
    return toks


def search(query: str, articles: list, limit: int = MAX_ARTICLES) -> list:
    """Rank articles against a query with BM25.

    Returns [{article, score, matched}] best first, or [] when nothing clears the
    thresholds. Pure function of its inputs: no database, no network, so it can
    be tested directly.
    """
    # Keep each stem alongside the word the user actually typed, so results can
    # report "expiring" rather than the internal stem "expir".
    q_pairs = [(_stem(w), w) for w in _WORD_RE.findall((query or "").lower())
               if len(w) >= 2 and w not in STOPWORDS]
    if not q_pairs or not articles:
        return []
    q_unique = {s for s, _ in q_pairs}
    surface  = {}
    for s, w in q_pairs:
        surface.setdefault(s, w)

    docs = []
    for a in articles:
        if not a.get("enabled", 1):
            continue
        toks = _doc_tokens(a)
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        docs.append({"article": a, "tf": tf, "len": len(toks)})
    if not docs:
        return []

    n_docs = len(docs)
    avgdl  = sum(d["len"] for d in docs) / n_docs

    # Document frequency per query term.
    df = {t: sum(1 for d in docs if t in d["tf"]) for t in q_unique}

    scored = []
    for d in docs:
        score   = 0.0
        matched = []
        for t in q_unique:
            f = d["tf"].get(t, 0)
            if not f:
                continue
            matched.append(t)
            # +1 inside the log keeps IDF positive even for a term in every
            # document, so a common word can never push a score negative.
            idf  = max(math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5)), IDF_FLOOR)
            norm = f * (K1 + 1) / (f + K1 * (1 - B + B * d["len"] / avgdl))
            score += idf * norm
        if not matched:
            continue
        coverage  = len(matched) / len(q_unique)
        relevance = score * coverage
        if relevance < MIN_RELEVANCE:
            continue
        scored.append({"article": d["article"], "score": round(relevance, 3),
                       "matched": sorted(surface.get(t, t) for t in matched),
                       "coverage": round(coverage, 2),
                       "bm25": round(score, 3)})

    if not scored:
        return []
    scored.sort(key=lambda r: r["score"], reverse=True)
    # Keep the leaders, drop the long tail. Three weak matches crowd out one good
    # one in a fixed prompt budget, and give the model more chances to pick wrong.
    floor = scored[0]["score"] * RELATIVE_CUTOFF
    return [r for r in scored if r["score"] >= floor][:limit]


def _truncate(body: str, limit: int = MAX_CHARS_PER_ARTICLE) -> str:
    """Trim to the limit on a paragraph or sentence edge where possible, so an
    article never ends mid-instruction and read as though it were complete."""
    body = (body or "").strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    for sep in ("\n\n", "\n", ". "):
        idx = cut.rfind(sep)
        if idx > limit * 0.5:
            return cut[:idx].rstrip() + "\n[article truncated]"
    return cut.rstrip() + "...\n[article truncated]"


def build_context_block(results: list) -> str:
    """Format retrieved articles for the system prompt.

    The wording matters. The model is told these are *candidates that may not be
    relevant*, and that it should ignore them rather than stretch them to fit,
    because a retrieved-but-wrong article is the main way this feature can make
    answers worse instead of better.
    """
    if not results:
        return ""
    lines = [
        "",
        "",
        "KNOWLEDGE BASE (articles written by this organisation's IT admins, "
        "retrieved for this question):",
        "These are candidates from a keyword search and may not be relevant. If an "
        "article answers the question, use it and say which one you used. If none "
        "of them fit, ignore them and answer normally: never stretch an article to "
        "fit a question it does not actually cover. These describe THIS "
        "organisation's setup, so prefer them over general knowledge when they "
        "conflict.",
    ]
    used = 0
    for r in results:
        a = r["article"]
        body = _truncate(a.get("body") or "")
        chunk = f"\n--- {a.get('title')} ---\n{body}"
        if used + len(chunk) > MAX_CHARS_TOTAL:
            break
        lines.append(chunk)
        used += len(chunk)
    return "\n".join(lines)


def retrieve_for_prompt(query: str, articles: list):
    """Convenience wrapper: returns (context_block, used_article_ids)."""
    results = search(query, articles)
    if not results:
        return "", []
    block = build_context_block(results)
    # Only count articles that actually made it into the block, not everything
    # that scored, or the usage figures overstate what the model really saw.
    ids, used = [], 0
    for r in results:
        chunk_len = len(_truncate(r["article"].get("body") or "")) + len(r["article"].get("title") or "") + 12
        if used + chunk_len > MAX_CHARS_TOTAL:
            break
        ids.append(r["article"]["id"])
        used += chunk_len
    return block, ids
