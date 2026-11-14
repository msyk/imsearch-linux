# INTER-Mediator 開発者向け検索システム 設計書（v0.1）

作成日: 2026-11-14

> **本リポジトリ（Linux・メモリ 4GB 版）について**: 本書は Mac 版の設計書です。モデル・メモリ設定・運用（§2, §5.5, §7）は [LOWMEM.md](LOWMEM.md) の内容で置き換えています。

## 1. 目的とスコープ

INTER-Mediator でアプリケーションを開発する人が、自然言語で「やりたいこと」を問い合わせると、

- 書くべき記述（定義ファイルのキー、ページファイルの `data-im` 属性など）
- 使うべき API（`INTERMediator.*`、`IMLibContextPool.*`、サーバー側 PHP クラス等）
- 根拠となるマニュアルのページ／ソースコードの箇所（リンク付き）

を返すローカル完結型の検索システム（RAG: Retrieval-Augmented Generation）を作る。

**非スコープ（初期）**: アプリケーションコードの自動生成、公開運用。ただし**将来の一般公開を前提に設計する**（§13）。

### 想定する問い合わせ例

| 種別 | 例 |
|---|---|
| 目的→記述 | 「一覧から詳細ページに遷移するには？」「検索ボックスで部分一致検索したい」 |
| キー／API の意味 | 「`repeat-control` に指定できる値は？」「`doAfterConstruct` はいつ呼ばれる？」 |
| 内部動作 | 「認証でパスワードはどこでハッシュ化されている？」 |
| 移行・バージョン差 | 「Ver.5 から Ver.6 への移行で変わった点は？」 |

## 2. 前提環境

| 項目 | 値 |
|---|---|
| マシン | Apple M2 Max / 64GB / 38コアGPU |
| ディスク空き | 約 73GB（モデル選定の制約） |
| LLM 実行 | Ollama（ローカル、`http://localhost:11434`） |
| 言語 | Python 3.12 + uv |

## 3. 情報源の調査結果

### 3.1 マニュアルサイト（https://inter-mediator.info）

- トップは `/ja/` と `/en/` への振り分けのみ。各言語トップに全ページへのリンクがある。
- **索引対象は `/ja/` のみ（決定）**。英語版はページ数・情報量が少ないため取り込まない。英語での利用は §6.1 の翻訳で対応する。
- `sitemap.xml` は **存在しない**（404）→ 言語トップからのリンク巡回でクロールする。
- 静的 HTML。`<h1 id="pageTitle">` がページタイトル、本文は `h1`〜`h4` で階層化。
- カテゴリ（URL パス）:
  - `for-novices/` 入門・導入（startup, trial, deploy, vm, handson …）
  - `for-developers/` 定義ファイル・ページファイル（**reference.html**: 定義ファイルのキー一覧、表が 28 個、deffile, format, lookup, db, localizing …）
  - `for-programmers/` JavaScript/PHP API（**api.html**: `h4` に API シグネチャ、serverextending, jscomponent, media …）
  - `practices/` 目的別の実践例（masterdetail, search, step, tips …）
  - `tutorial/` チュートリアル（proof-01〜09 など。epub は対象外）
- バージョン違いのページが併存している。**旧バージョン向けのページは索引に含めない（決定）**。除外リストは `config/exclude.yaml` で管理する。

  | ページ | リンク文言 | 扱い |
  |---|---|---|
  | `for-novices/deploy.html` | ダウンロードとインストール(Ver.5まで) | 除外 |
  | `for-novices/deploy6.html` | 同(Ver.6以降) | 除外（`deploy16` が後継） |
  | `for-novices/deploy16.html` | 同(Ver.16以降) | **対象** |
  | `for-developers/sendmail.html` | メール送信(Ver.5) | 除外 |
  | `for-developers/sendmail6.html` | メール送信(Ver.6以降) | 除外（暫定。§12 で確認） |
  | `for-developers/sendmail10.html` | メール送信(Ver.10) | **対象** |
  | `for-developers/sharing5.html` | 即時更新(Ver.5) | 除外 |
  | `for-developers/sharing7.html` | 即時更新(Ver.7以降) | **対象** |
  | `for-programmers/serverextending53.html` | サーバーサイドの拡張（Ver.5.3まで） | 除外 |
  | `for-programmers/serverextending54.html` | 同（Ver.5.4以降のVer.5） | 除外 |
  | `for-programmers/serverextending.html` | 同（Ver.6.0以降） | **対象** |
  | `tutorial_v5/` 配下 | 旧チュートリアル | 除外 |

  - 新しく追加されたページは、リンク文言の `(Ver.N まで)` `(Ver.5)` などのパターンで旧版候補を検出し、クロール時に警告を出す（自動除外はせず、作者が除外リストに追記）。
  - `for-developers/migration.html`（移行ガイド）は現行版への移行情報なので対象に含める。
  - ページ内に部分的に残る旧バージョンの記述は、初期段階では除去しない（評価で問題が出たら対処）。

### 3.2 ソースコード（https://github.com/INTER-Mediator/INTER-Mediator）

- 既定ブランチ `master`、主言語 PHP、約 38MB。
- 主な構成:
  - `src/php/`（約130ファイル）: サーバー側。`Auth/`, `DB/`, `Data_Converter/`, `Media/`, `Messaging/` …
  - `src/js/`（約17ファイル）: クライアント側ライブラリ
  - `dist-docs/`: 配布用ドキュメント・設定サンプル。**`prompts/prompt*.{ja,en}.md` は AI 向けに INTER-Mediator の使い方を凝縮した高価値文書**
  - `spec/`: ユニットテスト・E2E（`spec/run` に約120の JS）→ **使用例**として価値あり（優先度は低め）
  - `editors/`, `themes/`: 対象外（または低優先）

### 3.3 サンプルアプリ（https://github.com/INTER-Mediator/INTER-Mediator_Samples）

- 既定ブランチ `main`、456 ファイル（HTML 199、PHP 134、YAML 35、JS 24、SQL 9）。
- **ディレクトリ 1 つが 1 つのサンプルアプリ**で、ページファイル（`data-im` 属性付き HTML）＋定義ファイル（PHP の `IM_Entry(...)` または YAML）＋必要に応じて JS で構成される。
- 「目的 → 書くべき記述」の実例として、マニュアルを補う最重要の情報源と位置付ける。

| 区分 | ディレクトリ | 扱い |
|---|---|---|
| 機能別サンプル | `Sample_search`, `Sample_form`, `Sample_invoice`, `Sample_pdf`, `Sample_Auth`, `Sample_products`, `Sample_realtime`, `Sample_Mobile`, `Sample_Extensible`, `Sample_API`, `Sample_webpage`, `Sample_chat` など | **高優先** |
| 実践・ハンズオン | `Practices/`（FileMaker Data API 含む）, `Hands-on/`, `Demo-Script/` | **高優先**（マニュアルの practices/ と対応） |
| 認証・メール・ログ | `Auth_Support/`（Passkey, PasswordReset, User_Enrollment）, `Mail_Support`, `Log_Support`, `Accounts` | **高優先** |
| DB スキーマ | `lib/MySQL_Schema`, `lib/PostgreSQL_Schema` | 対象（`kind=schema`。サンプルが使うテーブル定義の参照用） |
| テスト用ページ | `E2E-Test/`, `Sample_forDebugging/` | 低優先（`kind=example`） |
| その他 | 画像、CSS、`composer.lock`, `saml-config/` | 除外 |

## 4. アーキテクチャ

```
                ┌──────────────── 取り込み（バッチ） ────────────────┐
 inter-mediator.info ─▶ Crawler ─▶ HTML Parser ─▶ 見出し単位チャンク ─┐
 GitHub repo (git clone) ─▶ Code Parser (tree-sitter) ─▶ 関数/クラス単位 ─┤
 Samples repo (git clone) ─▶ Sample Parser ─▶ サンプル単位＋ファイル単位 ─┤
                                                                        ▼
                                              Ollama Embedding (bge-m3)
                                                                        ▼
                                         SQLite: chunks + FTS5 + sqlite-vec
                └───────────────────────────────────────────────────┘
                                                                        ▲
 利用者 ─▶ CLI / Web UI / MCP ─▶ Query Engine ─────────────────────────┘
                                  │ 1. 言語判定・英語なら日本語へ翻訳、API名候補の抽出
                                  │ 2. ハイブリッド検索（BM25 + ベクトル → RRF統合）
                                  │ 3. 再ランキング（LLMによる絞り込み、任意）
                                  └ 4. 回答生成（日本語で生成 → 英語の質問なら英訳、出典リンク必須）
```

### 4.1 コンポーネント一覧

| コンポーネント | 役割 | 技術 |
|---|---|---|
| Crawler | `/ja/` 配下を巡回取得、ETag/ハッシュで差分検出 | httpx |
| HTML Parser | ナビ等を除去し、見出し階層・表・コードを保持して Markdown 化 | selectolax / BeautifulSoup + markdownify |
| Code Parser | PHP/JS をクラス・メソッド・関数単位に分割、PHPDoc/JSDoc 抽出 | tree-sitter (php, javascript) |
| Sample Parser | サンプル単位で、ページファイルと定義ファイルの対応付け、`data-im` 属性と定義キーの抽出 | selectolax + tree-sitter + PyYAML |
| Embedder | チャンクをベクトル化 | Ollama `bge-m3`（多言語・日英対応） |
| Store | 本文・メタデータ・全文索引・ベクトルを 1 ファイルで管理 | SQLite + FTS5（trigram）+ sqlite-vec |
| Query Engine | 検索・統合・回答生成 | Python |
| UI | CLI → Web UI → MCP サーバー | Typer / FastAPI + 素の HTML / MCP Python SDK |

**選定理由**: SQLite 一本にすると、外部サービス不要・バックアップはファイルコピー・FTS とベクトルを同一トランザクションで更新できる。データ量（数千〜1万チャンク程度の見込み）なら性能は十分。

## 5. 取り込み（Ingestion）設計

### 5.1 マニュアルのチャンク分割

- 単位: **見出し（h1〜h4）ごとのセクション**。各チャンクの先頭に見出しパス（`ページ名 > h1 > h2`）を付与して文脈を保持する。
- 目安 300〜800 トークン。長すぎるセクションは段落境界で分割し、前後 1 段落を重ねる。
- **reference.html の表**: 定義ファイルのキー 1 行 = 1 チャンクとして別途登録（`kind=definition_key`）。キー名は完全一致検索の対象にする。
- **api.html の `h4`**: API シグネチャ 1 つ = 1 チャンク（`kind=api`、`symbol=INTERMediator.construct`）。
- コードブロック・表は Markdown 化して保持（LLM が書き方を再現できるように）。

### 5.2 ソースコードのチャンク分割

- 単位: PHP はクラス／メソッド、JS は関数／オブジェクトのメソッド。docblock を含める。
- 長大なメソッドはシグネチャ + docblock + 先頭部分を代表チャンクにし、全体は分割して保持。
- 対象: `src/php/**`, `src/js/**`, `dist-docs/**/*.md`, `README.md`。`spec/**` は `kind=example` として低優先で登録。
- 除外: `vendor/`, `node_modules/`, 画像、lock ファイル、`themes/`。
- リンクはコミット SHA 固定の GitHub パーマリンク（`blob/<sha>/path#L10-L42`）。

### 5.3 サンプルアプリのチャンク分割

- **サンプル概要チャンク**（`kind=sample`）: サンプル 1 つにつき 1 件。ディレクトリ名、含まれるファイル一覧、使われている定義キー・`data-im` 属性・API 名の一覧、取り込み時に LLM で生成した日本語の 2〜3 行の説明（「何を実現しているサンプルか」）を持つ。目的ベースの質問はまずここにヒットさせる。
- **ページファイル**（`kind=sample_page`）: HTML から `<head>` の定型部分やスタイルを除き、`data-im` / `data-im-control` 等を持つ要素とその周辺構造を残して Markdown のコードブロック化。
- **定義ファイル**（`kind=sample_def`）: `IM_Entry(...)` の配列（または YAML）をコンテキスト単位に分割。各コンテキストの `name`, `table`, `key`, `query`, `relation` などを `symbol` に入れる。
- **JS / PHP 拡張**（`kind=sample_code`）: §5.2 と同じくコード単位。
- ページファイルと定義ファイルの対応は、ページ内の `<script src="...php">` などの参照から解決し、`sample_id` で相互に辿れるようにする（回答時に「ページ側の記述」と「定義ファイル側の記述」をセットで提示するため）。

### 5.4 チャンクのスキーマ

```sql
CREATE TABLE chunks (
  id          INTEGER PRIMARY KEY,
  source      TEXT,    -- 'manual' | 'code' | 'doc' | 'samples'
  kind        TEXT,    -- 'section' | 'definition_key' | 'api' | 'function' | 'class' | 'example'
                       -- | 'sample' | 'sample_page' | 'sample_def' | 'sample_code' | 'schema'
  sample_id   TEXT,    -- サンプルアプリのディレクトリ名（samples のみ）
  lang        TEXT,    -- 'ja'（マニュアル・日本語文書） | 'php' | 'js' | 'md'
  title       TEXT,    -- 見出しパス or 完全修飾名
  symbol      TEXT,    -- キー名・API名・メソッド名（完全一致用）
  url         TEXT,    -- マニュアルURL#アンカー or GitHubパーマリンク
  commit_sha  TEXT,    -- コードの場合の取り込み元コミット
  content     TEXT,    -- Markdown 化した本文
  content_hash TEXT,   -- 差分更新用
  updated_at  TEXT
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(title, symbol, content, tokenize='trigram');
CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[1024]);  -- bge-m3
```

### 5.5 更新

- マニュアル: 週 1 回の再クロール。`content_hash` が変わったチャンクだけ再埋め込み。
- コード・サンプル: 両リポジトリを `git pull` 後、変更ファイルのみ再解析（サンプル概要チャンクは、そのサンプル内に変更があれば再生成）。
- 実行: `launchd`（または手動 `imsearch ingest`）。

## 6. 検索・回答設計

1. **クエリ整形**: 質問の言語を判定し、英語なら §6.1 の手順で日本語に翻訳する。あわせて LLM で API 名・定義ファイルのキー名の候補（英字の識別子）を抽出し、コード検索と `symbol` 完全一致に使う。
2. **ハイブリッド検索**:
   - キーワード: FTS5（trigram なので日本語も分かち書き不要）＋ `symbol` 完全一致ブースト
   - ベクトル: bge-m3 で上位 30 件
   - **RRF（Reciprocal Rank Fusion）** で統合し上位 20 件
3. **再ランキング（任意）**: LLM に上位 20 件を判定させ 6〜8 件に絞る。精度と速度を見て有効化を判断。
4. **回答生成**: システムプロンプトで「出典のチャンクのみ根拠とする」「該当がなければそう答える」「出典 URL を必ず列挙」を指示。索引は現行バージョンの情報のみなので、バージョンの選別は行わない。
5. **検索のみモード**: LLM を使わず、ヒットしたチャンクの一覧（タイトル・抜粋・URL）を即時返す。高速で、回答の正しさの確認にも使える。

### 6.1 英語での利用（翻訳方式）

索引は日本語のみとし、英語の質問には次の流れで対応する。

```
英語の質問 ─▶ 言語判定 ─▶ 日本語へ翻訳（検索キーワード化）─▶ 通常の検索・回答（日本語）─▶ 英語へ逆翻訳 ─▶ 回答
```

- **キーワード翻訳**: LLM で「日本語の検索文」と「識別子（API名・キー名）」に分けて出力させる。識別子は翻訳せずそのまま使う（例: "How do I use repeat-control?" → 「repeat-control の使い方」＋ `repeat-control`）。
- **用語集（glossary）**: INTER-Mediator 固有の用語の日英対応表（`config/glossary.yaml`。例: 定義ファイル ⇔ definition file、ページファイル ⇔ page file、コンテキスト ⇔ context）を翻訳プロンプトに入れ、訳語の揺れを防ぐ。作者が随時追記する。
- **逆翻訳**: 日本語で生成した回答を英訳する。コードブロック・識別子・URL は翻訳しないよう指示する。出典リンクは日本語マニュアルのままとし、「(Japanese)」と表記する。
- **検索のみモード**: ヒットしたチャンクのタイトルと抜粋だけを英訳して返す。
- **補足**: 埋め込みモデル `bge-m3` は多言語対応のため、英語のままでもベクトル検索はある程度効く。ただしキーワード検索（FTS5）は日本語本文に対して行うので、翻訳は必須とする。
- 翻訳には回答生成と同じ LLM を使う（モデルを増やさずメモリとディスクを節約）。

## 7. モデル選定（Ollama）

| 用途 | 第一候補 | サイズ目安 | 備考 |
|---|---|---|---|
| 埋め込み | `bge-m3` | 約1.2GB | 多言語・1024次元。日英混在に強い |
| 回答生成 | `qwen3:30b-a3b`（MoE） | 約19GB | 日本語が自然・高速。コード理解も良好 |
| 代替 | `gpt-oss:20b` | 約14GB | 推論力重視の比較用 |

※ 実装時点の Ollama ライブラリで最新版を確認し、評価セット（§9）で比較して決定する。

## 8. インターフェース

| 段階 | 形態 | 内容 |
|---|---|---|
| Phase 1 | CLI | `imsearch ingest` / `imsearch search "..."` / `imsearch ask "..."` |
| Phase 2 | Web UI | FastAPI。質問欄・回答・出典カード。ストリーミング表示 |
| Phase 3 | MCP サーバー | Claude Code / エディタの AI から `search_im_docs` ツールとして呼べるようにする（開発中に最も使われる形態と想定） |

## 9. 評価

- 開発者（作者）が **評価用の質問と正解ページの組を 30〜50 件** 作成（`eval/questions.yaml`）。
- 指標: 検索の Recall@5（正解ページが上位5件に入る率）、回答の正誤（人手で ○/△/×）。
- チャンク分割やモデルを変更するたびに再評価する。

## 10. ディレクトリ構成（案）

```
testdev1/
├── pyproject.toml
├── docs/DESIGN.md
├── imsearch/
│   ├── crawl.py          # マニュアル取得
│   ├── parse_html.py     # HTML→チャンク
│   ├── parse_code.py     # tree-sitter でコード→チャンク
│   ├── parse_samples.py  # サンプルアプリ→チャンク
│   ├── store.py          # SQLite / FTS5 / sqlite-vec
│   ├── embed.py          # Ollama 埋め込み
│   ├── retrieve.py       # ハイブリッド検索 + RRF
│   ├── answer.py         # 回答生成（プロンプト）
│   ├── cli.py
│   ├── web.py            # Phase 2
│   └── mcp_server.py     # Phase 3
├── config/
│   ├── exclude.yaml      # 旧バージョン等の除外ページ
│   └── glossary.yaml     # 日英用語集（翻訳用）
├── eval/questions.yaml
└── data/                 # git管理外: クロール結果・clone・imsearch.db
```

## 11. 実装フェーズ

1. **Phase 1（土台）**: Ollama 導入、クロール＋パース＋索引、CLI での検索・回答、評価セット 10 件で手応え確認
2. **Phase 2**: Web UI、差分更新、評価セット拡充と再ランキングのチューニング
3. **Phase 3**: MCP サーバー化、定期更新（launchd）

## 12. 確認したい事項

1. ~~対象言語~~ → **決定（2026-11-14）**: 日本語のみ索引。英語は質問の翻訳と回答の逆翻訳で対応（§6.1）
2. ~~旧バージョンのページ~~ → **決定（2026-11-14）**: 含めない（§3.1 の除外リスト）
   - 残る確認: `sendmail6.html`（Ver.6以降）は `sendmail10.html`（Ver.10）で置き換えられたと見なして除外してよいか。Ver.6〜9 の利用者向けに残す必要があれば対象に戻す
3. ~~サンプルアプリ~~ → **決定（2026-11-14）**: INTER-Mediator_Samples を検索対象に含める（§3.3, §5.3）。本体の `spec/` は低優先で含める
4. ~~利用者の範囲~~ → **決定（2026-11-14）**: 将来は一般公開も視野に入れる（§13）

## 13. 公開を見据えた設計方針

初期は作者のMac上で動かすが、後から公開できるよう以下を守る。

1. **層を分ける**: 取り込み・検索・回答のコアは UI から独立したライブラリにし、外部には HTTP API（`/search`, `/ask`）として出す。Web UI と MCP サーバーはこの API の利用者にする。
2. **LLM 呼び出しの抽象化**: Ollama 専用にせず、OpenAI 互換 API のインターフェースで呼ぶ。公開時は、GPU サーバー上の Ollama/vLLM や外部の LLM API に設定だけで切り替えられるようにする。
3. **索引の可搬性**: 索引は SQLite 1 ファイルで、埋め込みモデル（`bge-m3`）とそのバージョンをファイル内に記録する。これにより次の 2 つの公開形態を選べる。
   - **サーバー型**: Web 上で検索・回答を提供する
   - **配布型**: 索引ファイルを GitHub Releases 等で配布し、利用者が自分の Ollama と MCP サーバーで使う（サーバー費用ゼロ。開発者向けとしては有力）
4. **公開時に追加するもの**（初期は実装しない）: レート制限、質問ログの扱い（個人情報を保存しない／保存期間）、悪用対策（無関係な質問や長大な入力の拒否）、応答キャッシュ。
5. **出典の明示**: 回答には必ずマニュアル・GitHub の URL を付け、生成内容が誤っていても利用者が原典を確認できるようにする（公開時の信頼性確保）。
6. **ライセンス表記**: 索引に含むソースコード・サンプルのライセンス（リポジトリの `LICENSE`）とマニュアルの権利表記を、公開時の UI と配布物に明記する。
