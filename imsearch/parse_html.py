"""Manual HTML -> Markdown -> heading-based chunks (+ definition keys, + API entries)."""
from __future__ import annotations

import re

from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import config
from .chunk import Chunk, split_text

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
KEY_ROW = re.compile(r"^\|\s*'([A-Za-z0-9_\-/]+)'\s*\|\s*(.+?)\s*\|?\s*$")
API_HEAD = re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|::|->)[\w$]+)+|[A-Za-z_$][\w$]{3,}(?=\s*\()")
NOT_API = {"function", "require", "require_once", "include", "array"}


def clean_ws(s: str) -> str:
    return " ".join(s.split())


# Link texts that say nothing about the target page ("こちらのページ" etc.)
GENERIC_LINK = re.compile(r"^(?:こちら|ここ|これ|詳細|詳しく|参照|リンク|→|＞|>)")


def resolve_page_title(rel: str, link_title: str, html: str) -> str:
    """Title of a manual page: config override > descriptive link text > the page's own headings.

    The crawler records the text of the first link to each page; for pages only linked as
    "こちらのページ" that is useless, and some pages have neither <title> nor a page-level h1."""
    override = (config.exclude_config()["manual"].get("titles") or {}).get(rel)
    if override:
        return clean_ws(override)
    t = clean_ws(link_title or "")
    if t and not GENERIC_LINK.match(t):
        return t
    tree = HTMLParser(html)
    tt = tree.css_first("title")
    if tt and tt.text(strip=True):
        return clean_ws(tt.text(strip=True))
    root = tree.css_first("article") or tree.body
    h1s = [clean_ws(h.text(strip=True)) for h in (root.css("h1") if root else [])]
    h1s = [h for h in h1s if h]
    return "・".join(h1s[:3]) or rel


def html_to_markdown(html: str) -> tuple[str, str]:
    """Return (first_h1_text, markdown of <article>)."""
    tree = HTMLParser(html)
    root = tree.css_first("article") or tree.body
    if root is None:
        return "", ""
    for sel in ("script", "style", "nav", "noscript"):
        for n in root.css(sel):
            n.decompose()
    for n in root.css("[style]"):  # hidden index markers in the EPUB-derived tutorial
        if re.search(r"display\s*:\s*none", n.attributes.get("style") or ""):
            n.decompose()
    h1 = root.css_first("h1")
    md = markdownify(root.html, heading_style="ATX", bullets="-", code_language="",
                     strip=["img"], escape_underscores=False, escape_asterisks=False)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return (h1.text(strip=True) if h1 else ""), md


def _sections(md: str) -> list[tuple[list[str], int, str]]:
    """Split markdown into (heading_path, level, body); ignores '#' inside code fences."""
    out, path, level, buf, in_fence = [], [], 0, [], False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else HEADING.match(line)
        if m:
            if buf and "".join(buf).strip():
                out.append((list(path), level, "\n".join(buf).strip()))
            buf = []
            level = len(m.group(1))
            text = m.group(2).strip().strip("#").strip()
            path = path[: level - 1] + [""] * max(0, level - 1 - len(path)) + [text]
            path = [p for p in path[:level]]
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        out.append((list(path), level, "\n".join(buf).strip()))
    return out


def _api_symbols(text: str) -> list[str]:
    ascii_ratio = sum(1 for c in text if ord(c) < 128) / max(1, len(text))
    if ascii_ratio <= 0.8:
        return []
    return [s for s in dict.fromkeys(API_HEAD.findall(text)) if s not in NOT_API]


def parse_manual_page(rel: str, url: str, title: str, html: str) -> list[Chunk]:
    _, md = html_to_markdown(html)
    page_title = resolve_page_title(rel, title, html)
    category = rel.split("/")[0] if "/" in rel else ""
    chunks: list[Chunk] = []

    for i, (path, level, body) in enumerate(_sections(md)):
        heads = [clean_ws(p) for p in path if p.strip()]
        if heads and heads[0] == page_title:
            heads = heads[1:]
        head_path = " > ".join([page_title] + heads)
        last = heads[-1] if heads else ""
        kind, symbol = "section", ""
        syms = _api_symbols(last) if last else []
        if syms:
            kind = "api"
            symbol = " ".join(syms)
        for j, part in enumerate(split_text(body)):
            text = f"# {head_path}\n\n{part}" if kind == "section" else f"# {head_path}\n\n`{last}`\n\n{part}"
            chunks.append(Chunk(
                source="manual", kind=kind, lang="ja", title=head_path, url=url,
                content=text, symbol=symbol, ckey=f"manual|{rel}|{i}|{j}",
                extra={"category": category},
            ))

        # definition-file keys: markdown table rows like | 'name' | description |
        for line in body.split("\n"):
            km = KEY_ROW.match(line.strip())
            if not km:
                continue
            key, desc = km.group(1), km.group(2).strip().strip("|").strip()
            if len(desc) < 4:
                continue
            chunks.append(Chunk(
                source="manual", kind="definition_key", lang="ja",
                title=f"{head_path} > '{key}'", url=url,
                content=f"# {head_path}\n\n定義ファイルのキー `'{key}'`: {desc}",
                symbol=key, ckey=f"manual|{rel}|key|{head_path}|{key}",
                extra={"category": category},
            ))
    return chunks
