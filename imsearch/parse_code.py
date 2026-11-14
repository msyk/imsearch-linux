"""Source code -> class/method/function chunks via tree-sitter (PHP, JavaScript)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_javascript as ts_js
import tree_sitter_php as ts_php
from tree_sitter import Language, Node, Parser

from .chunk import Chunk, split_text
from .parse_html import _sections, clean_ws

_PARSERS = {
    "php": Parser(Language(ts_php.language_php())),
    "js": Parser(Language(ts_js.language())),
}
_FUNC_VALUES = {"function_expression", "arrow_function", "function", "generator_function"}
MAX_CODE = 3500


@dataclass
class Sym:
    kind: str          # function | method | class
    name: str          # qualified name
    short: str
    start: int         # 1-based line
    end: int
    text: str
    doc: str


def git_sha(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "master"


def _t(n: Node | None) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _doc_before(n: Node) -> str:
    p = n.prev_named_sibling
    if p is not None and p.type == "comment" and _t(p).startswith("/**") and n.start_point[0] - p.end_point[0] <= 1:
        return _t(p)
    return ""


def _walk_js(node: Node, container: list[str], out: list[Sym], members: dict[str, list[str]]):
    for n in node.named_children:
        t = n.type
        if t == "class_declaration" or t == "class":
            name = _t(n.child_by_field_name("name")) or "(anonymous)"
            out.append(Sym("class", ".".join(container + [name]), name, n.start_point[0] + 1, n.end_point[0] + 1,
                           "", _doc_before(n)))
            _walk_js(n, container + [name], out, members)
        elif t == "method_definition":
            name = _t(n.child_by_field_name("name"))
            q = ".".join(container + [name])
            out.append(Sym("method", q, name, n.start_point[0] + 1, n.end_point[0] + 1, _t(n), _doc_before(n)))
            members.setdefault(".".join(container), []).append(name)
        elif t == "function_declaration" or t == "generator_function_declaration":
            name = _t(n.child_by_field_name("name"))
            out.append(Sym("function", ".".join(container + [name]), name, n.start_point[0] + 1,
                           n.end_point[0] + 1, _t(n), _doc_before(n)))
        elif t in ("lexical_declaration", "variable_declaration"):
            doc = _doc_before(n)
            for d in n.named_children:
                if d.type != "variable_declarator":
                    continue
                name, val = _t(d.child_by_field_name("name")), d.child_by_field_name("value")
                if val is None:
                    continue
                if val.type in _FUNC_VALUES:
                    out.append(Sym("function", ".".join(container + [name]), name, n.start_point[0] + 1,
                                   n.end_point[0] + 1, _t(n), doc))
                elif val.type == "object":
                    out.append(Sym("class", ".".join(container + [name]), name, n.start_point[0] + 1,
                                   n.end_point[0] + 1, "", doc))
                    _walk_js(val, container + [name], out, members)
                elif val.type == "class":
                    _walk_js(d, container, out, members)
        elif t == "pair":
            key = _t(n.child_by_field_name("key")).strip("'\"")
            val = n.child_by_field_name("value")
            if val is not None and val.type in _FUNC_VALUES:
                q = ".".join(container + [key])
                out.append(Sym("method", q, key, n.start_point[0] + 1, n.end_point[0] + 1, _t(n), _doc_before(n)))
                members.setdefault(".".join(container), []).append(key)
            elif val is not None and val.type == "object" and len(container) < 3:
                members.setdefault(".".join(container), []).append(key)
                _walk_js(val, container + [key], out, members)
            else:
                members.setdefault(".".join(container), []).append(key)
        elif t == "expression_statement":
            e = n.named_children[0] if n.named_children else None
            if e is not None and e.type == "assignment_expression":
                right = e.child_by_field_name("right")
                if right is not None and right.type in _FUNC_VALUES:
                    left = _t(e.child_by_field_name("left"))
                    out.append(Sym("function", left, left.rsplit(".", 1)[-1], n.start_point[0] + 1,
                                   n.end_point[0] + 1, _t(n), _doc_before(n)))
        elif t in ("class_body", "object", "export_statement", "statement_block"):
            _walk_js(n, container, out, members)


def _walk_php(node: Node, container: list[str], out: list[Sym], members: dict[str, list[str]]):
    for n in node.named_children:
        t = n.type
        if t == "namespace_definition":
            name = _t(n.child_by_field_name("name"))
            body = n.child_by_field_name("body")
            if body is not None:
                _walk_php(body, [name], out, members)
            else:
                container[:] = [name]
        elif t in ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"):
            name = _t(n.child_by_field_name("name"))
            q = "\\".join(container + [name])
            header = _t(n).split("{", 1)[0].strip()
            out.append(Sym("class", q, name, n.start_point[0] + 1, n.end_point[0] + 1, header, _doc_before(n)))
            body = n.child_by_field_name("body")
            if body is not None:
                for m in body.named_children:
                    if m.type == "method_declaration":
                        mname = _t(m.child_by_field_name("name"))
                        out.append(Sym("method", f"{q}::{mname}", mname, m.start_point[0] + 1, m.end_point[0] + 1,
                                       _t(m), _doc_before(m)))
                        sig = _t(m).split("{", 1)[0].strip()
                        members.setdefault(q, []).append(clean_ws(sig))
                    elif m.type in ("property_declaration", "const_declaration"):
                        members.setdefault(q, []).append(clean_ws(_t(m)))
        elif t == "function_definition":
            name = _t(n.child_by_field_name("name"))
            out.append(Sym("function", "\\".join(container + [name]), name, n.start_point[0] + 1,
                           n.end_point[0] + 1, _t(n), _doc_before(n)))
        elif t in ("compound_statement", "program"):
            _walk_php(n, container, out, members)


def extract_symbols(lang: str, src: bytes) -> tuple[list[Sym], dict[str, list[str]]]:
    tree = _PARSERS[lang].parse(src)
    out: list[Sym] = []
    members: dict[str, list[str]] = {}
    (_walk_php if lang == "php" else _walk_js)(tree.root_node, [], out, members)
    return out, members


def _short_doc(doc: str) -> str:
    lines = [l.strip().lstrip("/*").strip() for l in doc.splitlines()]
    lines = [l for l in lines if l and not l.startswith("@")]
    return lines[0] if lines else ""


def code_chunks(path: Path, rel: str, *, source: str, repo_url: str, sha: str,
                weight: float = 1.0, sample_id: str = "", kind_prefix: str = "") -> list[Chunk]:
    """Chunks for one source file. Falls back to file-level splitting if nothing is found."""
    lang = "php" if path.suffix == ".php" else "js"
    raw = path.read_bytes()
    src_text = raw.decode("utf-8", "replace")
    blob = f"{repo_url}/blob/{sha}/{rel}"
    chunks: list[Chunk] = []
    try:
        syms, members = extract_symbols(lang, raw)
    except Exception:
        syms, members = [], {}

    for s in syms:
        url = f"{blob}#L{s.start}-L{s.end}"
        kind = kind_prefix + ("class" if s.kind == "class" else "function")
        symbol = " ".join(dict.fromkeys([s.name, s.short]))
        if s.kind == "class":
            mem = members.get(s.name, [])
            body = "\n".join(f"- {m}" for m in mem[:120])
            text = f"// {rel} L{s.start}-{s.end} ({lang})\n{s.doc}\n{s.text}\n\nメンバー:\n{body}".strip()
            parts = split_text(text, max_chars=MAX_CODE, target=MAX_CODE)
        else:
            text = f"{s.doc}\n{s.text}".strip()
            parts = split_text(text, max_chars=MAX_CODE, target=MAX_CODE)
        for j, part in enumerate(parts):
            head = f"`{s.name}` — {rel} L{s.start}-{s.end}"
            if s.doc and j > 0:
                head += f"（{_short_doc(s.doc)}）"
            chunks.append(Chunk(
                source=source, kind=kind, lang=lang, title=f"{s.name} ({rel})", url=url,
                content=f"{head}\n\n```{lang}\n{part}\n```", symbol=symbol, commit_sha=sha,
                weight=weight, sample_id=sample_id, ckey=f"{source}|{rel}|{s.name}|{s.start}|{j}",
            ))

    if not chunks:  # scripts, tests, or files with only top-level statements
        for j, part in enumerate(split_text(src_text, max_chars=MAX_CODE, target=MAX_CODE)):
            chunks.append(Chunk(
                source=source, kind=kind_prefix + ("example" if weight < 1 else "file"), lang=lang,
                title=f"{rel} (part {j + 1})", url=blob, content=f"// {rel}\n```{lang}\n{part}\n```",
                commit_sha=sha, weight=weight, sample_id=sample_id, ckey=f"{source}|{rel}|file|{j}",
            ))
    return chunks


def markdown_chunks(path: Path, rel: str, *, source: str, repo_url: str, sha: str,
                    weight: float = 1.0, sample_id: str = "") -> list[Chunk]:
    md = path.read_text(encoding="utf-8", errors="replace")
    url = f"{repo_url}/blob/{sha}/{rel}"
    chunks = []
    for i, (heads, _lvl, body) in enumerate(_sections(md) or [([], 0, md)]):
        head_path = " > ".join([rel] + [clean_ws(h) for h in heads if h.strip()])
        for j, part in enumerate(split_text(body)):
            chunks.append(Chunk(
                source=source, kind="doc", lang="md", title=head_path, url=url,
                content=f"# {head_path}\n\n{part}", commit_sha=sha, weight=weight, sample_id=sample_id,
                ckey=f"{source}|{rel}|{i}|{j}",
            ))
    return chunks


def repo_code_chunks(repo: Path, repo_url: str, log=print) -> list[Chunk]:
    """Main INTER-Mediator repository."""
    cfg = __import__("imsearch.config", fromlist=["x"]).exclude_config()["code"]
    sha = git_sha(repo)
    seen: set[Path] = set()
    chunks: list[Chunk] = []
    for weight, globs in ((1.0, cfg["include_globs"]), (0.5, cfg.get("low_priority_globs", []))):
        for g in globs:
            for p in sorted(repo.glob(g)):
                if p in seen or not p.is_file() or "node_modules" in p.parts or "vendor" in p.parts:
                    continue
                seen.add(p)
                rel = p.relative_to(repo).as_posix()
                if p.suffix == ".md":
                    chunks += markdown_chunks(p, rel, source="code", repo_url=repo_url, sha=sha, weight=weight)
                elif p.suffix in (".php", ".js"):
                    chunks += code_chunks(p, rel, source="code", repo_url=repo_url, sha=sha, weight=weight,
                                          kind_prefix="" if weight >= 1 else "")
    log(f"code: {len(seen)} files -> {len(chunks)} chunks (commit {sha[:8]})")
    return chunks
