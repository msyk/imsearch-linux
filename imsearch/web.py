"""Web UI + HTTP API (DESIGN §8 Phase 2, §13: /search and /ask as the public interface).

    GET  /                 browser UI (imsearch/static/index.html)
    GET  /api/search?q=…   hybrid search only (no generation; fast)
    POST /api/ask          {"q": "…", "sources": [...]} → Server-Sent Events stream
    GET  /api/health       index / model status

Low-memory policy: answer generation runs one at a time (a 4GB box can hold only one
generation's KV cache); further requests wait in a short queue and are refused with 503
when the queue is full. Client disconnects abort the generation upstream.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Query as Q
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import config, llm, store
from .answer import ask as ask_fn, search as search_fn, translate_to_en
from .retrieve import Hit

STATIC = Path(__file__).resolve().parent / "static"
SOURCES = {"manual", "code", "samples"}

app = FastAPI(title="imsearch", description="INTER-Mediator 開発者向け検索", docs_url="/api/docs",
              redoc_url=None, openapi_url="/api/openapi.json")

_gen_slots = threading.Semaphore(config.WEB_CONCURRENCY)  # answers being generated
_waiting = 0  # requests generating or queued (touched only on the event loop)


def _excerpt(text: str, n: int = 220) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("# ") and not l.startswith("```")]
    s = " ".join(" ".join(lines).split())
    return s[:n] + ("…" if len(s) > n else "")


def _hit_json(h: Hit, i: int, full: bool = False) -> dict:
    d = {"n": i, "source": h.source, "kind": h.kind, "title": h.title, "url": h.url,
         "excerpt": _excerpt(h.content), "score": round(h.score * 1000, 2), "via": h.via}
    if full:
        d["content"] = h.content
    return d


def _sources(sources: Optional[list[str]]) -> Optional[list[str]]:
    if not sources:
        return None
    bad = set(sources) - SOURCES
    if bad:
        raise HTTPException(400, f"unknown source: {', '.join(sorted(bad))}")
    return sources


def _check_question(q: str) -> str:
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "質問が空です")
    if len(q) > config.WEB_MAX_QUESTION:
        raise HTTPException(400, f"質問が長すぎます（{config.WEB_MAX_QUESTION} 文字まで）")
    return q


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health():
    con = store.connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        upd = con.execute("SELECT MAX(updated_at) FROM chunks").fetchone()[0]
    finally:
        con.close()
    try:
        loaded = [m.get("name") for m in llm.ollama_ps()]
        llm_ok = True
    except Exception:
        loaded, llm_ok = [], False
    return {"chunks": n, "updated_at": upd, "chat_model": config.CHAT_MODEL, "embed_model": config.EMBED_MODEL,
            "llm_ok": llm_ok, "loaded_models": loaded, "busy": _waiting}


@app.get("/api/search")
def search(q: str = Q(..., description="質問・キーワード"), n: int = Q(10, ge=1, le=50),
           source: Optional[list[str]] = Q(None, description="manual / code / samples")):
    q = _check_question(q)
    t = time.time()
    con = store.connect()
    try:
        query, hits = search_fn(con, q, limit=n, sources=_sources(source))
    except llm.LLMError as e:
        raise HTTPException(503, str(e))
    finally:
        con.close()
    return {"lang": query.lang, "query_ja": query.ja_query, "identifiers": query.identifiers,
            "elapsed": round(time.time() - t, 2), "hits": [_hit_json(h, i) for i, h in enumerate(hits, 1)]}


class AskRequest(BaseModel):
    q: str = Field(..., description="質問（日本語または英語）")
    sources: Optional[list[str]] = None
    show_context: bool = False


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _ask_worker(q: str, sources: Optional[list[str]], show_context: bool, stop: threading.Event,
                emit, t0: float) -> None:
    """Runs in a worker thread (blocking LLM calls). Checks `stop` between tokens; closing the
    token generator closes the HTTP stream to Ollama, which aborts the generation there. `t0` is when the request arrived (queue time included)."""
    try:
        emit("status", {"message": "検索中…"})
        con = store.connect()
        try:
            query, ctx, stream = ask_fn(con, q, sources=sources)
        finally:
            con.close()
        emit("meta", {"lang": query.lang, "query_ja": query.ja_query, "identifiers": query.identifiers,
                      "sources": [_hit_json(h, i, full=show_context) for i, h in enumerate(ctx, 1)],
                      "elapsed": round(time.time() - t0, 1)})
        if query.lang == "en":
            emit("status", {"message": "回答を生成中（日本語）… / generating the answer in Japanese…"})
            ja = ""
            with closing(stream):
                for tok in stream:
                    if stop.is_set():
                        return
                    ja += tok
                    emit("ja_token", {"t": tok})
            emit("status", {"message": "Translating into English…"})
            stream = translate_to_en(ja)
        else:
            emit("status", {"message": "回答を生成中…"})
        with closing(stream):
            for tok in stream:
                if stop.is_set():
                    return
                emit("token", {"t": tok})
        emit("done", {"elapsed": round(time.time() - t0, 1)})
    except llm.LLMError as e:
        emit("error", {"message": str(e)})
    except Exception as e:  # keep the stream well-formed for the browser
        emit("error", {"message": f"内部エラー: {type(e).__name__}: {e}"})
    finally:
        _gen_slots.release()


async def _ask_events(q: str, sources: Optional[list[str]], show_context: bool) -> AsyncIterator[str]:
    global _waiting
    if _waiting >= config.WEB_CONCURRENCY + config.WEB_MAX_QUEUE:
        yield _sse("error", {"message": "混み合っています。しばらくしてからもう一度お試しください。"})
        return
    _waiting += 1
    stop = threading.Event()
    t0 = time.time()
    try:
        ahead = _waiting - config.WEB_CONCURRENCY
        if ahead > 0:
            yield _sse("status", {"message": f"順番待ち（前に {ahead} 件）…"})
        # Poll instead of blocking a thread: a client that leaves while waiting just stops polling.
        while not _gen_slots.acquire(blocking=False):
            await asyncio.sleep(0.3)
        # From here the worker owns the slot and releases it when it really finishes.
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()

        def emit(event: str, data) -> None:
            loop.call_soon_threadsafe(events.put_nowait, _sse(event, data))

        def run() -> None:
            try:
                _ask_worker(q, sources, show_context, stop, emit, t0)
            finally:
                loop.call_soon_threadsafe(events.put_nowait, None)

        threading.Thread(target=run, name="imsearch-ask", daemon=True).start()
        while True:
            item = await events.get()
            if item is None:
                break
            yield item
    finally:
        stop.set()      # client gone or finished: the worker stops at the next token
        _waiting -= 1


@app.post("/api/ask")
def ask(req: AskRequest):
    q = _check_question(req.q)
    return StreamingResponse(_ask_events(q, _sources(req.sources), req.show_context),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
