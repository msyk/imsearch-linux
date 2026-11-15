"""SQLite store: chunks + FTS5 (bigram-tokenized) + sqlite-vec, with incremental sync."""
from __future__ import annotations

import json
import re
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Callable, Iterable

import sqlite_vec

from . import config
from .chunk import Chunk, cjk_bigrams

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY,
  ckey         TEXT UNIQUE NOT NULL,
  source       TEXT NOT NULL,
  kind         TEXT NOT NULL,
  lang         TEXT,
  title        TEXT,
  symbol       TEXT,
  url          TEXT,
  sample_id    TEXT,
  commit_sha   TEXT,
  weight       REAL DEFAULT 1.0,
  content      TEXT,
  content_hash TEXT,
  extra        TEXT,
  updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
CREATE INDEX IF NOT EXISTS chunks_sample ON chunks(sample_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, symbol, body, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{config.EMBED_DIM}] distance_metric=cosine);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (hash TEXT PRIMARY KEY, text TEXT);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    if not hasattr(con, "enable_load_extension"):
        raise SystemExit("この Python の sqlite3 は拡張の読み込みに対応していません。"
                         "Ubuntu 標準の python3（apt の python3-venv）で venv を作ってください。")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    # Low-memory settings: bounded page cache, no mmap, temp data on disk.
    con.execute(f"PRAGMA cache_size=-{config.SQLITE_CACHE_KB}")
    con.execute("PRAGMA mmap_size=0")
    con.execute("PRAGMA temp_store=FILE")
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)
    _check_embed_model(con)
    _check_fts(con)
    return con


def _check_embed_model(con: sqlite3.Connection):
    """The index is only valid for the embedding model it was built with (DESIGN §13 portability)."""
    row = con.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
    cur = f"{config.EMBED_MODEL}:{config.EMBED_DIM}"
    if row is None:
        con.execute("INSERT INTO meta VALUES ('embed_model', ?)", (cur,))
        con.commit()
    elif row[0] != cur:
        raise SystemExit(f"index was built with embedding model {row[0]}, but {cur} is configured. "
                         f"Delete {config.DB_PATH} and re-ingest, or change IMSEARCH_EMBED_MODEL.")


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# Bump when fts_text() changes: connect() then rebuilds the keyword index from the stored chunks
# (no re-embedding needed).
FTS_VERSION = "2"
_CAMEL_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*")
_CAMEL_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def camel_words(text: str) -> str:
    """'AuthPasskeyHandler' -> 'Auth Passkey Handler', so the word 'passkey' finds that class."""
    words = []
    for ident in _CAMEL_IDENT.findall(text):
        words += _CAMEL_PART.findall(ident)
    return " ".join(dict.fromkeys(words))


def fts_text(title: str, symbol: str, content: str) -> tuple[str, str, str]:
    sym = " ".join(p for p in (symbol, camel_words(f"{title} {symbol}")) if p)
    return cjk_bigrams(title), sym, cjk_bigrams(content)


def _check_fts(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
    if row and row[0] == FTS_VERSION:
        return
    if con.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
        con.execute("DELETE FROM chunks_fts")
        for r in con.execute("SELECT id, title, symbol, content FROM chunks").fetchall():
            con.execute("INSERT INTO chunks_fts (rowid, title, symbol, body) VALUES (?,?,?,?)",
                        (r["id"], *fts_text(r["title"] or "", r["symbol"] or "", r["content"] or "")))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('fts_version', ?)", (FTS_VERSION,))
    con.commit()


def _delete_ids(con: sqlite3.Connection, ids: Iterable[int]):
    for i in ids:
        con.execute("DELETE FROM chunks WHERE id=?", (i,))
        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (i,))
        con.execute("DELETE FROM chunks_vec WHERE rowid=?", (i,))


def sync_source(con: sqlite3.Connection, source: str, chunks: list[Chunk],
                embed: Callable[[list[str]], list[list[float]]], batch: int | None = None, log=print) -> dict:
    """Make the stored chunks of `source` equal to `chunks`; only new/changed chunks are embedded."""
    uniq: dict[str, Chunk] = {}
    for c in chunks:
        uniq.setdefault(c.ckey, c)
    existing = {r["ckey"]: (r["id"], r["content_hash"])
                for r in con.execute("SELECT id, ckey, content_hash FROM chunks WHERE source=?", (source,))}
    stale = [existing[k][0] for k in existing if k not in uniq]
    todo = [c for k, c in uniq.items() if k not in existing or existing[k][1] != c.content_hash]

    _delete_ids(con, stale)
    _delete_ids(con, [existing[c.ckey][0] for c in todo if c.ckey in existing])
    con.commit()

    batch = batch or config.EMBED_BATCH
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs = embed([c.embed_text() for c in part])
        for c, v in zip(part, vecs):
            cur = con.execute(
                "INSERT INTO chunks (ckey, source, kind, lang, title, symbol, url, sample_id, commit_sha, weight,"
                " content, content_hash, extra, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c.ckey, c.source, c.kind, c.lang, c.title, c.symbol, c.url, c.sample_id, c.commit_sha, c.weight,
                 c.content, c.content_hash, json.dumps(c.extra, ensure_ascii=False), now))
            rid = cur.lastrowid
            con.execute("INSERT INTO chunks_fts (rowid, title, symbol, body) VALUES (?,?,?,?)",
                        (rid, *fts_text(c.title, c.symbol, c.content)))
            con.execute("INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)", (rid, pack(v)))
        con.commit()
        done += len(part)
        log(f"  {source}: embedded {done}/{len(todo)}", end="\r")
    if todo:
        log("")
    stats = {"total": len(uniq), "added_or_changed": len(todo), "removed": len(stale)}
    log(f"  {source}: {stats}")
    return stats


def cache_get(con: sqlite3.Connection, h: str) -> str | None:
    r = con.execute("SELECT text FROM llm_cache WHERE hash=?", (h,)).fetchone()
    return r[0] if r else None


def cache_put(con: sqlite3.Connection, h: str, text: str):
    con.execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?)", (h, text))
    con.commit()


def stats(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT source, kind, COUNT(*) n FROM chunks GROUP BY source, kind ORDER BY source, n DESC").fetchall()
