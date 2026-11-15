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
           sources: list[str] | None = None, vector_query: str | None = None) -> list[Hit]:
    k = config.CANDIDATES
    kw_text = " ".join([query] + (extra_keywords or []) + (identifiers or []))
    rankings = {
        "bm25": (keyword_search(con, kw_text, k), 1.0),
        "vector": (vector_search(con, vector_query or query, k), 1.0),
        "symbol": (symbol_search(con, identifiers or [], k), 1.5),
    }
    if extra_keywords:
        # The added terms (English glossary words for a Japanese question, the English original of a
        # translated one) get a ranking of their own: mixed into one OR query they are outweighed
        # by common katakana bigrams ("パスキー" -> パス / キー match every password page).
        rankings["terms"] = (keyword_search(con, " ".join(extra_keywords), k), 1.0)
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


# --- prerequisite hop -------------------------------------------------------------------
# A sample that matches the question ("パスキー" -> Auth_Support/Passkey) works only because of
# settings in its definition file (authentication / storing: credential / target: field-user).
# Those settings are explained in the manual pages that never mention the question's words, so
# similarity search cannot reach them. Follow the sample's definition keys to the manual's
# explanation of each key instead.

_DEF_LINE = re.compile(r"^\s*(?:-\s+)?([A-Za-z][\w-]*)\s*:\s*(.*)$|'([A-Za-z][\w-]*)'\s*=>\s*(.*)$")
_WORD = re.compile(r"[A-Za-z][\w-]{2,}")
_df_cache: dict[str, float] | None = None


def _def_keys(content: str) -> tuple[list[str], set[str]]:
    """(keys in order, value words) of a YAML / PHP definition file."""
    keys, values = [], set()
    for line in content.splitlines():
        for m in _DEF_LINE.finditer(line):
            key = m.group(1) or m.group(3)
            keys.append(key)
            values.update(w.lower() for w in _WORD.findall(m.group(2) or m.group(4) or ""))
    return list(dict.fromkeys(keys)), values


def _key_doc_freq(con: sqlite3.Connection) -> dict[str, float]:
    """Share of sample definition files using each key ('name' ~ 1.0, 'storing' rare)."""
    global _df_cache
    if _df_cache is None:
        rows = con.execute("SELECT content FROM chunks WHERE kind='sample_def'").fetchall()
        cnt: dict[str, int] = {}
        for r in rows:
            for k in _def_keys(r["content"])[0]:
                cnt[k] = cnt.get(k, 0) + 1
        _df_cache = {k: v / max(1, len(rows)) for k, v in cnt.items()}
    return _df_cache


def prerequisites(con: sqlite3.Connection, hits: list[Hit], max_keys: int | None = None,
                  max_df: float = 0.25) -> list[Hit]:
    """The manual page that explains the top sample hits' distinctive settings: its intro section
    plus the rows for up to `max_keys` of those settings."""
    max_keys = config.PREREQ_KEYS if max_keys is None else max_keys
    if max_keys <= 0:
        return []
    sample_ids = list(dict.fromkeys(h.sample_id for h in hits[:5] if h.source == "samples" and h.sample_id))[:2]
    if not sample_ids:
        return []
    df = _key_doc_freq(con)
    keys: list[str] = []
    values: set[str] = set()
    for sid in sample_ids:
        r = con.execute("SELECT content FROM chunks WHERE sample_id=? AND kind='sample_def' ORDER BY id LIMIT 1",
                        (sid,)).fetchone()
        if r:
            k, v = _def_keys(r["content"])
            keys += k
            values |= v
    keys = sorted(dict.fromkeys(k for k in keys if df.get(k, 1.0) <= max_df), key=lambda k: df.get(k, 1.0))
    if not keys:
        return []
    # manual rows explaining each key, grouped by page
    by_page: dict[str, dict[str, tuple[int, sqlite3.Row]]] = {}
    for k in keys:
        rows = con.execute("SELECT * FROM chunks WHERE source='manual' AND kind='definition_key' AND symbol=?",
                           (k,)).fetchall()
        for r in rows:
            # several rows per key on one page: prefer the one mentioning the sample's values
            overlap = len(values & {w.lower() for w in _WORD.findall(r["content"])})
            cur = by_page.setdefault(r["url"], {}).get(k)
            if cur is None or overlap > cur[0]:
                by_page[r["url"]][k] = (overlap, r)
    if not by_page:
        return []
    # the page explaining most of the sample's distinctive settings is its prerequisite reading
    pages = sorted(by_page, key=lambda u: -sum(1.0 - df.get(k, 1.0) for k in by_page[u]))
    seen = {h.id for h in hits}
    out: list[Hit] = []

    def add(r: sqlite3.Row) -> None:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(Hit(r["id"], r["source"], r["kind"], r["title"], r["url"], r["content"],
                           r["symbol"] or "", r["sample_id"] or "", r["weight"], 0.0, ["prereq"]))

    intro = con.execute("SELECT * FROM chunks WHERE url=? AND kind='section' ORDER BY id LIMIT 1",
                        (pages[0],)).fetchone()
    if intro:
        add(intro)
    covered: set[str] = set()
    for u in pages:
        for k in sorted(by_page[u], key=lambda k: df.get(k, 1.0)):
            if len(out) >= max_keys + 1 or k in covered:
                continue
            covered.add(k)
            add(by_page[u][k][1])
    return out


def select_context(con: sqlite3.Connection, hits: list[Hit], n: int | None = None,
                   budget_chars: int | None = None) -> list[Hit]:
    """Pick diverse chunks for the LLM; attach page + definition file to top sample summaries,
    then the manual's explanation of that sample's settings (prerequisites)."""
    n = n or config.CONTEXT_CHUNKS
    budget_chars = budget_chars or config.CONTEXT_CHARS
    prereq: list[Hit] = []
    reserved = 0
    for p in prerequisites(con, hits):
        if reserved + len(p.content) <= config.PREREQ_CHARS:
            prereq.append(p)
            reserved += len(p.content)
    budget_chars -= reserved
    prereq_ids = {p.id for p in prereq}
    hits = [h for h in hits if h.id not in prereq_ids]
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
    return chosen + extras + prereq
