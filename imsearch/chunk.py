"""Chunk model and text helpers shared by all parsers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MAX_CHARS = 1800      # Japanese text: ~1 token per 1-1.5 chars
TARGET_CHARS = 1200


@dataclass
class Chunk:
    source: str            # manual | code | doc | samples
    kind: str              # section | definition_key | api | function | class | example | sample | ...
    lang: str              # ja | php | js | md | html | yaml | sql
    title: str             # heading path or qualified name
    url: str
    content: str
    symbol: str = ""       # space-separated identifiers for exact match
    sample_id: str = ""
    commit_sha: str = ""
    weight: float = 1.0    # retrieval weight (low priority = 0.5)
    ckey: str = ""         # stable identity used for incremental updates
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.ckey:
            self.ckey = f"{self.source}|{self.url}|{self.title}"

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for part in (self.title, self.symbol, self.content, self.url, str(self.weight)):
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def embed_text(self) -> str:
        return f"{self.title}\n{self.symbol}\n{self.content}"[:3000]


def split_text(text: str, max_chars: int = MAX_CHARS, target: int = TARGET_CHARS) -> list[str]:
    """Split on blank lines (never inside ``` fences), packing paragraphs up to `target`."""
    if len(text) <= max_chars:
        return [text]
    blocks, cur, in_fence = [], [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and not line.strip() and cur:
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))

    parts, buf = [], ""
    for b in blocks:
        while len(b) > max_chars:  # a single huge block (e.g. long code)
            if buf:
                parts.append(buf)
                buf = ""
            cut = b.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            parts.append(b[:cut])
            b = b[cut:].lstrip("\n")
        if buf and len(buf) + len(b) + 2 > target:
            parts.append(buf)
            buf = b
        else:
            buf = f"{buf}\n\n{b}" if buf else b
    if buf:
        parts.append(buf)
    return parts


_CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ"
_CJK_RUN = re.compile(f"[{_CJK}ー]+")
_HIRAGANA_ONLY = re.compile(r"^[぀-ゟ]+$")


def cjk_bigrams(text: str, drop_hiragana: bool = False) -> str:
    """Index-side tokenization: CJK runs -> overlapping bigrams; ASCII left as-is.

    FTS5's unicode61 tokenizer then splits on spaces/punctuation, giving
    bigram BM25 for Japanese without a morphological analyzer.
    """
    def repl(m: re.Match) -> str:
        run = m.group(0)
        if len(run) == 1:
            return f" {run} "
        grams = [run[i:i + 2] for i in range(len(run) - 1)]
        if drop_hiragana:
            grams = [g for g in grams if not _HIRAGANA_ONLY.match(g)]
        return " " + " ".join(grams) + " "
    return _CJK_RUN.sub(repl, text)


def cjk_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if re.match(f"[{_CJK}]", c)) / len(letters)


IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|::|->)[A-Za-z_$][\w$]*)*(?:-[a-z][\w]*)*")


def identifiers(text: str) -> list[str]:
    """ASCII identifiers that look like API / key names (dotted, hyphenated, camelCase, snake)."""
    out = []
    for m in IDENT_RE.finditer(text):
        s = m.group(0)
        if len(s) < 3:
            continue
        plain_word = s.isalpha() and s[0].isupper() and s[1:].islower()  # "How", "Docker"
        if any(ch in s for ch in ".:-_>") or re.search(r"[a-z][A-Z]", s) or (s[0].isupper() and not plain_word):
            out.append(s)
    return list(dict.fromkeys(out))
