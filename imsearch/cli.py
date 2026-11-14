"""Command line interface: imsearch ingest | search | ask | stats | eval"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

import typer
import yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from . import config, llm, store
from .answer import analyze, ask as ask_fn, search as search_fn, translate_to_en
from .ingest import SOURCES, ingest as ingest_fn
from .retrieve import Hit, hybrid

app = typer.Typer(add_completion=False, help="INTER-Mediator 開発者向け検索（ローカル RAG / Ollama・省メモリ版）")
console = Console()

SOURCE_LABEL = {"manual": "マニュアル", "samples": "サンプル", "code": "ソース"}


def _log(*args, end="\n"):
    console.print(*args, end=end, highlight=False)


def _excerpt(text: str, n: int = 160) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("# ") and not l.startswith("```")]
    s = " ".join(" ".join(lines).split())
    return s[:n] + ("…" if len(s) > n else "")


def _print_sources(hits: list[Hit]):
    console.print("\n[bold]参考資料 / Sources[/]")
    for i, h in enumerate(hits, 1):
        console.print(f"[cyan][{i}][/] [{SOURCE_LABEL.get(h.source, h.source)}] {h.title}\n    {h.url}",
                      highlight=False)


@app.command()
def ingest(
    source: List[str] = typer.Option(list(SOURCES), "--source", "-s", help="manual / code / samples（複数指定可）"),
    update: bool = typer.Option(True, help="サイトの再クロールとリポジトリの git pull を行う"),
    describe: bool = typer.Option(True, help="サンプルの説明文を LLM で生成する（初回は数分かかる）"),
):
    """情報源を取り込み、索引を作成・差分更新する。"""
    t = time.time()
    ingest_fn([s for s in source if s in SOURCES], update=update, describe=describe, log=_log)
    console.print(f"done in {time.time() - t:.0f}s → {config.DB_PATH}")


@app.command()
def search(
    query: str,
    n: int = typer.Option(10, "-n", help="表示件数"),
    expand: bool = typer.Option(False, "--expand", "-x", help="LLM で検索語・識別子を補う（日本語の質問時）"),
    source: Optional[List[str]] = typer.Option(None, "--source", "-s", help="情報源で絞り込む"),
):
    """検索のみ（LLM による回答なし）。英語の質問は翻訳して検索し、結果も英訳する。"""
    con = store.connect()
    q, hits = search_fn(con, query, limit=n, expand=expand, sources=source)
    if q.lang == "en" or expand:
        console.print(f"[dim]query(ja): {q.ja_query}  ids: {q.identifiers}  kw: {q.keywords}[/]")
    rows = [(h, _excerpt(h.content)) for h in hits]
    if q.lang == "en" and rows:
        listing = "\n".join(f"{i}. {h.title} ||| {ex}" for i, (h, ex) in enumerate(rows, 1))
        translated = "".join(translate_to_en(listing)).splitlines()
        tmap = {}
        for line in translated:
            num, _, rest = line.partition(". ")
            if num.strip().isdigit() and "|||" in rest:
                t, _, e = rest.partition("|||")
                tmap[int(num)] = (t.strip(), e.strip())
        rows = [(h, ex) if i not in tmap else (h, f"{tmap[i][0]} — {tmap[i][1]}") for i, (h, ex) in enumerate(rows, 1)]

    table = Table(show_lines=True)
    table.add_column("#", justify="right")
    table.add_column("源")
    table.add_column("種別")
    table.add_column("タイトル / 抜粋", overflow="fold")
    table.add_column("score", justify="right")
    for i, (h, ex) in enumerate(rows, 1):
        table.add_row(str(i), SOURCE_LABEL.get(h.source, h.source), h.kind,
                      f"[bold]{h.title}[/]\n{ex}\n[blue]{h.url}[/]", f"{h.score * 1000:.1f}\n{'+'.join(h.via)}")
    console.print(table)


@app.command()
def ask(
    question: str,
    source: Optional[List[str]] = typer.Option(None, "--source", "-s", help="情報源で絞り込む"),
    show_ja: bool = typer.Option(False, "--show-ja", help="英語の質問で、翻訳前の日本語回答も表示する"),
    show_context: bool = typer.Option(False, "--show-context", help="LLM に渡した資料を表示する"),
    raw: bool = typer.Option(False, "--raw", help="Markdown を整形せずにストリーム表示する"),
):
    """質問に対し、資料を根拠とした回答を生成する（英語の質問は日本語で検索・回答し英訳）。"""
    con = store.connect()
    t0 = time.time()
    with console.status("検索中…"):
        q, ctx, stream = ask_fn(con, question, sources=source)
    console.print(f"[dim]lang={q.lang}  query(ja)={q.ja_query}  ids={q.identifiers}  "
                  f"({time.time() - t0:.1f}s)[/]")
    if show_context:
        for i, h in enumerate(ctx, 1):
            console.rule(f"[{i}] {h.title}")
            console.print(h.content, markup=False, highlight=False)

    def render(tokens) -> str:
        buf = ""
        if raw:
            for tok in tokens:
                buf += tok
                console.print(tok, end="", markup=False, highlight=False)
            console.print()
        else:
            with console.status("回答を生成中…"):
                buf = "".join(tokens)
            console.print(Markdown(buf))
        return buf

    if q.lang == "en":
        with console.status("回答を生成中（日本語）…"):
            ja = "".join(stream)
        if show_ja:
            console.rule("日本語の回答")
            console.print(Markdown(ja))
            console.rule("English")
        render(translate_to_en(ja))
    else:
        render(stream)
    _print_sources(ctx)
    console.print(f"[dim]{time.time() - t0:.1f}s[/]")


@app.command()
def stats():
    """索引の件数を表示する。"""
    con = store.connect()
    table = Table("source", "kind", "chunks")
    total = 0
    for r in store.stats(con):
        table.add_row(r["source"], r["kind"], str(r["n"]))
        total += r["n"]
    console.print(table)
    console.print(f"total {total} chunks / {config.DB_PATH} "
                  f"({config.DB_PATH.stat().st_size / 1e6:.1f} MB)  embed={config.EMBED_MODEL} chat={config.CHAT_MODEL}")


@app.command(name="eval")
def eval_cmd(
    file: Path = typer.Option(config.ROOT / "eval" / "questions.yaml", help="評価用の質問ファイル"),
    k: int = typer.Option(5, help="Recall@k の k"),
    expand: bool = typer.Option(False, "--expand", "-x", help="LLM でクエリを補う"),
):
    """評価セットで検索の Recall@k を測る（正解 URL の一部が上位 k 件に含まれるか）。"""
    con = store.connect()
    items = yaml.safe_load(file.read_text(encoding="utf-8")) or []
    ok = 0
    table = Table("#", "質問", "期待", f"top{k} に含まれるか", "順位")
    for i, it in enumerate(items, 1):
        _q, hits = search_fn(con, it["q"], limit=max(k, 20), expand=expand)
        expects = it["expect"] if isinstance(it["expect"], list) else [it["expect"]]
        rank = next((r for r, h in enumerate(hits, 1) if any(e in h.url or e in h.title for e in expects)), None)
        hit = rank is not None and rank <= k
        ok += hit
        table.add_row(str(i), it["q"], ", ".join(expects), "✅" if hit else "❌", str(rank or "-"))
    console.print(table)
    console.print(f"Recall@{k}: {ok}/{len(items)} = {ok / max(1, len(items)):.0%}")


@app.command()
def serve(
    host: str = typer.Option(config.WEB_HOST, help="待ち受けアドレス（公開時は nginx 等の後ろに置く）"),
    port: int = typer.Option(config.WEB_PORT, help="ポート番号"),
):
    """Web アプリ（ブラウザ UI と HTTP API）を起動する。"""
    import uvicorn

    if not config.DB_PATH.exists():
        console.print(f"[yellow]索引がまだありません: {config.DB_PATH} → imsearch ingest[/]")
    console.print(f"imsearch web: http://{host}:{port}/  (API: /api/search, /api/ask, /api/docs)")
    # one worker process: the models live in Ollama, the app itself stays small
    uvicorn.run("imsearch.web:app", host=host, port=port, workers=1, log_level="info",
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


def _meminfo() -> dict[str, int]:
    """/proc/meminfo in MiB (Linux only)."""
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) // 1024
    except (OSError, ValueError):
        pass
    return out


@app.command()
def doctor():
    """動作環境を診断する（メモリ・swap・SQLite 拡張・Ollama とモデル・索引）。"""
    import platform
    import sqlite3

    import sqlite_vec

    ok = True

    def line(status: str, msg: str):
        nonlocal ok
        mark = {"ok": "[green]OK[/]  ", "warn": "[yellow]WARN[/]", "ng": "[red]NG[/]  "}[status]
        ok = ok and status != "ng"
        console.print(f"{mark} {msg}", highlight=False)

    line("ok", f"Python {platform.python_version()} / {platform.system()} {platform.machine()}")
    mem = _meminfo()
    if mem:
        total, avail, swap = mem.get("MemTotal", 0), mem.get("MemAvailable", 0), mem.get("SwapTotal", 0)
        line("ok" if avail >= 2500 else "warn",
             f"メモリ 合計 {total} MiB / 利用可能 {avail} MiB（モデル読み込み前で 2500 MiB 以上が目安）")
        line("ok" if swap >= 1024 else "warn",
             f"swap {swap} MiB" + ("" if swap >= 1024 else "（2GB 程度の swap を推奨。README 参照）"))

    con = sqlite3.connect(":memory:")
    line("ok", f"SQLite {sqlite3.sqlite_version}")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        line("ok", "FTS5 使用可")
    except sqlite3.OperationalError:
        line("ng", "SQLite に FTS5 がありません")
    if hasattr(con, "enable_load_extension"):
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        line("ok", f"sqlite-vec {con.execute('select vec_version()').fetchone()[0]}")
    else:
        line("ng", "sqlite3 が拡張の読み込みに対応していません（Ubuntu 標準の python3 を使ってください）")

    line("ok", f"LLM API {config.LLM_API} @ {config.LLM_BASE_URL}  chat={config.CHAT_MODEL}  "
               f"embed={config.EMBED_MODEL}  num_ctx={config.NUM_CTX}")
    try:
        tags = llm.ollama_tags() if config.LLM_API == "ollama" else None
    except Exception as e:
        tags = None
        line("ng", f"Ollama に接続できません: {e}")
    if tags is not None:
        names = set(tags) | {t.removesuffix(":latest") for t in tags}
        for m in (config.CHAT_MODEL, config.EMBED_MODEL):
            if m in names:
                line("ok", f"モデル {m}")
            else:
                line("ng", f"モデル {m} がありません → ollama pull {m}")
        for m in llm.ollama_ps():
            line("ok", f"読み込み中: {m.get('name')} {m.get('size', 0) / 2**20:.0f} MiB "
                       f"(context {m.get('context_length', '?')})")

    if config.DB_PATH.exists():
        try:
            c = store.connect()
            n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            em = c.execute("SELECT value FROM meta WHERE key='embed_model'").fetchone()
            line("ok" if n else "warn", f"索引 {config.DB_PATH} {n} チャンク（embed={em[0] if em else '?'}）")
        except SystemExit as e:
            line("ng", str(e))
    else:
        line("warn", f"索引がまだありません: {config.DB_PATH} → imsearch ingest")
    raise typer.Exit(0 if ok else 1)


def main():
    try:
        app()
    except llm.LLMError as e:
        console.print(f"[red]{e}[/]", highlight=False)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
