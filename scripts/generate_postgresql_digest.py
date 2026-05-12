#!/usr/bin/env python3
"""Generate a daily PostgreSQL digest from HN, git commits, and mailing list discussions."""

import argparse
import html
import os
import re
import sys
import subprocess
import shutil
import requests
from requests import RequestException
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from generate_digest import get_target_date, build_stories_text, resolve_llm_cli

ALGOLIA_HN_URL = "https://hn.algolia.com/api/v1/search_by_date"
GITHUB_COMMITS_URL = "https://api.github.com/repos/postgres/postgres/commits"
HN_LIMIT = 30
COMMITS_LIMIT = 50
THREAD_CHAR_LIMIT = 2000




def resolve_postgresql_llm_cli(cli_arg: str | None) -> str:
    """Resolve LLM CLI with Gemini SDK fallback when binary is unavailable."""
    try:
        return resolve_llm_cli(cli_arg)
    except RuntimeError as err:
        requested = cli_arg or os.getenv("DIGEST_LLM_CLI")
        if requested == "gemini" and os.getenv("GEMINI_API_KEY"):
            print(
                "gemini CLIが見つからないため、Gemini SDK経由で実行します。",
                file=sys.stderr,
            )
            return "gemini"
        raise err

def generate_with_gemini_sdk(prompt: str, system: str, model: str | None = None) -> str:
    try:
        import google.generativeai as genai
    except ImportError as err:
        raise RuntimeError(
            "gemini CLIが見つからず、Gemini SDKも利用できません。requirements.txt の依存をインストールしてください。"
        ) from err

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が未設定です。")

    genai.configure(api_key=api_key)
    model_name = model or "gemini-2.0-flash"
    sdk_model = genai.GenerativeModel(model_name)
    try:
        response = sdk_model.generate_content(f"{system}\n\n{prompt}")
    except Exception as err:
        raise RuntimeError(f"Gemini SDK error: {err}") from err

    text = getattr(response, "text", "") or ""
    if not text.strip():
        raise RuntimeError("Gemini SDKから有効な応答を取得できませんでした。")
    return text.strip()

def fetch_hn_stories(date: datetime) -> list[dict]:
    start_ts = int(date.timestamp())
    params = {
        "query": "postgresql postgres",
        "tags": "story",
        "numericFilters": f"created_at_i>{start_ts},created_at_i<{start_ts + 86400}",
        "hitsPerPage": HN_LIMIT,
        "attributesToRetrieve": "title,url,points,num_comments,objectID",
    }
    resp = requests.get(ALGOLIA_HN_URL, params=params, timeout=15)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    return sorted(hits, key=lambda x: x.get("points", 0), reverse=True)


def fetch_pg_commits(date: datetime) -> list[dict]:
    date_str = date.strftime("%Y-%m-%d")
    params = {
        "since": f"{date_str}T00:00:00Z",
        "until": f"{date_str}T23:59:59Z",
        "per_page": COMMITS_LIMIT,
    }
    resp = requests.get(
        GITHUB_COMMITS_URL,
        params=params,
        headers={"Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def extract_discussion_urls(commit_message: str) -> list[str]:
    return re.findall(r"Discussion:\s*(https?://\S+)", commit_message)


def fetch_thread_text(url: str) -> str | None:
    """Fetch mailing list thread text from a postgr.es or postgresql.org URL."""
    try:
        resp = requests.get(url, timeout=10, allow_redirects=True)
        final_url = resp.url

        # Convert to flat thread view on postgresql.org
        if "message-id" in final_url and "/flat/" not in final_url:
            msg_id = final_url.split("message-id/")[-1].strip("/")
            flat_url = f"https://www.postgresql.org/message-id/flat/{msg_id}"
            resp = requests.get(flat_url, timeout=10)

        # Extract <pre> blocks which contain email bodies in the archive
        pre_blocks = re.findall(r"<pre[^>]*>(.*?)</pre>", resp.text, re.DOTALL)
        if not pre_blocks:
            return None

        text_blocks = []
        for block in pre_blocks[:3]:
            plain = re.sub(r"<[^>]+>", "", block)
            plain = html.unescape(plain)
            plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
            if plain:
                text_blocks.append(plain)

        if not text_blocks:
            return None

        text = "\n\n---\n\n".join(text_blocks)
        return text[:THREAD_CHAR_LIMIT]
    except Exception:
        return None


def build_commits_section(commits: list[dict]) -> str:
    """Build text with commit summaries and fetched discussion threads."""
    if not commits:
        return "（この日のコミットはありません）"

    parts = []
    seen_urls: set[str] = set()

    for c in commits:
        message = c["commit"]["message"]
        subject = message.split("\n")[0]
        author = c["commit"]["author"]["name"]
        url = c["html_url"]
        sha = c["sha"][:8]

        # Full commit message body (useful for Claude)
        body = message[len(subject):].strip()

        section = f"### [{subject}]({url})\nAuthor: {author} ({sha})\n"
        if body:
            section += f"\n{body[:500]}\n"

        # Fetch mailing list discussion if linked
        discussion_urls = extract_discussion_urls(message)
        for disc_url in discussion_urls:
            if disc_url in seen_urls:
                continue
            seen_urls.add(disc_url)
            print(f"  Fetching discussion: {disc_url}")
            thread_text = fetch_thread_text(disc_url)
            if thread_text:
                section += f"\n**メーリングリスト議論** ({disc_url}):\n```\n{thread_text}\n```\n"

        parts.append(section)

    return "\n\n".join(parts)


def generate_digest(
    hn_stories: list[dict],
    commits: list[dict],
    commits_section: str,
    date: datetime,
    llm_cli: str,
    model: str | None = None,
) -> str:
    date_ja = date.strftime("%Y年%m月%d日")
    date_str = date.strftime("%Y-%m-%d")

    hn_text = build_stories_text(hn_stories) if hn_stories else "（この日のHNストーリーはありません）"

    system = (
        "あなたは優秀なPostgreSQLエキスパートです。"
        "Hacker News、GitHubコミット、メーリングリスト議論をもとに"
        "PostgreSQL開発者・運用者向けのニュースダイジェストを日本語で作成します。"
    )

    user_prompt = f"""{date_ja}のPostgreSQLに関するニュースダイジェストを作成してください。

## 出力フォーマット（厳守）

```
# {date_str} PostgreSQL ニュースダイジェスト

## コミット

（コミット件数・傾向の1〜2文の導入）

---

### ⭐ [コミットタイトル](コミットURL)
**著者名** — *Discussion: [メッセージID](メーリングリストURL)*

説明文（1〜2文）

**なぜ必要か:** （新機能の場合のみ）この機能が求められる背景・動機を1〜2文で記述。

**技術的課題:** （議論で言及されている場合のみ）解決すべき技術的課題を1〜2文で記述。

---

### [コミットタイトル](コミットURL)
**著者名**

説明文（1〜2文）

---

## HN ニュース

### ⭐ [記事タイトル](URL)
**スコア: X / コメント: Y**

説明文（1〜2文）

---

### [記事タイトル](URL)
**スコア: X / コメント: Y**

説明文（1〜2文）

---

## まとめ

2〜3文で当日の総括。

---

本ダイジェストはHacker News・GitHub・PostgreSQLメーリングリストの情報を元に{llm_cli}で生成しました。
```

## フォーマット規則
- タイトル直後に `---` を入れない。`## コミット` をすぐ続ける
- コミット・HN記事の見出しに番号（`1.`など）を付けない
- 重要度の高いものの見出しにのみ `⭐` を付ける（`### ⭐ [title](url)` の形式）
- 著者行は `**著者名**` とし、Discussionリンクがある場合は ` — *Discussion: [短縮テキスト](URL)*` を続ける
- メーリングリスト議論がある新機能のコミットには `**なぜ必要か:**` の行を追加する（バグ修正・リファクタリングには付けない）
- 議論中に技術的課題が言及されている場合は `**技術的課題:**` の行を追加する（言及がなければ省略）
- HNスコアは `**スコア: X / コメント: Y**` の形式（英語表記にしない）
- 各エントリの区切りに `---` を使う
- まとめセクションの後に `---` を置く
- 末尾フッターはイタリック（`*...*`）にしない
- フッター以降に余計な文章を追加しない

## コミット（{len(commits)}件）

{commits_section}

## HN ストーリー

{hn_text}
"""

    if llm_cli == "claude":
        cmd = [llm_cli, "-p", user_prompt, "--system-prompt", system]
        if model:
            cmd += ["--model", model]
    elif llm_cli == "codex":
        merged_prompt = f"{system}\n\n{user_prompt}"
        cmd = [llm_cli, "exec", merged_prompt]
        if model:
            cmd = [llm_cli, "-m", model, "exec", merged_prompt]
    elif llm_cli == "gemini":
        merged_prompt = f"{system}\n\n{user_prompt}"
        if shutil.which("gemini"):
            cmd = [llm_cli, "--skip-trust", "-p", merged_prompt]
            if model:
                cmd += ["--model", model]
        else:
            return generate_with_gemini_sdk(user_prompt, system, model)
    else:
        raise RuntimeError(f"Unsupported llm_cli: {llm_cli}")

    timeout = int(os.getenv("DIGEST_LLM_TIMEOUT", "600"))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as err:
        raise RuntimeError(
            f"{llm_cli} CLIが見つかりません。PATHに存在するか確認してください。 ({err})"
        ) from err
    except subprocess.TimeoutExpired:
        if llm_cli == "gemini":
            print(f"gemini CLIがタイムアウトしました（{timeout}秒）。SDK経由でフォールバックします。", file=sys.stderr)
            return generate_with_gemini_sdk(user_prompt, system, model)
        raise RuntimeError(f"{llm_cli} CLI がタイムアウトしました（{timeout}秒）")

    if result.returncode != 0:
        raise RuntimeError(f"{llm_cli} CLI error: {result.stderr.strip()}")

    output = result.stdout.strip()
    if "\n---\n" in output:
        output = output.split("\n---\n", 1)[1].strip()
    return output


def generate_digest_en(
    hn_stories: list[dict],
    commits: list[dict],
    commits_section: str,
    date: datetime,
    llm_cli: str,
    model: str | None = None,
) -> str:
    date_str = date.strftime("%Y-%m-%d")
    date_en = date.strftime("%B %d, %Y")

    hn_text = build_stories_text(hn_stories) if hn_stories else "(No HN stories for this day)"

    system = (
        "You are an expert PostgreSQL journalist. "
        "Based on Hacker News, GitHub commits, and mailing list discussions, "
        "you create daily news digests for PostgreSQL developers and operators in English."
    )

    user_prompt = f"""Create a PostgreSQL news digest for {date_en}.

## Output Format (strict)

# {date_str} PostgreSQL News Digest

## Commits

(1-2 sentence intro about the number and themes of commits)

---

### ⭐ [Commit Title](CommitURL)
**Author Name** — *Discussion: [MessageID](MailingListURL)*

Description (1-2 sentences)

**Why it matters:** (for new features only) Background and motivation in 1-2 sentences.

**Technical challenge:** (only if mentioned in the discussion) Technical challenge being solved in 1-2 sentences.

---

### [Commit Title](CommitURL)
**Author Name**

Description (1-2 sentences)

---

## HN News

### ⭐ [Article Title](URL)
**Score: X / Comments: Y**

Description (1-2 sentences)

---

## Summary

2-3 sentence summary of the day's highlights.

---

This digest was generated by {llm_cli} based on Hacker News, GitHub, and the PostgreSQL mailing list.

## Format rules
- Do not put `---` immediately after the title
- Do not number commit/HN entries
- Only mark high-importance items with ⭐ (format: ### ⭐ [title](url))
- Author line: **Author Name**, with Discussion link if available: — *Discussion: [short text](URL)*
- Add **Why it matters:** only for new feature commits with mailing list discussion (not for bug fixes)
- Add **Technical challenge:** only if mentioned in the discussion
- Score format: **Score: X / Comments: Y**
- Use --- as separator between entries
- Do not italicize the footer line

## Commits ({len(commits)} total)

{commits_section}

## HN Stories

{hn_text}
"""

    if llm_cli == "claude":
        cmd = [llm_cli, "-p", user_prompt, "--system-prompt", system]
        if model:
            cmd += ["--model", model]
    elif llm_cli == "codex":
        merged_prompt = f"{system}\n\n{user_prompt}"
        cmd = [llm_cli, "exec", merged_prompt]
        if model:
            cmd = [llm_cli, "-m", model, "exec", merged_prompt]
    elif llm_cli == "gemini":
        merged_prompt = f"{system}\n\n{user_prompt}"
        if shutil.which("gemini"):
            cmd = [llm_cli, "--skip-trust", "-p", merged_prompt]
            if model:
                cmd += ["--model", model]
        else:
            return generate_with_gemini_sdk(user_prompt, system, model)
    else:
        raise RuntimeError(f"Unsupported llm_cli: {llm_cli}")

    timeout = int(os.getenv("DIGEST_LLM_TIMEOUT", "600"))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as err:
        raise RuntimeError(f"{llm_cli} CLI not found: {err}") from err
    except subprocess.TimeoutExpired:
        if llm_cli == "gemini":
            return generate_with_gemini_sdk(user_prompt, system, model)
        raise RuntimeError(f"{llm_cli} CLI timed out ({timeout}s)")

    if result.returncode != 0:
        raise RuntimeError(f"{llm_cli} CLI error: {result.stderr.strip()}")

    output = result.stdout.strip()
    if "\n---\n" in output:
        output = output.split("\n---\n", 1)[1].strip()
    return output


def save_digest(content: str, date: datetime) -> str:
    month_dir = f"digests/ja/{date.strftime('%Y-%m')}"
    os.makedirs(month_dir, exist_ok=True)
    filename = f"{month_dir}/{date.strftime('%Y-%m-%d')}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def save_digest_en(content: str, date: datetime) -> str:
    month_dir = f"digests/en/{date.strftime('%Y-%m')}"
    os.makedirs(month_dir, exist_ok=True)
    filename = f"{month_dir}/{date.strftime('%Y-%m-%d')}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def load_digest_overview(filename: str, max_lines: int = 20) -> str:
    with open(filename, encoding="utf-8") as f:
        lines = [line.rstrip() for line in f.readlines()]
    return "\n".join(lines[:max_lines]).strip()


_MONTH_NAMES_EN = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


def _month_header_ja(year: int, month: int) -> str:
    return f"## {year}年{month}月"


def _month_header_en(year: int, month: int) -> str:
    return f"## {_MONTH_NAMES_EN[month]} {year}"


def update_index(date: datetime, no_data_dates: set[str] | None = None) -> None:
    index_path = "digests/index.md"
    index_en_path = "digests/index_en.md"
    date_str = date.strftime("%Y-%m-%d")
    no_data_dates = no_data_dates or set()

    # Collect existing date strings from JA index (match new link format ./ja/YYYY-MM/YYYY-MM-DD.md)
    dates: list[str] = [date_str]
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                m = re.search(r"\[(\d{4}-\d{2}-\d{2})\]", line)
                if m and m.group(1) != date_str:
                    dates.append(m.group(1))

    # Group dates by month and sort descending
    from collections import defaultdict
    months: dict[str, list[str]] = defaultdict(list)
    for d in dates:
        months[d[:7]].append(d)
    sorted_months = sorted(months.keys(), reverse=True)
    for key in sorted_months:
        months[key].sort(reverse=True)

    # Build Japanese index
    ja_header = (
        "# PostgreSQL News Digest\n\n"
        "**[日本語]** | [English](./index_en.md)\n\n"
        "Hacker News・GitHub・メーリングリストをもとに毎日自動生成されるPostgreSQL向けニュースダイジェストです。\n"
    )
    ja_sections: list[str] = []
    for ym in sorted_months:
        year, month = int(ym.split("-")[0]), int(ym.split("-")[1])
        ja_sections.append(_month_header_ja(year, month))
        for d in months[ym]:
            if d in no_data_dates:
                ja_sections.append(f"- {d}（HNの記事とコミットがない）")
            else:
                ja_sections.append(f"- [{d}](./ja/{ym}/{d}.md)")
        ja_sections.append("")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(ja_header + "\n" + "\n".join(ja_sections).rstrip() + "\n")

    # Build English index
    en_header = (
        "# PostgreSQL News Digest\n\n"
        "[日本語](./index.md) | **[English]**\n\n"
        "A daily auto-generated PostgreSQL news digest based on Hacker News, GitHub, and mailing lists.\n"
    )
    en_sections: list[str] = []
    for ym in sorted_months:
        year, month = int(ym.split("-")[0]), int(ym.split("-")[1])
        en_sections.append(_month_header_en(year, month))
        for d in months[ym]:
            if d in no_data_dates:
                en_sections.append(f"- {d} (No HN stories or commits)")
            else:
                en_sections.append(f"- [{d}](./en/{ym}/{d}.md)")
        en_sections.append("")

    with open(index_en_path, "w", encoding="utf-8") as f:
        f.write(en_header + "\n" + "\n".join(en_sections).rstrip() + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="Target date in YYYY-MM-DD format")
    parser.add_argument("--llm-cli", choices=["claude", "codex", "gemini"])
    parser.add_argument("--model", help="Model name to use (e.g. sonnet, gemini-2.0-flash, o3)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        llm_cli = resolve_postgresql_llm_cli(args.llm_cli)
    except RuntimeError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
    if args.date:
        try:
            date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Invalid date format: {args.date}. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)
    else:
        date = get_target_date(offset_days=1)

    date_str = date.strftime("%Y-%m-%d")
    required_key_by_cli = {
        "codex": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    required_key = required_key_by_cli.get(llm_cli)
    if required_key and not os.getenv(required_key):
        print(
            f"{required_key} が未設定です。{llm_cli} CLIを使う場合は環境変数を設定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Fetching data for {date_str}...")

    try:
        print("  [1/3] Fetching HN stories...")
        hn_stories = fetch_hn_stories(date)
        print(f"        Found {len(hn_stories)} HN stories.")

        print("  [2/3] Fetching PostgreSQL commits...")
        commits = fetch_pg_commits(date)
        print(f"        Found {len(commits)} commits.")
    except RequestException as err:
        print(f"ネットワークエラーが発生しました: {err}", file=sys.stderr)
        sys.exit(1)

    if not hn_stories and not commits:
        print(
            "該当日にPostgreSQLのストーリー/コミットが見つかりませんでした。インデックスに記録して終了します。",
            file=sys.stderr,
        )
        update_index(date, no_data_dates={date_str})
        sys.exit(0)
    if not hn_stories:
        print(
            "該当日にPostgreSQLのストーリーが見つからなかったため、コミットのみでダイジェストを生成します。",
            file=sys.stderr,
        )

    print("  [3/3] Fetching mailing list discussions...")
    commits_section = build_commits_section(commits)

    model = args.model or os.getenv("DIGEST_LLM_MODEL")
    print(f"Generating digest with {llm_cli}{f' ({model})' if model else ''}...")
    try:
        digest = generate_digest(hn_stories, commits, commits_section, date, llm_cli, model)
    except RuntimeError as err:
        message = str(err)
        if llm_cli == "gemini" and any(token in message.lower() for token in ("quota", "resource_exhausted", "429")):
            print(
                "Gemini APIのクォータ超過のため、本日のPostgreSQLダイジェスト生成をスキップします。",
                file=sys.stderr,
            )
            print(message, file=sys.stderr)
            sys.exit(0)
        print(message, file=sys.stderr)
        sys.exit(1)

    output_file = save_digest(digest, date)
    print(f"Generating English digest with {llm_cli}{f' ({model})' if model else ''}...")
    try:
        digest_en = generate_digest_en(hn_stories, commits, commits_section, date, llm_cli, model)
        output_file_en = save_digest_en(digest_en, date)
        print(f"English digest saved to: {output_file_en}")
    except RuntimeError as err:
        print(f"Warning: Failed to generate English digest: {err}", file=sys.stderr)
    update_index(date)
    overview = load_digest_overview(output_file)

    print(f"Digest saved to: {output_file}")
    print("Index updated: digests/postgresql/index.md")
    print("\n=== Digest Overview ===")
    print(overview)


if __name__ == "__main__":
    main()
