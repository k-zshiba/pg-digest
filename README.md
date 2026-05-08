# pg-digest

Hacker News・GitHub・PostgreSQLメーリングリストをもとに、毎日自動生成されるPostgreSQLニュースダイジェストです。日本語と英語の両言語に対応しています。

## 概要

`scripts/generate_postgresql_digest.py` を実行すると、以下の3つの情報源から当日分のダイジェストを生成します。

- **Hacker News** — PostgreSQL関連ストーリーをAlgolia APIで取得
- **GitHub** — `postgres/postgres` リポジトリの当日コミット
- **PostgreSQLメーリングリスト** — コミットに紐づく議論スレッドを自動取得

生成されたダイジェストは日本語版・英語版の両方が `digests/` 配下に保存され、インデックスページも自動更新されます。

## ディレクトリ構成

```
digests/
  index.md           # 日本語インデックス
  index_en.md        # 英語インデックス
  ja/
    YYYY-MM/
      YYYY-MM-DD.md  # 日本語ダイジェスト
  en/
    YYYY-MM/
      YYYY-MM-DD.md  # 英語ダイジェスト
scripts/
  generate_postgresql_digest.py  # メイン生成スクリプト
  generate_digest.py             # 共通ユーティリティ
mkdocs.yml                       # MkDocs 設定
requirements.txt                 # Python 依存パッケージ
```

## セットアップ

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 使い方

```bash
# 前日分のダイジェストを生成（デフォルト）
python scripts/generate_postgresql_digest.py

# 日付を指定して生成
python scripts/generate_postgresql_digest.py 2026-05-07

# LLI CLIとモデルを指定
python scripts/generate_postgresql_digest.py --llm-cli gemini --model gemini-2.0-flash
python scripts/generate_postgresql_digest.py --llm-cli claude --model claude-sonnet-4-5
python scripts/generate_postgresql_digest.py --llm-cli codex
```

## 環境変数

| 変数名 | 説明 | デフォルト |
|--------|------|-----------|
| `DIGEST_LLM_CLI` | 使用するLLM CLI (`claude` / `codex` / `gemini`) | 自動検出 |
| `DIGEST_LLM_MODEL` | モデル名 | CLIのデフォルト |
| `DIGEST_LLM_TIMEOUT` | LLI呼び出しのタイムアウト（秒） | `600` |
| `GEMINI_API_KEY` | Gemini APIキー（`gemini` 使用時） | — |
| `OPENAI_API_KEY` | OpenAI APIキー（`codex` 使用時） | — |

## ドキュメントの表示

[MkDocs](https://www.mkdocs.org/) でローカルプレビューできます。

```bash
pip install mkdocs
mkdocs serve
```

## ライセンス

MIT
