"""Query analysis, translation (EN <-> JA) and grounded answer generation."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterator

from . import config, llm
from .chunk import cjk_ratio, identifiers
from .retrieve import Hit, hybrid, prerequisites, select_context


def _glossary_text() -> str:
    return "\n".join(f"- {g['ja']} = {g['en']}" for g in config.glossary())


def glossary_terms(text: str) -> list[str]:
    """English terms for the glossary words found in a Japanese question ("パスキー" -> "passkey").
    The source code, sample paths and English identifiers are written in English, so a Japanese
    question reaches them only through these terms (no LLM call: instant on a CPU-only box)."""
    out = []
    for g in config.glossary():
        ja, en = str(g.get("ja") or ""), str(g.get("en") or "")
        if ja and en and ja in text:
            out.append(en)
    return list(dict.fromkeys(out))


def detect_lang(text: str) -> str:
    """'ja' or 'en'. A query made only of identifiers ("repeat-control", "IMLibContextPool.contextFromName")
    is not English prose, so it is searched as is ('ja' = no translation)."""
    if cjk_ratio(text) >= 0.2:
        return "ja"
    words = text.split()
    ids = set(identifiers(text))
    if len(words) == 1 or len(words) <= 3 and all(w.strip("?.,!'\"`") in ids for w in words):
        return "ja"
    return "en"


@dataclass
class Query:
    original: str
    lang: str
    ja_query: str
    keywords: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)


ANALYZE_PROMPT = """あなたは Webアプリケーションフレームワーク INTER-Mediator の開発者向け検索システムのクエリ解析器です。
利用者の質問から、日本語のマニュアル・サンプル・ソースコードを検索するための情報を JSON で出力してください。

出力形式:
{{"ja_query": "質問を自然な日本語の検索文にしたもの（質問が英語なら日本語に翻訳）",
  "keywords": ["検索に効く日本語の重要語（3〜6語）"],
  "identifiers": ["質問に関係しそうな INTER-Mediator のAPI名・定義ファイルのキー名・data-im関連の属性名など英字の識別子（確信がある場合のみ、最大5個）"]}}

規則:
- 質問に含まれる英字の識別子（例: repeat-control, INTERMediator.constructMain）は翻訳せず identifiers にそのまま入れる。
- 用語は次の対応表に従って訳す。
{glossary}

質問: {question}"""


# Small models (1-4B) echo the input when given a Japanese instruction; an English system
# prompt with one example translates reliably.
QUERY_TRANSLATE_SYSTEM = """You are a translator. Translate the user's English question about the INTER-Mediator web framework into natural Japanese suitable for searching the Japanese manual.
Keep identifiers (API names, definition-file keys, data-im values) unchanged.
Glossary (Japanese = English):
{glossary}
Output only the Japanese translation, in one line."""
QUERY_TRANSLATE_SHOTS = [
    {"role": "user", "content": "How do I use repeat-control in the definition file?"},
    {"role": "assistant", "content": "定義ファイルで repeat-control を使うには？"},
]


def translate_query_to_ja(question: str) -> str:
    msgs = [{"role": "system", "content": QUERY_TRANSLATE_SYSTEM.format(glossary=_glossary_text())},
            *QUERY_TRANSLATE_SHOTS, {"role": "user", "content": question}]
    return _clean_line(llm.chat(msgs, temperature=0.0))


def _clean_line(text: str) -> str:
    lines = [l.strip().strip("「」\"") for l in text.strip().splitlines() if l.strip()]
    return lines[0] if lines else ""


def analyze(question: str, use_llm: bool = True) -> Query:
    """Language detection, EN->JA translation and identifier extraction.

    IMSEARCH_ANALYZE_MODE=simple (default, for small models): the LLM only translates English
    questions with a plain prompt; identifiers come from the question text itself.
    IMSEARCH_ANALYZE_MODE=json (large models): one JSON call also proposes keywords/identifiers.
    """
    lang = detect_lang(question)
    q = Query(original=question, lang=lang, ja_query=question, identifiers=identifiers(question))
    if lang == "ja":
        q.keywords = glossary_terms(question)
    if not use_llm and lang == "ja":
        return q
    if config.ANALYZE_MODE != "json":
        if lang == "en":
            ja = translate_query_to_ja(question)
            if ja and cjk_ratio(ja) >= 0.2:
                q.ja_query = ja
                q.keywords = [question]  # the English text still helps the bigram/ASCII keyword search
        return q
    data = llm.chat_json([{"role": "user", "content": ANALYZE_PROMPT.format(
        glossary=_glossary_text(), question=question)}])
    q.ja_query = str(data.get("ja_query") or question)
    q.keywords = list(dict.fromkeys(q.keywords + [str(k) for k in data.get("keywords") or [] if k][:8]))
    llm_ids = [str(i) for i in data.get("identifiers") or [] if i][:5]
    q.identifiers = list(dict.fromkeys(q.identifiers + llm_ids))
    return q


def _vector_query(q: Query) -> str:
    """A short Japanese question embeds poorly ("パスキー" lands on generic overview pages);
    the glossary's English terms anchor it to the code and samples that use those words."""
    if q.lang == "ja" and q.keywords:
        return f"{q.ja_query} ({', '.join(q.keywords)})"
    return q.ja_query


def _hybrid(con: sqlite3.Connection, q: Query, limit: int, sources: list[str] | None) -> list[Hit]:
    return hybrid(con, q.ja_query, q.identifiers, q.keywords, limit=limit, sources=sources,
                  vector_query=_vector_query(q))


def search(con: sqlite3.Connection, question: str, limit: int = 10, expand: bool = False,
           sources: list[str] | None = None) -> tuple[Query, list[Hit]]:
    q = analyze(question, use_llm=expand)
    hits = _hybrid(con, q, max(limit, 20), sources)
    pre = prerequisites(con, hits) if not sources or "manual" in sources else []
    top = hits[:limit]
    ids = {h.id for h in top}
    return q, top + [p for p in pre if p.id not in ids]  # appended after the ranked hits


SYSTEM_PROMPT = """あなたは Webアプリケーションフレームワーク INTER-Mediator のアプリケーション開発者を支援するアシスタントです。
与えられた「資料」（公式マニュアル、サンプルアプリ、ソースコードの抜粋）だけを根拠に、日本語で回答します。

回答の方針:
- 開発者の目的を達成するために「何をどこに書けばよいか」を具体的に示す。必要に応じて
  1) ページファイル（HTML、data-im 属性など）
  2) 定義ファイル（IM_Entry のコンテキスト定義やオプション、YAML）
  3) JavaScript / PHP の API
  の順に、短いコード例を添えて説明する。
- 根拠にした資料の番号を、該当する文や段落の末尾に [1] のように付ける。
- 資料から分からないことは推測で補わず、「資料からは確認できません」と明記する。
- 資料にない API 名やキー名を作り出さない。
- 回答の最後に参照先の一覧は書かない（システムが別途表示する）。"""


def build_context(hits: list[Hit]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        src = {"manual": "マニュアル", "samples": "サンプル", "code": "ソースコード"}.get(h.source, h.source)
        blocks.append(f"[{i}] ({src}) {h.title}\nURL: {h.url}\n{h.content}")
    return "\n\n---\n\n".join(blocks)


def answer_stream(question_ja: str, hits: list[Hit]) -> Iterator[str]:
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"# 資料\n\n{build_context(hits)}\n\n# 質問\n\n{question_ja}"},
    ]
    return llm.chat_stream(msgs, temperature=0.2)


# Instructions go in the system prompt and the text alone in the user turn: small models
# otherwise echo the instructions back.
TRANSLATE_SYSTEM = """You translate Japanese text about the INTER-Mediator web framework into English.
Rules:
- Keep code blocks, identifiers (API names, definition keys, data-im values), URLs and citation markers like [1] exactly as they are.
- Keep the Markdown structure (headings, lists, code fences).
- Use this glossary (Japanese = English):
{glossary}
- Output only the English translation of the user's text. Do not add explanations."""
TRANSLATE_SHOTS = [
    {"role": "user", "content": "定義ファイルの `repeat-control` に `insert delete` を指定すると、追加と削除のボタンが表示されます [1]。"},
    {"role": "assistant", "content": "If you specify `insert delete` for `repeat-control` in the definition file, "
                                     "the add and delete buttons are shown [1]."},
]


def translate_to_en(text: str) -> Iterator[str]:
    msgs = [{"role": "system", "content": TRANSLATE_SYSTEM.format(glossary=_glossary_text())},
            *TRANSLATE_SHOTS, {"role": "user", "content": text}]
    return llm.chat_stream(msgs, temperature=0.1)


def ask(con: sqlite3.Connection, question: str, sources: list[str] | None = None):
    """Returns (query, context_hits, token_stream_ja). Caller handles EN back-translation."""
    q = analyze(question, use_llm=config.ANALYZE_JA)
    hits = _hybrid(con, q, 30, sources)
    ctx = select_context(con, hits)
    return q, ctx, answer_stream(q.ja_query if q.lang == "en" else question, ctx)
