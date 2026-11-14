# Linux・メモリ 4GB 版での変更点

作成日: 2026-11-14

[DESIGN.md](DESIGN.md) は Mac（Apple M2 Max / 64GB）向けに書かれた設計書です。本書は、同じ機能を **Ubuntu Linux・メモリ 4GB・GPU なし** で動かすために変えた点をまとめます。機能（情報源、チャンク分割、ハイブリッド検索、回答生成、英語対応、評価）とコマンドは変えていません。

## 1. 前提環境（DESIGN §2 の置き換え）

| 項目 | Mac 版 | 本版 |
|---|---|---|
| マシン | M2 Max / 64GB / GPU | Ubuntu 22.04・24.04 / 4GB / CPU のみ |
| Python | Homebrew の 3.12 | Ubuntu 標準の python3（3.10 以上） |
| LLM 実行 | Ollama（Metal） | Ollama（CPU、systemd サービス） |
| 定期更新 | launchd | systemd timer |

## 2. モデル（DESIGN §7 の置き換え）

| 用途 | Mac 版 | 本版 | 理由 |
|---|---|---|---|
| 回答生成・翻訳 | `qwen3:30b-a3b`（18GB） | `qwen3:1.7b`（1.4GB） | 4GB に収まり、日本語が使える最大級。余裕があれば `qwen3:4b` |
| 埋め込み | `bge-m3`（1.2GB, F16） | `qwen3-embedding:0.6b`（0.6GB, Q8） | 同じ 1024 次元・多言語で、サイズが約半分 |

- 埋め込みモデルが違うので、**Mac 版の索引（bge-m3 製）はそのままでは使えません**。索引は本版の設定で作り直します（別マシンで作ってコピーする手順は README 参照）。
- `qwen3-embedding` は質問側にだけ指示文（Instruct: … Query: …）を付けると精度が上がるので、検索時のみ自動で付けます（`IMSEARCH_EMBED_QUERY_PREFIX`）。

## 3. メモリを抑えるための調整

| 箇所 | Mac 版 | 本版 | 効果 |
|---|---|---|---|
| 回答生成の `num_ctx` | 32768 | 8192（`IMSEARCH_NUM_CTX`） | KV キャッシュが 1/4 |
| KV キャッシュの型 | f16 | q8_0（`OLLAMA_KV_CACHE_TYPE`、flash attention 併用） | さらに 1/2 |
| 並列リクエスト | 既定 | `OLLAMA_NUM_PARALLEL=1` | 並列数ぶんの KV キャッシュを確保しない |
| 同時に載せるモデル | 既定 | `OLLAMA_MAX_LOADED_MODELS=2`（回答用＋埋め込み用） | 切り替えの再読み込みを避けつつ上限を明示 |
| LLM に渡す資料 | 8 件・16,000 字 | 5 件・7,000 字（`IMSEARCH_CONTEXT_CHUNKS/CHARS`） | 8K コンテキストに収める |
| 埋め込みのバッチ | 32 件 | 8 件・1 件 2,000 字まで・`num_ctx` 2048 | 取り込み時のピークを抑える |
| サンプル説明文の生成 | ページ 3,000 字を入力 | 1,500 字（`IMSEARCH_DESCRIBE_PAGE_CHARS`） | CPU での生成時間とメモリを削減 |
| SQLite | 既定 | `cache_size` 16MB、`mmap_size=0`、`temp_store=FILE` | Python プロセスの常駐量を抑える |
| git | `clone --depth 1` + `pull` | `clone --depth 1 --single-branch` + `fetch --depth 1` + `reset` | 浅い clone のまま更新し、履歴を溜めない |
| Ollama のメモリ上限 | なし | systemd で `MemoryHigh=3000M` / `MemoryMax=3500M`、`OOMScoreAdjust=500` | メモリ不足時に sshd などではなく Ollama 側が止まる |

## 4. 小型モデル向けのプロンプト調整

1.7B 程度のモデルは Mac 版のプロンプトをそのまま使うと次のように失敗したため、作りを変えました。

| 処理 | Mac 版 | 小型モデルでの失敗 | 本版 |
|---|---|---|---|
| 英語の質問の翻訳 | 1 回の JSON 出力で「日本語の検索文・重要語・識別子の候補」を出させる | 翻訳せず英語のまま返し、存在しない API 名（`INTERMediator.constructMain` など）を識別子として出す | 英語のシステムプロンプト＋例 1 つで「翻訳だけ」をさせる。識別子は質問文から正規表現で抽出（`IMSEARCH_ANALYZE_MODE=simple`） |
| 回答・検索結果の英訳 | 指示と本文を 1 つのユーザー発言に入れる | 指示文そのものを出力する | 指示はシステムプロンプト、本文はユーザー発言に分け、例を 1 つ付ける |
| 識別子だけの検索（`repeat-control` など） | 英語と判定して翻訳・英訳する | （小型モデルでは数分かかる） | 識別子だけ・1 語だけの質問は翻訳しない |

大きいモデルや外部 API を使うときは `IMSEARCH_ANALYZE_MODE=json` で Mac 版と同じ解析に戻せます（`search -x` の検索語補完もこのモードで効きます）。

評価セット（`eval/questions.yaml`、14 問）の Recall@5:

| 構成 | Recall@5 |
|---|---|
| Mac 版（bge-m3 ＋ qwen3:30b-a3b） | 11/14（79%） |
| 本版（qwen3-embedding:0.6b ＋ qwen3:1.7b） | 12/14（86%） |

検索の精度は落ちていません。一方、回答文の質は 30B モデルより明らかに下がります（資料にない記述を混ぜることがある）。回答には必ず出典が付くので、原典で確認してください。品質が必要なら `qwen3:4b`、あるいは別サーバーの大きいモデルを使います。

## 5. 速度とメモリ（実測）

メモリ 4GB・4 CPU に制限した Ubuntu 24.04 コンテナ（arm64、Apple M2 Max 上）で測定。ホストが高負荷の状態での値なので、目安として見てください。

| 処理 | 時間 | メモリ |
|---|---|---|
| `search`（モデル読み込み済み） | 約 1 秒 | — |
| `search`（埋め込みモデルの読み込み込み） | 約 4 秒 | — |
| `ask`（日本語） | 1.5〜2.5 分 | コンテナ全体のピーク 2.8GB（Ollama の 2 モデル＋Python） |
| `ask`（英語、翻訳 2 回を含む） | 約 2 分 | 同上 |
| 差分更新（変更なし） | 約 3 秒 | Python 81MB |
| 初回の埋め込み（5,325 チャンク） | 約 3 時間（2 秒/チャンク） | — |
| Mac（M2 Max）での初回取り込み一式 | 約 17 分 | — |

- 回答生成の速度は 4 CPU で約 26 トークン/秒、入力の読み込みは約 70 トークン/秒でした。
- **CPU 制限のあるコンテナや VM では、Ollama がホストの全コア数ぶんのスレッドを立てて 10 倍以上遅くなる**ことがあります（実測 1.5 トークン/秒）。本版は cgroup の CPU 制限を検出して `num_thread` を自動で合わせます（`IMSEARCH_NUM_THREAD` で手動指定も可）。
- 初回の索引は GPU のあるマシンで作ってコピーする運用を推奨します（README「索引を別マシンで作る」）。コピー後の差分更新は Linux 側で問題なく動きます（実測で確認済み）。
- 日本語の質問では LLM によるクエリ解析を既定で省きます（`IMSEARCH_ANALYZE_JA=0`）。

## 6. 追加・修正したもの

- `imsearch doctor`: メモリ・swap・SQLite（FTS5／拡張の読み込み）・sqlite-vec・Ollama 接続・モデルの有無・読み込み中のモデルと使用量・索引の状態を表示。
- `imsearch.env`: 環境変数をファイルで設定（systemd から起動するときも同じ設定を使える）。
- LLM のエラー表示: Ollama がメモリ不足やモデル未取得で失敗したとき、対処方法を表示。接続できない・タイムアウトの場合も分かりやすく表示。
- `scripts/setup-ubuntu.sh`、`deploy/`（Ollama 設定、索引更新の systemd service/timer）。
- CPU スレッド数の自動調整（§5）。
- Web アプリ（DESIGN §8 Phase 2）: `imsearch serve`。FastAPI ＋ 外部ライブラリに依存しない 1 枚の HTML。回答は Server-Sent Events で逐次表示。4GB 向けに回答の生成を同時 1 件に制限して短い順番待ちを設け、クライアントが切断したら Ollama への接続を閉じて生成を止める。systemd サービス（`deploy/imsearch-web.service`）と nginx の設定例を同梱。
- マニュアルのキャッシュ一覧（`data/manual/_pages.json`）を絶対パスからファイル名に変更。Mac 版では索引やキャッシュを別マシンにコピーすると取り込みが失敗していた。
