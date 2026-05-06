PostgreSQL に関するニュースダイジェストを生成します。以下の3つの情報源を統合します：
- Hacker News 上の PostgreSQL 関連ストーリー
- postgres/postgres リポジトリのその日のコミット
- 各コミットの `Discussion:` リンクから取得したメーリングリスト議論

日付を指定しない場合は前日分を生成します。

以下の手順を実行してください:

1. 依存パッケージが未インストールの場合はインストールする:
   ```
   source venv/bin/activate && pip3 install -r requirements.txt -q
   ```

2. ダイジェスト生成スクリプトを実行する。`$ARGUMENTS` に日付（YYYY-MM-DD形式）が指定されていれば引数として渡す:
   ```
   # 日付指定あり
   python3 scripts/generate_postgresql_digest.py 2026-01-01
   # 日付指定なし（前日分）
   python3 scripts/generate_postgresql_digest.py
   ```

3. 生成されたMarkdownファイル（`digests/YYYY-MM-DD.md`）の内容を読み込み、ユーザーに概要を報告する。

4. 生成に失敗した場合は、エラーメッセージを確認してユーザーに原因を説明する。
   - APIキー未設定の場合はその旨を伝える
   - ネットワークエラーの場合はその旨を伝える
   - 該当日にPostgreSQLのストーリーが見つからない場合はその旨を伝える

補足:
- 使用するCLIは `--llm-cli` で明示可能。
- 省略時は `DIGEST_LLM_CLI` 環境変数、なければ `claude` → `codex` の順で自動選択。
