# imsearch — INTER-Mediator 開発者向け検索（ローカル RAG・省メモリ版）

INTER-Mediator の日本語マニュアル（https://inter-mediator.info/ja/）、本体のソースコード、サンプルアプリ（INTER-Mediator_Samples）を情報源にして検索し、Ollama 上の LLM で回答を作ります。

これは testdev1 の imsearch（Mac・64GB 向け）と同じ機能・同じコマンドを、**メモリ 4GB・GPU なしの Ubuntu Linux** で動くように調整した版です。設計の全体像は [docs/DESIGN.md](docs/DESIGN.md)、Mac 版からの変更点は [docs/LOWMEM.md](docs/LOWMEM.md) を参照してください。

## 動作環境

| 項目 | 内容 |
|---|---|
| OS | Ubuntu 22.04 / 24.04（x86_64・arm64） |
| メモリ | 4GB（swap 2GB を推奨） |
| ディスク | 空き 5GB 程度（モデル 2.1GB、リポジトリ・索引 約 0.2GB） |
| Python | 3.10 以上（Ubuntu 標準の python3） |
| LLM | Ollama（CPU のみで可） |

既定のモデル:

| 用途 | モデル | サイズ | Mac 版 |
|---|---|---|---|
| 回答生成・翻訳 | `qwen3:1.7b` | 1.4GB | `qwen3:30b-a3b`（18GB） |
| 埋め込み | `qwen3-embedding:0.6b`（1024 次元・多言語） | 0.6GB | `bge-m3`（1.2GB） |

## セットアップ

```bash
git clone https://github.com/msyk/imsearch-linux.git && cd imsearch-linux
./scripts/setup-ubuntu.sh --swap --timer --web
```

スクリプトは次を行います（sudo を使います）。

1. `python3-venv` `git` `curl` を apt で導入
2. `--swap`: swap が無ければ 2GB の `/swapfile` を作成（`vm.swappiness=10`）
3. Ollama を公式インストーラーで導入し、4GB 向けの設定（[deploy/ollama-override.conf](deploy/ollama-override.conf)）を systemd に適用
4. モデル 2 つを `ollama pull`
5. `.venv` を作って imsearch をインストールし、`imsearch.env` を作成
6. `--timer`: 毎週日曜 3 時の索引更新（systemd timer）を登録
7. `--web`: Web アプリを systemd サービス（`imsearch-web`）として常駐
8. `imsearch doctor` で診断

LLM を別のサーバーで動かす場合は `--no-ollama` を付け、`imsearch.env` の `IMSEARCH_LLM_BASE_URL` などを設定します。

## 使い方

コマンドは Mac 版と同じです。

```bash
.venv/bin/imsearch ingest                 # 取り込み（2回目以降は差分のみ）
.venv/bin/imsearch ingest -s manual       # 情報源を指定して更新
.venv/bin/imsearch ingest --no-update     # ネットワークにアクセスせず、キャッシュから再索引
.venv/bin/imsearch ingest --no-describe   # サンプルの説明文を LLM で作らない（速い）

.venv/bin/imsearch search "repeat-control"            # 検索のみ（LLM を使わず高速）
.venv/bin/imsearch search "一覧と詳細の切り替え" -x     # LLM で検索語を補って検索（IMSEARCH_ANALYZE_MODE=json のとき）
.venv/bin/imsearch ask "検索ボックスで部分一致検索をしたい"   # 資料を根拠に回答
.venv/bin/imsearch ask "How do I send an email?"      # 英語：翻訳して検索・回答し、英訳して返す
.venv/bin/imsearch ask "..." --show-context --raw     # LLM に渡した資料も表示／逐次表示

.venv/bin/imsearch stats                  # 索引の件数
.venv/bin/imsearch eval                   # eval/questions.yaml で Recall@5 を計測
.venv/bin/imsearch doctor                 # 環境診断（メモリ・swap・SQLite・Ollama・モデル・索引）
```

## Web アプリ

```bash
.venv/bin/imsearch serve                  # http://127.0.0.1:8000/ で起動
.venv/bin/imsearch serve --host 0.0.0.0 --port 8080
```

ブラウザで質問を入力し、「回答を作成」（Ctrl/⌘+Enter）で資料を根拠にした回答を逐次表示します。「検索のみ」なら LLM を使わずに資料の一覧を約 1 秒で返します。英語の質問は、日本語に翻訳して検索・回答し、英訳して表示します（翻訳前の日本語の回答も見られます）。参考資料の番号 `[1]` から出典に飛べます。

| URL | 内容 |
|---|---|
| `GET /` | ブラウザ UI |
| `GET /api/search?q=…&n=10&source=manual` | 検索のみ（JSON） |
| `POST /api/ask` `{"q": "…", "sources": ["manual"]}` | 回答（Server-Sent Events: `status` `meta` `token` `ja_token` `done` `error`） |
| `GET /api/health` | 索引の件数・モデル・混雑状況 |
| `GET /api/docs` | API ドキュメント（OpenAPI） |

メモリ 4GB 向けに、回答の生成は同時に 1 件だけ行い、後続は 3 件まで順番待ちにします。それを超える要求には「混み合っています」と返します（`IMSEARCH_WEB_CONCURRENCY`、`IMSEARCH_WEB_MAX_QUEUE`）。ブラウザを閉じたり「中止」を押したりすると、Ollama 側の生成も止まります。

### インターネットに公開する

`imsearch serve` は既定で 127.0.0.1 だけで待ち受けます。公開するときは nginx などのリバースプロキシを前に置いてください。設定例は [deploy/nginx-imsearch.conf](deploy/nginx-imsearch.conf) です（SSE を逐次届けるための `proxy_buffering off`、長い生成に合わせたタイムアウト、IP ごとの回答リクエスト数の制限を含みます）。HTTPS は certbot などで設定します。

## 索引を別マシンで作る（推奨）

CPU だけのマシンでは、初回の取り込み（約 5,300 チャンクの埋め込みとサンプル説明文の生成）に数時間かかります（4 CPU で埋め込みだけで約 3 時間。Mac M2 Max なら一式で約 17 分）。GPU のあるマシン（Apple Silicon の Mac など）で**同じ埋め込みモデル**を使って索引を作り、ファイルをコピーすると早く済みます。

```bash
# Mac 側（https://github.com/msyk/imsearch-linux を clone して）
ollama pull qwen3-embedding:0.6b && ollama pull qwen3:1.7b
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/imsearch ingest

# Linux へコピー（索引とクロール・clone のキャッシュ）
rsync -a data/ server:imsearch-linux/data/
```

索引には作成時の埋め込みモデル名が記録され、設定と食い違うとエラーで止まります（別モデルのベクトルが混ざるのを防ぐため）。サンプルの説明文は「モデル名＋プロンプト」をキーに索引内へキャッシュされるので、同じ `IMSEARCH_CHAT_MODEL` なら Linux 側の差分更新で作り直しは起きません。コピー後の差分更新は Linux 側の `ingest` やタイマーで行えます（変更分だけ埋め込むので軽い）。

## メモリ 4GB での運用

おおよその内訳（Ollama の設定は [deploy/ollama-override.conf](deploy/ollama-override.conf)）:

| 項目 | 目安 |
|---|---|
| OS・sshd など | 0.3〜0.5GB |
| `qwen3:1.7b`（重み＋KV キャッシュ 8K/q8_0。`ollama ps` で 2.0GB） | 約 2.0GB |
| `qwen3-embedding:0.6b`（`ollama ps` で 0.9GB） | 約 0.9GB |
| imsearch（Python・SQLite） | 0.1〜0.2GB |

4GB に制限した Ubuntu 24.04 コンテナで、両モデルを載せた状態の `ask` がピーク 2.8GB で動くことを確認しています（[docs/LOWMEM.md](docs/LOWMEM.md) §5）。回答には CPU で 1.5〜2.5 分かかります。検索だけ（`search`）なら約 1 秒です。

足りないときの対処（上から順に効果が大きい）:

1. swap を 2GB 付ける（`setup-ubuntu.sh --swap`）。モデルの一時的な読み込みピークを吸収します。
2. `imsearch.env` で `IMSEARCH_NUM_CTX=4096`、`IMSEARCH_CONTEXT_CHARS=3500` に下げる。
3. Ollama の `OLLAMA_MAX_LOADED_MODELS=1` にする（モデルを同時に 1 つだけ置く。切り替えの数秒が毎回かかる）。
4. `IMSEARCH_KEEP_ALIVE=0` にする（使い終わったらすぐ解放）。

逆に余裕がある（swap 込みで 6GB 以上、あるいは 8GB 機）なら、回答品質を上げるために `IMSEARCH_CHAT_MODEL=qwen3:4b`（約 2.5GB）を使えます。LLM を GPU サーバーや外部 API に置く場合は次の変数で切り替えます。

| 変数 | 既定値 |
|---|---|
| `IMSEARCH_LLM_API` | `ollama`（`openai` で OpenAI 互換 API） |
| `IMSEARCH_LLM_BASE_URL` | `http://localhost:11434` |
| `IMSEARCH_CHAT_MODEL` | `qwen3:1.7b` |
| `IMSEARCH_EMBED_MODEL` | `qwen3-embedding:0.6b` |
| `IMSEARCH_NUM_CTX` | `8192` |
| `IMSEARCH_CONTEXT_CHUNKS` / `IMSEARCH_CONTEXT_CHARS` | `5` / `7000` |
| `IMSEARCH_PREREQ_KEYS` / `IMSEARCH_PREREQ_CHARS` | `4` / `2000`（前提の設定の説明を加える数と文字数。`0` で無効） |
| `IMSEARCH_ANALYZE_JA` | `0`（`1` で日本語の質問にも LLM のクエリ解析を使う） |
| `IMSEARCH_ANALYZE_MODE` | `simple`（`json` で Mac 版と同じ JSON 解析。大きいモデル向け） |
| `IMSEARCH_NUM_THREAD` | `0`（自動。コンテナ等の CPU 制限を検出） |
| `IMSEARCH_WEB_HOST` / `IMSEARCH_WEB_PORT` | `127.0.0.1` / `8000` |
| `IMSEARCH_WEB_CONCURRENCY` / `IMSEARCH_WEB_MAX_QUEUE` | `1` / `3` |
| `IMSEARCH_DB` | `data/imsearch.db` |

全項目は [imsearch.env.example](imsearch.env.example) にあります。

## 設定ファイル

| ファイル | 内容 |
|---|---|
| `imsearch.env` | 上記の環境設定（`imsearch.env.example` をコピー） |
| `config/exclude.yaml` | 除外するマニュアルページ（旧バージョン）、ページタイトルの上書き、サンプル・コードの対象範囲 |
| `config/glossary.yaml` | 日英用語集（英語の質問の翻訳と回答の英訳、日本語の質問への英語の検索語の追加に使う） |
| `eval/questions.yaml` | 検索精度の評価セット |

## 構成

```
imsearch/
  crawl.py          マニュアルのクロール（ETag による差分取得、旧版ページの警告）
  parse_html.py     マニュアル → 見出し単位チャンク／定義ファイルのキー／API 項目
  parse_code.py     tree-sitter で PHP/JS をクラス・メソッド単位に分割
  parse_samples.py  サンプル → 概要（LLM 説明付き）／ページファイル／定義ファイル／スキーマ
  store.py          SQLite（FTS5 + sqlite-vec）と差分同期
  retrieve.py       BM25（日本語は bi-gram）＋識別子完全一致＋ベクトル検索を RRF で統合
  answer.py         クエリ解析・翻訳・回答生成
  llm.py            Ollama / OpenAI 互換 API クライアント（メモリ不足時の案内付き）
  cli.py            コマンドライン（doctor, serve を追加）
  web.py            Web アプリ・HTTP API（FastAPI、回答は SSE で逐次送信）
  static/index.html ブラウザ UI
deploy/
  ollama-override.conf     Ollama の 4GB 向け systemd 設定
  imsearch-ingest.service  週 1 回の索引更新（timer から起動）
  imsearch-ingest.timer
  imsearch-web.service     Web アプリの常駐
  nginx-imsearch.conf      nginx で公開する場合の設定例
scripts/setup-ubuntu.sh    Ubuntu 用セットアップ
```

## ライセンス

[MIT License](LICENSE)

このリポジトリには索引やクロール結果は含みません（`imsearch ingest` で各自の環境に作成します）。取り込み対象の [INTER-Mediator](https://github.com/INTER-Mediator/INTER-Mediator) と [INTER-Mediator_Samples](https://github.com/INTER-Mediator/INTER-Mediator_Samples) は MIT License、マニュアル（https://inter-mediator.info/ja/）の権利はその著作者に帰属します。作成した索引を配布・公開する場合は、それぞれの表記に従ってください。
