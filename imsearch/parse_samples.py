"""INTER-Mediator_Samples -> sample summaries, page files, definition files, code, schema."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from . import config
from .chunk import Chunk, split_text
from .parse_code import _PARSERS, _t, code_chunks, git_sha, markdown_chunks

DB_SUFFIX = re.compile(r"_(MySQL|PostgreSQL|SQLite|SQLServer|FMS_DataAPI|FMDataAPI|FileMaker\w*|Oracle|MongoDB)(?=[_.]|$)")
DB_PREFERENCE = ["MySQL", "SQLite", "PostgreSQL"]
DEF_EXT = {".yaml", ".yml", ".json"}
LICENSE_HEADER = re.compile(r"^<\?php\s*/\*\*.*?\*/\s*", re.S)
PHP_KEY = re.compile(r"'([a-z][a-z0-9\-]*)'\s*=>")
API_USE = re.compile(r"\b(INTERMediator(?:OnPage|Lib|Log)?|IMLib\w+|IMParts_\w+)\.(\w+)")
MAX_DEF = 3500


@dataclass
class SampleUnit:
    sample_id: str                 # e.g. "Sample_search/search"
    page_rel: str
    title: str
    variants: list[str]
    def_rels: list[str] = field(default_factory=list)
    js_rels: list[str] = field(default_factory=list)
    data_im: list[str] = field(default_factory=list)
    data_im_control: list[str] = field(default_factory=list)
    def_keys: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)
    weight: float = 1.0


def _cfg() -> dict:
    return config.exclude_config()["samples"]


def _excluded(rel: str) -> bool:
    return any(rel.startswith(p) for p in _cfg().get("exclude_prefixes", []))


def _weight(rel: str) -> float:
    return 0.5 if any(rel.startswith(p) for p in _cfg().get("low_priority_prefixes", [])) else 1.0


def _is_local_lib(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return ".min." in name or rel.startswith("vendor/") or "/vendor/" in rel or name.startswith("jquery")


def _strip_license(text: str) -> str:
    return LICENSE_HEADER.sub("<?php\n", text, count=1)


def _resolve_refs(repo: Path, html_path: Path, tree: HTMLParser) -> tuple[list[str], list[str]]:
    defs, js = [], []
    for s in tree.css("script[src]"):
        src = s.attributes.get("src") or ""
        u = urlparse(src)
        if u.scheme or src.startswith("//"):
            continue
        cand: list[Path] = []
        if u.path.endswith("index.php") and "deffile" in u.query:
            d = parse_qs(u.query).get("deffile", [""])[0]
            cand.append(html_path.parent / Path(d).name)
        elif u.path.endswith("index.php"):  # convention: <page>.yaml / .yml / .json next to the page
            cand += [html_path.with_suffix(ext) for ext in (".yaml", ".yml", ".json")]
        elif not u.path.endswith("index.php"):
            cand.append((html_path.parent / u.path).resolve())
        for p in cand:
            try:
                rel = p.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError:
                continue
            if not p.is_file() or _is_local_lib(rel) or _excluded(rel):
                continue
            if p.suffix in DEF_EXT:
                defs.append(rel)
            elif p.suffix == ".php":
                txt = p.read_text(encoding="utf-8", errors="replace")
                if "IM_Entry" in txt:
                    defs.append(rel)
                    for m in re.finditer(r"['\"]([\w\-]+\.(?:ya?ml|json))['\"]", txt):
                        y = p.parent / m.group(1)
                        if y.is_file():
                            defs.append(y.relative_to(repo).as_posix())
            elif p.suffix == ".js":
                js.append(rel)
    return list(dict.fromkeys(defs)), list(dict.fromkeys(js))


def _yaml_keys(obj, acc: set[str]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                acc.add(k)
            _yaml_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _yaml_keys(v, acc)


def _def_info(path: Path) -> tuple[list[str], list[str]]:
    """(keys used, context names)"""
    text = path.read_text(encoding="utf-8", errors="replace")
    keys: set[str] = set()
    contexts: list[str] = []
    if path.suffix == ".php":
        keys |= set(PHP_KEY.findall(text))
        contexts = re.findall(r"'name'\s*=>\s*'([^']+)'", text)
        heredoc = re.search(r"<<<'?(\w+)'?\n(.*?)\n\s*\1;", text, re.S)
        if heredoc:
            text, path = heredoc.group(2), path.with_suffix(".yaml")
        else:
            return sorted(keys), contexts
    try:
        obj = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except Exception:
        return sorted(keys), contexts
    _yaml_keys(obj, keys)
    if isinstance(obj, dict):
        for c in obj.get("contexts", []) or []:
            if isinstance(c, dict) and c.get("name"):
                contexts.append(str(c["name"]))
    return sorted(keys), contexts


def scan_units(repo: Path) -> list[SampleUnit]:
    groups: dict[str, list[Path]] = {}
    for html in sorted(repo.rglob("*.html")):
        rel = html.relative_to(repo).as_posix()
        if _excluded(rel) or "vendor/" in rel or "node_modules/" in rel or "/" not in rel:
            continue
        key = DB_SUFFIX.sub("", rel[:-5])
        groups.setdefault(key, []).append(html)

    units = []
    for key, files in groups.items():
        def pref(p: Path) -> int:
            m = DB_SUFFIX.search(p.stem)
            db = m.group(1) if m else ""
            return DB_PREFERENCE.index(db) if db in DB_PREFERENCE else (-1 if not m else 9)
        files.sort(key=pref)
        rep = files[0]
        rel = rep.relative_to(repo).as_posix()
        tree = HTMLParser(rep.read_text(encoding="utf-8", errors="replace"))
        defs, js = _resolve_refs(repo, rep, tree)
        if not defs and not tree.css_first("[data-im]"):
            continue  # not an INTER-Mediator page (menus, static docs)
        title_node = tree.css_first("title")
        u = SampleUnit(
            sample_id=key, page_rel=rel, title=(title_node.text(strip=True) if title_node else ""),
            variants=[m.group(1) for f in files if (m := DB_SUFFIX.search(f.stem))],
            def_rels=defs, js_rels=js, weight=_weight(rel),
        )
        for n in tree.css("*"):
            for a, v in (n.attributes or {}).items():
                if a == "data-im" and v:
                    u.data_im.append(v)
                elif a.startswith("data-im-") and v is not None:
                    u.data_im_control.append(f"{a}={v}" if v else a)
        keys: set[str] = set()
        for d in defs:
            k, c = _def_info(repo / d)
            keys |= set(k)
            u.contexts += c
        apis = set()
        for j in js:
            apis |= {f"{a}.{b}" for a, b in API_USE.findall((repo / j).read_text(encoding="utf-8", errors="replace"))}
        for s in tree.css("script"):
            if not s.attributes.get("src"):
                apis |= {f"{a}.{b}" for a, b in API_USE.findall(s.text() or "")}
        u.data_im = list(dict.fromkeys(u.data_im))[:80]
        u.data_im_control = list(dict.fromkeys(u.data_im_control))[:40]
        u.def_keys, u.contexts, u.apis = sorted(keys), list(dict.fromkeys(u.contexts)), sorted(apis)
        units.append(u)
    return units


def summary_facts(u: SampleUnit) -> str:
    lines = [
        f"サンプル: {u.sample_id}（ページ: {u.page_rel}）",
        f"タイトル: {u.title}" if u.title else "",
        f"対応DB: {', '.join(u.variants)}" if u.variants else "",
        f"定義ファイル: {', '.join(u.def_rels)}" if u.def_rels else "",
        f"JavaScript: {', '.join(u.js_rels)}" if u.js_rels else "",
        f"コンテキスト: {', '.join(u.contexts)}" if u.contexts else "",
        f"定義ファイルのキー: {', '.join(u.def_keys)}" if u.def_keys else "",
        f"data-im: {', '.join(u.data_im[:40])}" if u.data_im else "",
        f"data-im-*属性: {', '.join(u.data_im_control)}" if u.data_im_control else "",
        f"使用API: {', '.join(u.apis)}" if u.apis else "",
    ]
    return "\n".join(l for l in lines if l)


def _clean_page(html: str) -> str:
    tree = HTMLParser(html)
    for sel in ("style", "svg", "link", "meta"):
        for n in tree.css(sel):
            n.decompose()
    for s in tree.css("script"):
        if not s.attributes.get("src") and len(s.text() or "") > 1500:
            s.replace_with(f"<!-- inline script ({len(s.text())} chars) omitted -->")
    out = tree.html or ""
    out = re.sub(r"<!DOCTYPE[^>]*>", "", out, flags=re.I)
    out = "\n".join(l.rstrip() for l in out.splitlines() if l.strip())
    return out


def _php_contexts(text: str) -> list[tuple[str, str]]:
    """Split an IM_Entry() call into (label, code) parts: one per context, plus the remaining arguments."""
    tree = _PARSERS["php"].parse(text.encode("utf-8"))
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "function_call_expression" and _t(n.child_by_field_name("function")) == "IM_Entry":
            args = n.child_by_field_name("arguments")
            if args is None:
                return []
            arg_nodes = [a for a in args.named_children if a.type == "argument"]
            parts = []
            if arg_nodes:
                first = arg_nodes[0].named_children[0] if arg_nodes[0].named_children else None
                if first is not None and first.type == "array_creation_expression":
                    for el in first.named_children:
                        if el.type == "array_element_initializer":
                            code = _t(el)
                            m = re.search(r"'name'\s*=>\s*'([^']+)'", code)
                            parts.append((f"context {m.group(1) if m else '?'}", code))
                rest = ",\n".join(_t(a) for a in arg_nodes[1:])
                if rest.strip():
                    parts.append(("options / db settings", rest))
            return parts
        stack.extend(n.named_children)
    return []


def def_chunks(repo: Path, rel: str, *, repo_url: str, sha: str, sample_id: str, weight: float) -> list[Chunk]:
    path = repo / rel
    text = path.read_text(encoding="utf-8", errors="replace")
    lang = "php" if path.suffix == ".php" else ("json" if path.suffix == ".json" else "yaml")
    text = _strip_license(text) if lang == "php" else text
    keys, contexts = _def_info(path)
    url = f"{repo_url}/blob/{sha}/{rel}"
    parts: list[tuple[str, str]]
    if len(text) <= MAX_DEF:
        parts = [("", text)]
    elif lang == "php":
        parts = _php_contexts(text) or [("", p) for p in split_text(text, MAX_DEF, MAX_DEF)]
    else:
        parts = [("", p) for p in split_text(text, MAX_DEF, MAX_DEF)]
    out = []
    for j, (label, code) in enumerate(parts):
        for k, piece in enumerate(split_text(code, MAX_DEF, MAX_DEF)):
            title = f"定義ファイル {rel}" + (f" — {label}" if label else "")
            out.append(Chunk(
                source="samples", kind="sample_def", lang=lang, title=title, url=url,
                content=f"# {title}（サンプル {sample_id}）\n\n```{lang}\n{piece}\n```",
                symbol=" ".join(dict.fromkeys(contexts + keys)), sample_id=sample_id, commit_sha=sha,
                weight=weight, ckey=f"samples|def|{rel}|{j}|{k}",
            ))
    return out


def schema_chunks(repo: Path, repo_url: str, sha: str) -> list[Chunk]:
    out = []
    for p in sorted((repo / "lib").glob("MySQL_Schema/*.sql")):
        rel = p.relative_to(repo).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"CREATE\s+(?:TABLE|VIEW)\s+(?:IF NOT EXISTS\s+)?`?(\w+)`?.*?;", text, re.S | re.I):
            name = m.group(1)
            line = text.count("\n", 0, m.start()) + 1
            out.append(Chunk(
                source="samples", kind="schema", lang="sql", title=f"スキーマ {name} ({rel})",
                url=f"{repo_url}/blob/{sha}/{rel}#L{line}", symbol=name,
                content=f"# サンプルDBのテーブル定義: {name}\n\n```sql\n{m.group(0)[:MAX_DEF]}\n```",
                commit_sha=sha, ckey=f"samples|schema|{rel}|{name}",
            ))
    return out


def sample_chunks(repo: Path, repo_url: str, demo_base: str, describe=None, log=print) -> list[Chunk]:
    """`describe(unit, facts, page_excerpt) -> str` optionally adds an LLM-written description."""
    sha = git_sha(repo)
    units = scan_units(repo)
    chunks: list[Chunk] = []
    used_defs: set[str] = set()
    used_code: set[str] = set()

    for u in units:
        facts = summary_facts(u)
        page_html = _clean_page((repo / u.page_rel).read_text(encoding="utf-8", errors="replace"))
        desc = describe(u, facts, page_html[:3000]) if describe else ""
        demo = demo_base + u.page_rel
        body = (f"# サンプルアプリ: {u.sample_id}\n\n" + (f"{desc}\n\n" if desc else "") + facts +
                f"\nデモ: {demo}")
        chunks.append(Chunk(
            source="samples", kind="sample", lang="ja", title=f"サンプル {u.sample_id}" + (f"「{u.title}」" if u.title else ""),
            url=f"{repo_url}/blob/{sha}/{u.page_rel}", content=body,
            symbol=" ".join(dict.fromkeys(u.contexts + u.def_keys + u.apis)), sample_id=u.sample_id,
            commit_sha=sha, weight=u.weight, ckey=f"samples|summary|{u.sample_id}",
            extra={"demo": demo},
        ))
        for j, part in enumerate(split_text(page_html, MAX_DEF, MAX_DEF)):
            chunks.append(Chunk(
                source="samples", kind="sample_page", lang="html", title=f"ページファイル {u.page_rel}",
                url=f"{repo_url}/blob/{sha}/{u.page_rel}",
                content=f"# ページファイル {u.page_rel}（サンプル {u.sample_id}）\n\n```html\n{part}\n```",
                symbol=" ".join(dict.fromkeys(u.data_im[:60] + u.data_im_control)), sample_id=u.sample_id,
                commit_sha=sha, weight=u.weight, ckey=f"samples|page|{u.page_rel}|{j}",
            ))
        for d in u.def_rels:
            if d not in used_defs:
                used_defs.add(d)
                chunks += def_chunks(repo, d, repo_url=repo_url, sha=sha, sample_id=u.sample_id, weight=u.weight)
        for j in u.js_rels:
            if j not in used_code:
                used_code.add(j)
                chunks += code_chunks(repo / j, j, source="samples", repo_url=repo_url, sha=sha,
                                      weight=u.weight, sample_id=u.sample_id, kind_prefix="sample_")

    # remaining own code (server-side extensions etc.) and docs
    for p in sorted(list(repo.rglob("*.php")) + list(repo.rglob("*.js")) + list(repo.rglob("*.md"))):
        rel = p.relative_to(repo).as_posix()
        if rel in used_defs or rel in used_code or _excluded(rel) or _is_local_lib(rel) or "node_modules/" in rel:
            continue
        if p.suffix == ".md":
            chunks += markdown_chunks(p, rel, source="samples", repo_url=repo_url, sha=sha)
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".php" and "IM_Entry" in text:  # definition files not linked from a page
            chunks += def_chunks(repo, rel, repo_url=repo_url, sha=sha, sample_id=rel.rsplit(".", 1)[0],
                                 weight=_weight(rel))
        elif len(text) > 200:
            chunks += code_chunks(p, rel, source="samples", repo_url=repo_url, sha=sha, weight=_weight(rel),
                                  sample_id=rel.split("/")[0], kind_prefix="sample_")
    chunks += schema_chunks(repo, repo_url, sha)
    log(f"samples: {len(units)} sample pages -> {len(chunks)} chunks (commit {sha[:8]})")
    return chunks
