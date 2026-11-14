"""Hybrid retrieval: bigram BM25 (FTS5) + exact symbol match + vector KNN, fused with RRF."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import config, llm
from .chunk import cjk_bigrams
from .store import pack

_TOKEN = re.compile(r"[\w぀-ヿ㐀-鿿]+")


@dataclass
class Hit:
    id: int
    source: str
    kind: str
    title: str
    url: str
    content: str
    symbol: str
    sample_id: str
    weight: float
    score: float = 0.0
    via: list[str] = field(default_factory=list)


def _fts_query(text: str) -> str:
    toks = []
    for t in _TOKEN.findall(cjk_bigrams(text, drop_hiragana=True)):
        if len(t) >= 2 or not t.isascii():
            toks.append(t.lower())
    toks = list(dict.fromkeys(toks))[:60]
    return " OR ".join(f'"{t}"' for t in toks)


def keyword_search(con: sqlite3.Connection, text: str, k: int) -> list[int]:
    q = _fts_query(text)
    if not q:
        return []
    rows = con.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 4.0, 6.0, 1.0) LIMIT ?",
        (q, k)).fetchall()
    return [r[0] for r in rows]


def symbol_search(con: sqlite3.Connection, idents: list[str], k: int) -> list[int]:
    """Exact identifier match on the symbol column ('constructMain' also matches 'INTERMediator.constructMain')."""
    out: list[int] = []
    for ident in idents[:8]:
        rows = con.execute("SELECT id, symbol, kind, source FROM chunks WHERE symbol LIKE ? LIMIT 400",
                           (f"%{ident}%",)).fetchall()
        exact = []
        for r in rows:
            for s in (r["symbol"] or "").split():
                if s == ident or re.search(rf"(?:\.|::|->|\\){re.escape(ident)}$", s) or s.strip("'") == ident:
                    exact.append(r)
                    break
        # documentation first, then samples, then code
        order = {"manual": 0, "samples": 1, "code": 2}
        exact.sort(key=lambda r: (order.get(r["source"], 3), r["kind"] != "api" and r["kind"] != "definition_key"))
        out += [r["id"] for r in exact]
    return list(dict.fromkeys(out))[:k]


def vector_search(con: sqlite3.Connection, text: str, k: int) -> list[int]:
    vec = llm.embed([text], query=True)[0]
    rows = con.execute("SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                       (pack(vec), k)).fetchall()
    return [r[0] for r in rows]


def hybrid(con: sqlite3.Connection, query: str, identifiers: list[str] | None = None,
           extra_keywords: list[str] | None = None, limit: int = 20,
           sources: list[str] | None = None) -> list[Hit]:
    k = config.CANDIDATES
    kw_text = " ".join([query] + (extra_keywords or []) + (identifiers or []))
    rankings = {
        "bm25": (keyword_search(con, kw_text, k), 1.0),
        "vector": (vector_search(con, query, k), 1.0),
        "symbol": (symbol_search(con, identifiers or [], k), 1.5),
    }
    scores: dict[int, float] = {}
    via: dict[int, list[str]] = {}
    for name, (ids, w) in rankings.items():
        for rank, i in enumerate(ids):
            scores[i] = scores.get(i, 0.0) + w / (config.RRF_K + rank + 1)
            via.setdefault(i, []).append(name)
    if not scores:
        return []
    qmarks = ",".join("?" * len(scores))
    rows = con.execute(f"SELECT * FROM chunks WHERE id IN ({qmarks})", list(scores)).fetchall()
    hits = []
    for r in rows:
        if sources and r["source"] not in sources:
            continue
        h = Hit(r["id"], r["source"], r["kind"], r["title"], r["url"], r["content"], r["symbol"] or "",
                r["sample_id"] or "", r["weight"], scores[r["id"]] * r["weight"], via[r["id"]])
        hits.append(h)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def select_context(con: sqlite3.Connection, hits: list[Hit], n: int | None = None,
                   budget_chars: int | None = None) -> list[Hit]:
    """Pick diverse chunks for the LLM; attach page + definition file to top sample summaries."""
    n = n or config.CONTEXT_CHUNKS
    budget_chars = budget_chars or config.CONTEXT_CHARS
    chosen: list[Hit] = []
    per_url: dict[str, int] = {}
    used = 0
    for h in hits:
        if len(chosen) >= n:
            break
        if per_url.get(h.url, 0) >= 2 or used + len(h.content) > budget_chars:
            continue
        chosen.append(h)
        per_url[h.url] = per_url.get(h.url, 0) + 1
        used += len(h.content)

    extras: list[Hit] = []
    for h in [h for h in chosen if h.kind == "sample"][:2]:
        for kind in ("sample_def", "sample_page"):
            r = con.execute("SELECT * FROM chunks WHERE sample_id=? AND kind=? ORDER BY id LIMIT 1",
                            (h.sample_id, kind)).fetchone()
            if r and all(c.id != r["id"] for c in chosen + extras) and used + len(r["content"]) <= budget_chars:
                extras.append(Hit(r["id"], r["source"], r["kind"], r["title"], r["url"], r["content"],
                                  r["symbol"] or "", r["sample_id"] or "", r["weight"], 0.0, ["sample-expand"]))
                used += len(r["content"])
    return chosen + extras
