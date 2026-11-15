"""Crawl the Japanese manual (https://inter-mediator.info/ja/) with conditional GETs."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import config
from .parse_html import GENERIC_LINK

USER_AGENT = "imsearch/0.1 (INTER-Mediator manual indexer)"
MAX_PAGES = 400
OLD_VERSION_TEXT = re.compile(r"Ver\.\s*[\d.]+\s*まで|\(Ver\.\s*[\d.]+\)|（Ver\.\s*[\d.]+）|Ver\.\s*\d+\s*の")


def _rel(url: str) -> str:
    return url[len(config.MANUAL_BASE):] if url.startswith(config.MANUAL_BASE) else url


def page_path(p: dict):
    """Cached HTML file of a page entry (accepts old absolute paths from another machine)."""
    from pathlib import Path
    return config.MANUAL_CACHE_DIR / Path(p["path"]).name


def is_excluded(rel: str) -> bool:
    ex = config.exclude_config()["manual"]
    if rel in ex.get("exclude_paths", []):
        return True
    return any(rel.startswith(p) for p in ex.get("exclude_prefixes", []))


def _normalize(href: str, base: str) -> str | None:
    url, _ = urldefrag(urljoin(base, href))
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not url.startswith(config.MANUAL_BASE):
        return None
    path = u.path
    if path.endswith("/"):
        url += "index.html" if url != config.MANUAL_BASE else ""
    elif "." not in path.rsplit("/", 1)[-1]:
        url += "/index.html"
    if not (url.endswith(".html") or url == config.MANUAL_BASE):
        return None
    return url


def crawl(log=print) -> dict[str, dict]:
    """Return {rel_path: {"url", "title", "path"}} for all crawled, non-excluded pages."""
    cache_dir = config.MANUAL_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cache_dir / "_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    confirmed = set(config.exclude_config()["manual"].get("confirmed_current", []))

    titles: dict[str, str] = {}
    pages: dict[str, dict] = {}
    seen = {config.MANUAL_BASE}
    queue = deque([config.MANUAL_BASE])
    warned = set()
    stats = {"fetched": 0, "not_modified": 0, "failed": 0}

    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        while queue and len(seen) <= MAX_PAGES:
            url = queue.popleft()
            rel = _rel(url) or "index.html"
            fname = cache_dir / (rel.replace("/", "__") or "index.html")
            m = meta.get(rel, {})
            headers = {}
            if fname.exists():
                if m.get("etag"):
                    headers["If-None-Match"] = m["etag"]
                if m.get("last_modified"):
                    headers["If-Modified-Since"] = m["last_modified"]
            try:
                r = c.get(url, headers=headers)
            except httpx.HTTPError as e:
                log(f"[yellow]fetch failed[/] {url}: {e}")
                stats["failed"] += 1
                continue
            if r.status_code == 304:
                stats["not_modified"] += 1
                html = fname.read_text(encoding="utf-8")
            elif r.status_code == 200:
                stats["fetched"] += 1
                html = r.text
                fname.write_text(html, encoding="utf-8")
                meta[rel] = {"etag": r.headers.get("etag", ""), "last_modified": r.headers.get("last-modified", "")}
                time.sleep(0.2)
            else:
                log(f"[yellow]HTTP {r.status_code}[/] {url}")
                stats["failed"] += 1
                continue

            tree = HTMLParser(html)
            for a in tree.css("a[href]"):
                nxt = _normalize(a.attributes.get("href") or "", url)
                if not nxt:
                    continue
                nrel = _rel(nxt)
                text = " ".join(a.text(strip=True).split())
                # keep the first descriptive link text ("こちらのページ" is replaced by a later, better one)
                if text and (nrel not in titles or GENERIC_LINK.match(titles[nrel])) and not GENERIC_LINK.match(text):
                    titles[nrel] = text
                elif text and nrel not in titles:
                    titles[nrel] = text
                if is_excluded(nrel):
                    continue
                if text and OLD_VERSION_TEXT.search(text) and nrel not in confirmed and nrel not in warned:
                    warned.add(nrel)
                    log(f"[yellow]possible old-version page[/] {nrel} 「{text}」 "
                        f"→ add to config/exclude.yaml (exclude_paths or confirmed_current)")
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)

            if url != config.MANUAL_BASE and not is_excluded(rel):
                pages[rel] = {"url": url, "path": fname.name}  # relative: the cache dir may be copied elsewhere

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    for rel, p in pages.items():
        p["title"] = titles.get(rel, "")
    (cache_dir / "_pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=1))
    log(f"manual crawl: {len(pages)} pages "
        f"(fetched {stats['fetched']}, not modified {stats['not_modified']}, failed {stats['failed']})")
    return pages
