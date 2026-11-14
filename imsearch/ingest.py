"""Ingest pipeline: crawl/pull -> parse -> embed -> store."""
from __future__ import annotations

import hashlib
import subprocess

from . import config, llm, store
from .crawl import crawl, page_path
from .parse_code import repo_code_chunks
from .parse_html import parse_manual_page
from .parse_samples import SampleUnit, sample_chunks

SOURCES = ("manual", "code", "samples")

DESCRIBE_PROMPT = """以下は、Webアプリケーションフレームワーク INTER-Mediator のサンプルアプリについての情報です。
このサンプルが「何を実現しているか」「どのような記述や機能の参考になるか」を、日本語で2〜3文で説明してください。
情報から読み取れないことは書かないでください。説明文のみを出力してください。

## 抽出した情報
{facts}

## ページファイル（抜粋）
```html
{page}
```"""


def update_repo(name: str, log=print):
    repo = config.REPOS[name]
    d = repo["dir"]
    if (d / ".git").exists():
        # shallow clone: fetch only the newest commit and move to it (keeps disk/RAM use small)
        r = subprocess.run(["git", "-C", str(d), "fetch", "-q", "--depth", "1", "origin", "HEAD"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            r = subprocess.run(["git", "-C", str(d), "reset", "-q", "--hard", "FETCH_HEAD"],
                               capture_output=True, text=True)
        if r.returncode != 0:
            log(f"[yellow]git update failed for {name}[/]: {r.stderr.strip()}")
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--single-branch", repo["url"], str(d)], check=True)


def _describer(con, log):
    def describe(u: SampleUnit, facts: str, page: str) -> str:
        prompt = DESCRIBE_PROMPT.format(facts=facts, page=page[: config.DESCRIBE_PAGE_CHARS])
        h = hashlib.sha256((config.CHAT_MODEL + prompt).encode()).hexdigest()
        cached = store.cache_get(con, h)
        if cached is not None:
            return cached
        log(f"  describing {u.sample_id}", end="\r")
        try:
            text = llm.chat([{"role": "user", "content": prompt}], temperature=0.1).strip()
        except Exception as e:  # keep ingest going without the description
            log(f"[yellow]describe failed[/] {u.sample_id}: {e}")
            return ""
        store.cache_put(con, h, text)
        return text
    return describe


def ingest(sources=SOURCES, update: bool = True, describe: bool = True, log=print):
    con = store.connect()
    for src in sources:
        log(f"[bold]== {src}[/]")
        if src == "manual":
            pages = crawl(log=log) if update else _cached_pages()
            chunks = []
            for rel, p in pages.items():
                html = page_path(p).read_text(encoding="utf-8")
                chunks += parse_manual_page(rel, p["url"], p.get("title", ""), html)
        elif src == "code":
            if update:
                update_repo("code", log)
            r = config.REPOS["code"]
            chunks = repo_code_chunks(r["dir"], r["url"], log=log)
        elif src == "samples":
            if update:
                update_repo("samples", log)
            r = config.REPOS["samples"]
            chunks = sample_chunks(r["dir"], r["url"], r["demo"],
                                   describe=_describer(con, log) if describe else None, log=log)
        else:
            raise ValueError(src)
        store.sync_source(con, src, chunks, llm.embed, log=log)
    con.close()


def _cached_pages() -> dict:
    """Use the crawl cache without network access."""
    import json

    from .crawl import is_excluded
    saved = config.MANUAL_CACHE_DIR / "_pages.json"
    if saved.exists():
        return {k: v for k, v in json.loads(saved.read_text()).items() if not is_excluded(k)}
    pages = {}
    for f in sorted(config.MANUAL_CACHE_DIR.glob("*.html")):
        rel = f.name.replace("__", "/")
        if rel == "index.html" or is_excluded(rel):
            continue
        pages[rel] = {"url": config.MANUAL_BASE + rel, "path": f.name, "title": ""}
    return pages
