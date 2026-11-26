#!/usr/bin/env python3
"""
IT情報収集・分析・通知システム - バックエンドエンジン
Hacker News API / RSS / Gemini REST API / Discord・Slack・LINE 通知

処理フロー:
  1. 収集   (Hacker News + RSS 8ソース)
  2. 重複除去 (URL正規化)
  3. 本文取得 (並列フェッチ + テキスト抽出)
  4. Gemini バッチ解析 (要約・翻訳・タグ・カテゴリ・スコア)
  5. HOTスコアリング (複数ソース出現で加点)
  6. 関連記事グルーピング (埋め込みベクトルのコサイン類似度)
  7. 保存 (data.json / archive / feed.xml / アーカイブローテーション)
  8. 週間ダイジェスト (日曜のみ)
  9. 通知 (Discord / Slack / LINE、NOTIFY_AT_JST 設定時はその時刻まで待機)

実行モード:
  python gather.py               収集 → 通知
  python gather.py --no-notify   収集・保存のみ (GitHub Actions の collect ジョブ)
  python gather.py --notify-only 保存済み data.json から通知のみ (notify ジョブ)
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import math
import os
import random
import sys
import io
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from urllib.parse import parse_qsl, urlencode, urlparse

import feedparser
import requests

from wait_until import wait_until_jst


# ---------------------------------------------------------------------------
# コンソール出力の文字化け対策 (Windows cp932)
# ---------------------------------------------------------------------------
if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace"
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace"
    )


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_FETCH_COUNT = 8

RSS_FEEDS = {
    "Zenn": "https://zenn.dev/feed",
    "Qiita": "https://qiita.com/popular-items/feed.atom",
    "Reddit": "https://www.reddit.com/r/technology/.rss",
    "はてなブックマーク": "https://b.hatena.ne.jp/hotentry/it.rss",
    "Publickey": "https://www.publickey1.jp/atom.xml",
    "Dev.to": "https://dev.to/feed",
    "GitHub Blog": "https://github.blog/feed/",
    "arXiv cs.AI": "https://rss.arxiv.org/rss/cs.AI",
}
RSS_FETCH_COUNT = 5

# gemini-2.5-flash: 安定版・高速応答・JSON出力完全対応（環境変数 GEMINI_MODEL で変更可）
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:batchEmbedContents?key={key}"
)
# 要約・分類に推論は不要。thinking 既定 ON だと 1 リクエスト数十秒かかり
# collect ジョブが時間切れになり得るため無効化する
GEMINI_THINKING_BUDGET = 0
GEMINI_SLEEP_SEC = 4          # リクエスト間隔（15 RPM対応）
GEMINI_MAX_RETRIES = 4        # リトライ上限
GEMINI_BACKOFF_MAX_SEC = 60   # バックオフ上限秒数
GLOBAL_TIMEOUT_SEC = 840      # 全体タイムアウト: 14分

ANALYSIS_BATCH_SIZE = 6       # 1リクエストで解析する記事数

BODY_FETCH_WORKERS = 8        # 本文取得の並列数
BODY_FETCH_TIMEOUT = 10       # 本文取得タイムアウト秒
BODY_FETCH_MAX_BYTES = 512 * 1024  # 本文取得の最大受信バイト数 (512KB)
BODY_MAX_CHARS = 1500         # Gemini に渡す本文抜粋の最大文字数
EMBED_BATCH_SIZE = 80         # batchEmbedContents 1リクエストあたりの上限対策
RETRY_FAILED_TODAY_MAX = 18   # 当日失敗記事の同日再解析上限

CATEGORIES = [
    "AI・機械学習",
    "Web・フロントエンド",
    "バックエンド・インフラ",
    "セキュリティ",
    "プログラミング言語",
    "モバイル",
    "ハードウェア・ガジェット",
    "ビジネス・業界動向",
    "その他",
]

SCORE_HOT_BONUS = 20
SCORE_MAX = 100
NOTIFY_TOP_N = 5
NOTIFY_SCORE_THRESHOLD = 80

TOPIC_SIM_THRESHOLD = 0.82    # 関連記事とみなすコサイン類似度
EMBED_DIMENSIONS = 256

ARCHIVE_KEEP_DAYS = 180       # アーカイブ保持日数
PAST_SEEN_DAYS = 3            # 既出記事の判定・解析再利用に参照する過去アーカイブ日数

HATENA_COUNT_URL = "https://bookmark.hatenaapis.com/count/entries"
HATENA_BATCH_SIZE = 50

DATA_FILE = "data.json"
ARCHIVE_DIR = "archive"
SEARCH_INDEX_FILE = os.path.join(ARCHIVE_DIR, "search_index.json")
TRENDS_FILE = os.path.join(ARCHIVE_DIR, "trends_30d.json")
FEED_FILE = "feed.xml"
DIGEST_FILE = "digest.json"
FEED_TOP_N = 20
SITE_URL = "https://muu0726.github.io/IT-News/"

# カテゴリ別 RSS フィード設定 (E-2)
CATEGORY_FEEDS = {
    "feed_ai.xml": {
        "title": "IT Info Hub — AI・機械学習",
        "description": "AI・機械学習・LLMに関する最新技術ニュース（スコア上位）",
        "categories": ["AI・機械学習"],
        "top_n": 15,
    },
    "feed_security.xml": {
        "title": "IT Info Hub — セキュリティ",
        "description": "セキュリティ・脆弱性・CI/CD保護に関する最新技術ニュース（スコア上位）",
        "categories": ["セキュリティ"],
        "top_n": 15,
    },
    "feed_web.xml": {
        "title": "IT Info Hub — Web & クラウド・インフラ",
        "description": "Web・フロントエンド・バックエンド・インフラに関する最新技術ニュース（スコア上位）",
        "categories": ["Web・フロントエンド", "バックエンド・インフラ"],
        "top_n": 15,
    },
}

JST = timezone(timedelta(hours=9))

BATCH_ANALYSIS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "title": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "actionable_takeaway": {"type": "STRING"},
            "github_repo": {"type": "STRING"},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "category": {"type": "STRING", "enum": CATEGORIES},
            "score": {"type": "INTEGER"},
            "score_reason": {"type": "STRING"},
        },
        "required": [
            "id", "title", "summary", "actionable_takeaway", "github_repo",
            "tags", "category", "score", "score_reason"
        ],
    },
}

DIGEST_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overview": {"type": "STRING"},
        "trends": {"type": "ARRAY", "items": {"type": "STRING"}},
        "highlights": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "url": {"type": "STRING"},
                    "comment": {"type": "STRING"},
                },
                "required": ["title", "url", "comment"],
            },
        },
    },
    "required": ["overview", "trends", "highlights"],
}

BRIEFING_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "edition": {"type": "STRING"},
        "summary_lines": {"type": "ARRAY", "items": {"type": "STRING"}},
        "focus_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary_lines", "focus_tags"],
}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
_KEEP_QUERY_KEYS = {"id", "v", "p"}

_GITHUB_RESERVED_PATHS = {
    "about", "collections", "contact", "customer-stories", "enterprise",
    "events", "explore", "features", "issues", "join", "login", "marketplace",
    "notifications", "organizations", "orgs", "pricing", "pulls", "readme",
    "security", "settings", "site", "sponsors", "team", "topics", "trending",
    "users", "blog"
}


def _normalize_github_repo(owner: str, repo: str) -> str | None:
    """owner / repo 文字列を検証・正規化して 'owner/repo' 形式で返す"""
    owner = (owner or "").strip()
    repo = (repo or "").strip()
    if repo.endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".,;)\"'>")
    if (
        owner
        and owner.lower() not in _GITHUB_RESERVED_PATHS
        and repo
        and not repo.endswith((".html", ".xml", ".png", ".jpg", ".md"))
        and re.match(r"^[a-zA-Z0-9_.-]+$", owner)
        and re.match(r"^[a-zA-Z0-9_.-]+$", repo)
    ):
        return f"{owner}/{repo}"
    return None


def extract_github_repo(url: str, text: str = "") -> str | None:
    """記事URLまたは本文抜粋から関連する GitHub リポジトリ名 (owner/repo) を抽出する (A-2)"""
    # 1. 記事URL自体の検証
    if url:
        parsed = urlparse(url)
        if "github.com" in parsed.netloc.lower():
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(parts) >= 2:
                normalized = _normalize_github_repo(parts[0], parts[1])
                if normalized:
                    return normalized

    # 2. 本文またはURLテキスト内の github.com/owner/repo パターン検索
    combined = f"{url} {text}"
    matches = re.findall(
        r"https?://(?:www\.)?github\.com/([a-zA-Z0-9_\.-]+)/([a-zA-Z0-9_\.-]+)",
        combined,
    )
    for owner, repo in matches:
        normalized = _normalize_github_repo(owner, repo)
        if normalized:
            return normalized

    return None


def normalize_url(url: str) -> str:
    """URL を正規化して重複比較に使う（トラッキングクエリは除去し、識別子パラメータは保持）"""
    if not url:
        return ""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()
    if parsed.query:
        kept = [
            (k.lower(), v)
            for k, v in parse_qsl(parsed.query)
            if k.lower() in _KEEP_QUERY_KEYS and v
        ]
        if kept:
            kept.sort()
            return f"{base}?{urlencode(kept)}"
    return base


def safe_get(url: str, timeout: int = 15) -> requests.Response | None:
    """安全な HTTP GET"""
    headers = {
        "User-Agent": "IT-Info-Collector/1.0 (GitHub Actions Bot)"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        print(f"[WARN] GET failed: {url} -- {e}")
        return None


def parse_iso_datetime(
    dt_str: str,
    default_tz: timezone = timezone.utc,
) -> datetime | None:
    """ISO8601 文字列を aware な datetime に安全に変換する"""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=default_tz)
        return dt
    except (ValueError, TypeError):
        return None


_ARCHIVE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


def _load_json_file(path: str, warn_prefix: str = "") -> dict | None:
    """JSON ファイルを安全に読み込み、dict のみ返す（未存在ファイルは静かに None）"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        if warn_prefix:
            print(f"[WARN] {warn_prefix} {path}: {e}")
        return None


def _list_archive_date_files(archive_dir: str = ARCHIVE_DIR, reverse: bool = True) -> list[str]:
    """アーカイブディレクトリ内の YYYY-MM-DD.json ファイル名一覧を返す"""
    if not os.path.exists(archive_dir):
        return []
    return sorted(
        [fn for fn in os.listdir(archive_dir) if _ARCHIVE_DATE_RE.match(fn)],
        reverse=reverse,
    )


def format_pub_date(dt_str: str) -> str:
    """ISO8601 日時文字列から RSS 用の <pubDate> タグ文字列を生成する"""
    dt = parse_iso_datetime(dt_str)
    return f"      <pubDate>{format_datetime(dt)}</pubDate>\n" if dt else ""


def post_webhook(
    url: str,
    payload: dict,
    service_name: str,
    headers: dict | None = None,
    timeout: int = 15,
) -> bool:
    """汎用 Webhook 送信ヘルパー（共通ヘッダー、タイムアウト、エラーハンドリング）"""
    if not url:
        return False
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    try:
        resp = requests.post(url, json=payload, headers=req_headers, timeout=timeout)
        if resp.status_code in (200, 204):
            print(f"[INFO] {service_name} sent")
            return True
        print(f"[WARN] {service_name} returned {resp.status_code}: {resp.text[:120]}")
        return False
    except requests.RequestException as e:
        print(f"[ERROR] {service_name} failed: {e}")
        return False


def deduplicate_articles(articles: list[dict]) -> list[dict]:
    """URL を正規化し、重複を除去（最初に出現したものを保持）"""
    seen_urls: set[str] = set()
    unique: list[dict] = []
    for art in articles:
        norm = normalize_url(art.get("url", ""))
        if not norm or norm not in seen_urls:
            seen_urls.add(norm)
            unique.append(art)
    return unique


def extract_text_from_gemini_response(data: dict) -> str | None:
    """Gemini レスポンスからテキストを安全に抽出する"""
    try:
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason", "")
        if finish_reason not in ("", "STOP", "MAX_TOKENS"):
            print(f"[WARN] Gemini finishReason: {finish_reason}")
            return None

        parts = candidate["content"]["parts"]
        for part in parts:
            if part.get("thought", False):
                continue
            if "text" in part:
                return part["text"]
        for part in reversed(parts):
            if "text" in part:
                return part["text"]
    except (KeyError, IndexError, TypeError) as e:
        print(f"[WARN] Gemini response structure error: {e}")
    return None


def parse_json_safely(text: str, quiet: bool = False):
    """JSON テキストを安全にパースする（コードフェンス・BOM・不正エスケープ除去付き）"""
    if not text:
        return None
    text = text.strip().lstrip("﻿")
    # 不要なプレフィックス・サフィックス（「以下がJSONです」等）を除去するため、
    # 最初の `[` または `{` から、最後の `]` または `}` までを抽出する
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # JSON 仕様外のバックスラッシュエスケープ (\_, \( 等) を補修して再試行
        repaired = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r"\\\\", text)
        if repaired != text:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        if not quiet:
            print(f"[WARN] JSON parse failed: {e}")
            print(f"[DEBUG] Raw text (first 200 chars): {text[:200]}")
        return None


def validate_analysis_item(item) -> bool:
    """バッチ解析結果の1件分を検証する"""
    if not isinstance(item, dict):
        return False
    for key in ("id", "title", "summary", "tags", "score"):
        if key not in item:
            return False
    if not isinstance(item["tags"], list):
        return False
    if not isinstance(item["score"], (int, float)) or isinstance(item["score"], bool):
        return False
    if not isinstance(item["id"], int) or isinstance(item["id"], bool):
        return False
    return True


def parse_published(entry) -> str:
    """feedparser のエントリから published を ISO8601 (UTC) に正規化する"""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except (TypeError, ValueError):
                pass
    return entry.get("published", entry.get("updated", ""))


def xml_escape(text: str) -> str:
    """XML 用エスケープ"""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# データソース取得
# ---------------------------------------------------------------------------
def _fetch_one_hn_item(sid: int) -> dict | None:
    """Hacker News の個別アイテムを1件取得する"""
    item_resp = safe_get(HN_ITEM_URL.format(sid))
    if not item_resp:
        return None
    try:
        item = item_resp.json()
    except ValueError as e:
        print(f"[WARN] Hacker News item {sid} JSON parse failed: {e}")
        return None
    if not isinstance(item, dict):
        return None
    return {
        "title": item.get("title", ""),
        "url": item.get("url", f"https://news.ycombinator.com/item?id={sid}"),
        "source": "Hacker News",
        "published": datetime.fromtimestamp(
            item.get("time", 0), tz=timezone.utc
        ).isoformat(),
        "hn_score": item.get("score", 0),
        "hn_comments": item.get("descendants", 0),
    }


def fetch_hackernews() -> list[dict]:
    """Hacker News Top Stories 上位 N 件を並列取得（並び順維持）"""
    print("[INFO] Fetching Hacker News Top Stories ...")
    resp = safe_get(HN_TOP_URL)
    if not resp:
        return []

    try:
        raw_ids = resp.json()
    except ValueError as e:
        print(f"[WARN] Hacker News topstories JSON parse failed: {e}")
        return []
    if not isinstance(raw_ids, list):
        print("[WARN] Hacker News topstories returned unexpected payload")
        return []
    story_ids = raw_ids[:HN_FETCH_COUNT]
    if not story_ids:
        return []

    workers = min(8, len(story_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_fetch_one_hn_item, story_ids))
    articles = [a for a in results if a is not None]
    print(f"[INFO] Hacker News: {len(articles)} articles fetched")
    return articles


def _fetch_one_rss_feed(item: tuple[str, str]) -> list[dict]:
    """1つの RSS/Atom フィードから最新 N 件を取得する"""
    source_name, feed_url = item
    print(f"[INFO] Fetching RSS: {source_name} ...")
    fetched: list[dict] = []
    try:
        resp = safe_get(feed_url, timeout=15)
        if not resp:
            return fetched
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            print(f"[WARN] RSS feed error ({source_name}): {feed.bozo_exception}")
            return fetched

        entries = feed.entries[:RSS_FETCH_COUNT]
        for entry in entries:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if hasattr(title, "replace"):
                title = re.sub(r"<[^>]+>", "", title).strip()
            fetched.append({
                "title": title,
                "url": link,
                "source": source_name,
                "published": parse_published(entry),
            })
        print(f"[INFO] {source_name}: {len(entries)} articles fetched")
    except Exception as e:
        print(f"[ERROR] RSS fetch failed ({source_name}): {e}")
    return fetched


def fetch_rss_feeds() -> list[dict]:
    """各 RSS/Atom フィードから最新 N 件を並列取得（ソース順維持）"""
    feed_items = list(RSS_FEEDS.items())
    if not feed_items:
        return []
    workers = min(8, len(feed_items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        per_feed = list(pool.map(_fetch_one_rss_feed, feed_items))
    all_articles: list[dict] = []
    for chunk in per_feed:
        all_articles.extend(chunk)
    return all_articles


def fetch_hatena_counts(articles: list[dict]) -> None:
    """はてなブックマーク件数取得APIで最大50件ずつ一括取得して hatena_count に格納する"""
    urls = [
        a.get("url", "")
        for a in articles
        if str(a.get("url", "")).startswith(("http://", "https://"))
    ]
    if not urls:
        return
    unique_urls = list(dict.fromkeys(urls))
    counts: dict[str, int] = {}
    headers = {"User-Agent": "IT-Info-Collector/1.0 (GitHub Actions Bot)"}
    for i in range(0, len(unique_urls), HATENA_BATCH_SIZE):
        chunk = unique_urls[i:i + HATENA_BATCH_SIZE]
        try:
            resp = requests.get(
                HATENA_COUNT_URL,
                params=[("url", u) for u in chunk],
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    for u, c in data.items():
                        if isinstance(c, (int, float)) and not isinstance(c, bool):
                            counts[u] = int(c)
        except (requests.RequestException, ValueError) as e:
            print(f"[WARN] Hatena bookmark count fetch failed: {e}")

    norm_counts: dict[str, int] = {}
    for u, c in counts.items():
        nu = normalize_url(u)
        if nu:
            norm_counts[nu] = max(norm_counts.get(nu, 0), c)

    for art in articles:
        raw_u = art.get("url", "")
        nu = normalize_url(raw_u)
        c = counts.get(raw_u, norm_counts.get(nu, art.get("hatena_count", 0)))
        art["hatena_count"] = int(c or 0)

    got = sum(1 for a in articles if a.get("hatena_count", 0) > 0)
    print(f"[INFO] Hatena bookmark counts: {got}/{len(articles)} articles have bookmarks")


# ---------------------------------------------------------------------------
# 記事本文の取得（要約精度向上）
# ---------------------------------------------------------------------------
_BODY_STRIP_RE = re.compile(
    r"<(script|style|noscript|svg|iframe|nav|header|footer|form|aside)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _extract_meta_and_ld_text(html: str) -> str:
    """HTML から OGP / meta description / JSON-LD の本文・説明文を抽出する"""
    snippets: list[str] = []
    # meta og:description / description / twitter:description
    for m in re.finditer(r"<meta\s+[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if not re.search(r'(?:property|name)\s*=\s*["\'](?:og:description|twitter:description|description)["\']', tag, re.IGNORECASE):
            continue
        cm = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if cm:
            val = html_module.unescape(cm.group(1)).strip()
            if val and val not in snippets:
                snippets.append(val)

    # JSON-LD articleBody / description
    for m in re.finditer(
        r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        raw_ld = m.group(1).strip()
        parsed_ld = parse_json_safely(raw_ld, quiet=True)
        items = parsed_ld if isinstance(parsed_ld, list) else [parsed_ld] if isinstance(parsed_ld, dict) else []
        for obj in items:
            if not isinstance(obj, dict):
                continue
            for key in ("articleBody", "description"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    clean_val = _WS_RE.sub(" ", html_module.unescape(_TAG_RE.sub(" ", val))).strip()
                    if clean_val and clean_val not in snippets:
                        snippets.append(clean_val)

    return " ".join(snippets).strip()


def _extract_clean_text_full(html: str) -> str:
    """HTML から本文テキスト全体（切り詰め前）を抽出する（OGP・JSON-LD フォールバック付き）"""
    if not html:
        return ""
    meta_text = _extract_meta_and_ld_text(html)
    text = _BODY_STRIP_RE.sub(" ", html)
    # <article> または <main> があればその内部を優先対象にする
    main_blocks = re.findall(r"<(article|main)[^>]*>(.*?)</\1>", text, re.IGNORECASE | re.DOTALL)
    if main_blocks:
        candidate = " ".join(b[1] for b in main_blocks)
        if len(_TAG_RE.sub("", candidate).strip()) > 200:
            text = candidate
    # <p> 系ブロックを優先的に抽出（本文である可能性が高い）
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", text, re.IGNORECASE | re.DOTALL)
    if paragraphs and sum(len(p) for p in paragraphs) > 300:
        text = " ".join(paragraphs)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    text = _WS_RE.sub(" ", text).strip()

    if len(text) < 250 and meta_text:
        return f"{meta_text} {text}".strip() if text else meta_text
    return text


def extract_main_text(html: str) -> str:
    """HTML から本文らしきテキストを抽出する（OGP・JSON-LD フォールバック付き）"""
    return _extract_clean_text_full(html)[:BODY_MAX_CHARS]


def estimate_reading_minutes(text: str) -> int:
    """本文テキストから推定読了時間（分、1〜60）を算出する"""
    cleaned = (text or "").strip()
    if not cleaned:
        return 1
    non_ws = re.sub(r"\s+", "", cleaned)
    if not non_ws:
        return 1
    ascii_chars = sum(1 for ch in non_ws if ord(ch) < 128)
    if ascii_chars / len(non_ws) > 0.7:
        words = len(cleaned.split())
        minutes = round(words / 220)
    else:
        minutes = round(len(non_ws) / 600)
    return max(1, min(60, minutes))


def _read_response_html(resp, max_bytes: int = BODY_FETCH_MAX_BYTES) -> str:
    """HTTP レスポンスをサイズ上限付きで読み込み、charset 未指定の日本語サイトも文字化けなくデコードする"""
    if not hasattr(resp, "iter_content"):
        return getattr(resp, "text", "") or ""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            remain = max_bytes - total
            if len(chunk) >= remain:
                chunks.append(chunk[:remain])
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        close_fn = getattr(resp, "close", None)
        if callable(close_fn):
            close_fn()

    raw = b"".join(chunks)
    if not raw:
        return ""

    content_type = (getattr(resp, "headers", None) or {}).get("Content-Type", "")
    enc: str | None = None
    m_ct = re.search(r"charset=([^\s;\"']+)", content_type, re.IGNORECASE)
    if m_ct:
        cand = m_ct.group(1).strip()
        if cand.lower() not in ("iso-8859-1", "latin-1", "latin1"):
            enc = cand

    if not enc:
        m_meta = re.search(
            rb'<meta[^>]+charset=["\']?\s*([A-Za-z0-9_.:-]+)',
            raw[:2048],
            re.IGNORECASE,
        )
        if m_meta:
            enc = m_meta.group(1).decode("ascii", "ignore").strip()

    if not enc:
        try:
            app_enc = getattr(resp, "apparent_encoding", None)
            if app_enc and str(app_enc).lower() not in ("iso-8859-1", "latin-1", "latin1", "ascii"):
                enc = str(app_enc)
        except Exception:
            pass

    if enc:
        try:
            return raw.decode(enc, errors="replace")
        except LookupError:
            pass

    for candidate in ("utf-8", "cp932", "euc_jp"):
        try:
            return raw.decode(candidate)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _fetch_one_body(art: dict) -> None:
    """1記事の本文を取得して art['_body'] および推定読了時間 art['reading_min'] に格納する"""
    url = art.get("url", "")
    if not url.startswith(("http://", "https://")):
        return
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; IT-Info-Collector/1.0)"},
            timeout=BODY_FETCH_TIMEOUT,
            stream=True,
        )
        content_type = (getattr(resp, "headers", None) or {}).get("Content-Type", "")
        if resp.ok and "html" in content_type.lower():
            html_text = _read_response_html(resp)
            full_text = _extract_clean_text_full(html_text)
            art["_body"] = full_text[:BODY_MAX_CHARS]
            if len(full_text) >= 200:
                art["reading_min"] = estimate_reading_minutes(full_text)
        elif hasattr(resp, "close"):
            resp.close()
    except requests.RequestException:
        pass  # 本文が取れなくてもタイトルベースで解析を続行


def fetch_article_bodies(articles: list[dict]) -> None:
    """全記事の本文を並列取得する"""
    print(f"[INFO] Fetching article bodies ({len(articles)} articles, "
          f"{BODY_FETCH_WORKERS} workers) ...")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=BODY_FETCH_WORKERS) as pool:
        futures = [pool.submit(_fetch_one_body, art) for art in articles]
        for f in as_completed(futures):
            f.result()
    got = sum(1 for a in articles if a.get("_body"))
    print(f"[INFO] Article bodies: {got}/{len(articles)} fetched "
          f"({time.monotonic() - t0:.1f}s)")


# ---------------------------------------------------------------------------
# Gemini バッチ解析
# ---------------------------------------------------------------------------
def build_batch_prompt(chunk: list[dict]) -> str:
    """複数記事をまとめて解析するプロンプトを構築する"""
    blocks = []
    for i, art in enumerate(chunk):
        body = art.get("_body", "")
        body_line = f"本文抜粋: {body}" if body else "本文抜粋: (取得できず。タイトルから推測)"
        blocks.append(
            f"### 記事ID: {i}\n"
            f"タイトル: {art.get('title', '')}\n"
            f"ソース: {art.get('source', '')}\n"
            f"URL: {art.get('url', '')}\n"
            f"{body_line}"
        )
    articles_block = "\n\n".join(blocks)
    categories = " / ".join(CATEGORIES)

    return (
        f"あなたはIT技術ニュースの分析AIです。以下の{len(chunk)}件の記事を分析してください。\n\n"
        f"各記事について、以下のフィールドを持つオブジェクトを生成してください:\n"
        f"- id: 入力の記事IDと同じ整数\n"
        f"- title: 記事タイトル（英語の場合は自然な日本語に翻訳。日本語ならそのまま）\n"
        f"- summary: 日本語で3行の要約（改行は\\nで区切る）。本文抜粋がある場合はそれに基づき、"
        f"ない場合はタイトルから内容を推測して作成\n"
        f"- actionable_takeaway: エンジニアの実務への影響や実践的示唆、試すべきこと（例: アップデート時の注意点、検証推奨コマンド、開発への具体的影響など）を日本語1文（80文字程度）で提示\n"
        f"- github_repo: 記事が特定のオープンソースソフトウェアやGitHub上のリポジトリに関する場合、その 'owner/repo' 名（例: 'facebook/react'）。関連するリポジトリがない場合は空文字 \"\"\n"
        f"- tags: 技術タグを3つ（日本語または英語）のリスト\n"
        f"- category: 次のリストから必ず1つだけ選択: {categories}\n"
        f"- score: 重要度スコア（0-100の整数）。IT業界への影響度、技術的新規性、実用性を総合評価\n"
        f"- score_reason: スコアの理由を日本語で1文\n\n"
        f"{articles_block}\n\n"
        f"回答は全{len(chunk)}件分のオブジェクトを含むJSON配列**のみ**を出力してください:\n"
        f'[{{"id": 0, "title": "...", "summary": "1行目\\n2行目\\n3行目", '
        f'"actionable_takeaway": "...", "github_repo": "", '
        f'"tags": ["a", "b", "c"], "category": "...", "score": 75, "score_reason": "..."}}, ...]'
    )


def _parse_retry_after(resp: requests.Response) -> float | None:
    """429 レスポンスの Retry-After ヘッダーや retryDelay フィールドから待機秒数を取得する"""
    headers = getattr(resp, "headers", None) or {}
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra is not None:
        try:
            val = float(str(ra).strip())
            if val > 0:
                return min(val, 120.0)
        except ValueError:
            pass
    text = getattr(resp, "text", "") or ""
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    if m:
        try:
            val = float(m.group(1))
            if val > 0:
                return min(val, 120.0)
        except ValueError:
            pass
    return None


def _calc_gemini_retry_wait(resp: requests.Response, attempt: int) -> float:
    """429 / 5xx 応答に対するバックオフ待機秒数（ジッター付き）を算出する"""
    exp_wait = min(GEMINI_SLEEP_SEC * (2 ** attempt), GEMINI_BACKOFF_MAX_SEC)
    if resp.status_code == 429:
        ra = _parse_retry_after(resp)
        if ra is not None:
            exp_wait = max(exp_wait, min(ra + 1.0, 90.0))
    return exp_wait + random.uniform(0, 2.0)


def _can_retry_before_deadline(wait_sec: float, deadline: float | None) -> bool:
    """待機後の時刻が deadline 未満に収まるかを判定する"""
    return True if deadline is None else (time.monotonic() + wait_sec) < deadline


def call_gemini_rest(
    prompt: str,
    api_key: str,
    response_schema: dict | None = None,
    deadline: float | None = None,
) -> list | dict | None:
    """Gemini REST API を直接呼び出し (リトライ・responseSchema・全体期限対応)"""
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)
    gen_config: dict = {
        "responseMimeType": "application/json",
        "temperature": 0.2,
    }
    if response_schema is not None:
        gen_config["responseSchema"] = response_schema
    # thinking を 0 にできるのは flash 系のみ（pro 系に送ると 400 になる）
    if "flash" in GEMINI_MODEL.lower():
        gen_config["thinkingConfig"] = {
            "thinkingBudget": GEMINI_THINKING_BUDGET
        }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        req_timeout = 120
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[WARN] Gemini API deadline reached -- aborting retries")
                return {"error": "Retry deadline exceeded"}
            req_timeout = min(120, max(10, remaining))

        try:
            resp = requests.post(url, json=payload, timeout=req_timeout)

            if resp.status_code == 200:
                data = resp.json()
                text = extract_text_from_gemini_response(data)
                if text is None:
                    if attempt < GEMINI_MAX_RETRIES:
                        if not _can_retry_before_deadline(GEMINI_SLEEP_SEC, deadline):
                            print(f"[WARN] Gemini retry skipped (wait {GEMINI_SLEEP_SEC}s would exceed deadline)")
                            return {"error": "Retry deadline exceeded"}
                        print(f"[WARN] Gemini returned no usable text (attempt {attempt}/{GEMINI_MAX_RETRIES}), retrying in {GEMINI_SLEEP_SEC}s ...")
                        time.sleep(GEMINI_SLEEP_SEC)
                        continue
                    return {"error": "Gemini returned no usable text"}
                result = parse_json_safely(text)
                if result is None:
                    if attempt < GEMINI_MAX_RETRIES:
                        if not _can_retry_before_deadline(GEMINI_SLEEP_SEC, deadline):
                            print(f"[WARN] Gemini retry skipped (wait {GEMINI_SLEEP_SEC}s would exceed deadline)")
                            return {"error": "Retry deadline exceeded"}
                        print(f"[WARN] Gemini JSON parse failed (attempt {attempt}/{GEMINI_MAX_RETRIES}), retrying in {GEMINI_SLEEP_SEC}s ...")
                        time.sleep(GEMINI_SLEEP_SEC)
                        continue
                    return {"error": f"JSON parse failed: {text[:100]}"}
                return result

            elif resp.status_code == 429:
                if attempt < GEMINI_MAX_RETRIES:
                    wait = _calc_gemini_retry_wait(resp, attempt)
                    if not _can_retry_before_deadline(wait, deadline):
                        print(f"[WARN] 429 retry skipped (wait {wait:.1f}s would exceed deadline)")
                        return {"error": "Retry deadline exceeded"}
                    print(f"[WARN] 429 Rate limited (attempt {attempt}/{GEMINI_MAX_RETRIES}), waiting {wait:.1f}s ...")
                    time.sleep(wait)
                continue

            elif resp.status_code >= 500:
                if attempt < GEMINI_MAX_RETRIES:
                    wait = _calc_gemini_retry_wait(resp, attempt)
                    if not _can_retry_before_deadline(wait, deadline):
                        print(f"[WARN] {resp.status_code} retry skipped (wait {wait:.1f}s would exceed deadline)")
                        return {"error": "Retry deadline exceeded"}
                    print(f"[WARN] {resp.status_code} Server error (attempt {attempt}/{GEMINI_MAX_RETRIES}), waiting {wait:.1f}s ...")
                    time.sleep(wait)
                continue

            else:
                err_msg = f"{resp.status_code} {resp.text[:150]}"
                print(f"[ERROR] Gemini API returned {err_msg}")
                return {"error": err_msg}

        except requests.RequestException as e:
            print(f"[ERROR] Gemini API request failed: {e}")
            if attempt < GEMINI_MAX_RETRIES:
                wait = min(GEMINI_SLEEP_SEC * attempt, GEMINI_BACKOFF_MAX_SEC)
                if not _can_retry_before_deadline(wait, deadline):
                    print(f"[WARN] Gemini retry skipped (wait {wait}s would exceed deadline)")
                    return {"error": "Retry deadline exceeded"}
                print(f"[WARN] Retrying in {wait}s (attempt {attempt}/{GEMINI_MAX_RETRIES}) ...")
                time.sleep(wait)
            continue

    print(f"[ERROR] Gemini API: all {GEMINI_MAX_RETRIES} retries exhausted")
    return {"error": "All retries exhausted"}


def _mark_analysis_failed(art: dict, reason: str, status: str = "error") -> None:
    """解析失敗した記事にプレースホルダーを設定する"""
    art.update({
        "summary": "(解析中にエラーが発生しました)" if status == "error"
                   else "(解析スキップ)",
        "tags": [],
        "category": "その他",
        "score": 50,
        "score_reason": reason,
        "analysis_status": status,
    })


def load_today_existing_articles(now: datetime | None = None) -> list[dict]:
    """当日 (JST) すでに保存されている data.json / archive/YYYY-MM-DD.json の記事を読み込む"""
    now = now or datetime.now(JST)
    today_prefix = now.strftime("%Y-%m-%d")
    collected: list[dict] = []

    archive_file = os.path.join(ARCHIVE_DIR, f"{today_prefix}.json")
    for path in (DATA_FILE, archive_file):
        data = _load_json_file(path)
        if not data:
            continue
        if path == DATA_FILE and not str(data.get("generated_at", "")).startswith(today_prefix):
            continue
        if isinstance(data.get("articles"), list):
            collected.extend(data["articles"])
    return deduplicate_articles(collected)


def load_recent_past_articles(
    now: datetime | None = None,
    days: int = PAST_SEEN_DAYS,
) -> dict[str, dict]:
    """直近 days 日間（当日を除く）のアーカイブから解析済み記事を URL 正規化キーで返す"""
    now = now or datetime.now(JST)
    today = now.date()
    past_map: dict[str, dict] = {}
    if not os.path.exists(ARCHIVE_DIR):
        return past_map

    for d in range(1, days + 1):
        target_date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        data = _load_json_file(os.path.join(ARCHIVE_DIR, f"{target_date}.json"))
        if not data:
            continue
        fallback_ts = str(data.get("generated_at") or f"{target_date}T00:00:00+09:00")
        for art in data.get("articles", []):
            if art.get("analysis_status") != "ok":
                continue
            norm = normalize_url(art.get("url", ""))
            if norm and norm not in past_map:
                copied = dict(art)
                if not copied.get("new_at"):
                    copied["new_at"] = fallback_ts
                past_map[norm] = copied
    return past_map


def _backfill_github_repo(art: dict) -> None:
    """記事に github_repo が未設定なら URL / 本文から補完する"""
    if not art.get("github_repo"):
        extracted = extract_github_repo(art.get("url", ""), art.get("_body", ""))
        if extracted:
            art["github_repo"] = extracted


def reuse_analyzed_articles(
    fetched: list[dict],
    existing_today: list[dict],
    past_seen_map: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """当日または直近数日間に解析済み (analysis_status == 'ok') の記事を再利用し、未解析の記事のみを抽出する"""
    ok_by_url: dict[str, dict] = {
        normalize_url(a.get("url", "")): a
        for a in existing_today
        if a.get("analysis_status") == "ok" and normalize_url(a.get("url", ""))
    }
    past_map = past_seen_map or {}
    reused: list[dict] = []
    to_analyze: list[dict] = []
    for art in fetched:
        norm = normalize_url(art.get("url", ""))
        cached = ok_by_url.get(norm)
        if cached:
            merged = dict(cached)
            # HN スコア・コメント等の最新値は引き継ぐ
            for k in ("hn_score", "hn_comments", "published"):
                if k in art:
                    merged[k] = art[k]
            _backfill_github_repo(merged)
            reused.append(merged)
        elif norm in past_map:
            past_art = past_map[norm]
            merged = dict(past_art)
            for k in ("hn_score", "hn_comments", "published", "source", "url"):
                if k in art and art[k]:
                    merged[k] = art[k]
            merged["is_prev_seen"] = True
            merged["new_at"] = past_art.get("new_at") or past_art.get("first_seen") or "past"
            _backfill_github_repo(merged)
            reused.append(merged)
        else:
            to_analyze.append(art)
    return reused, to_analyze


def collect_today_retry_articles(
    existing_today: list[dict],
    already_queued: list[dict],
    max_retry: int = RETRY_FAILED_TODAY_MAX,
) -> list[dict]:
    """当日の前回実行で失敗 (error/skipped) したが今回フィードから外れた記事を再解析キューに戻す"""
    queued_norms = {
        normalize_url(a.get("url", ""))
        for a in already_queued
        if normalize_url(a.get("url", ""))
    }
    retries: list[dict] = []
    for art in existing_today:
        if len(retries) >= max_retry:
            break
        if art.get("analysis_status") not in ("error", "skipped"):
            continue
        norm = normalize_url(art.get("url", ""))
        if not norm or norm in queued_norms:
            continue
        queued_norms.add(norm)
        item = {
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "source": art.get("source", ""),
            "published": art.get("published", ""),
        }
        for k in ("hn_score", "hn_comments", "first_seen"):
            if k in art:
                item[k] = art[k]
        retries.append(item)
    return retries


def merge_today_articles(
    existing_today: list[dict],
    current_articles: list[dict],
    run_at: str,
) -> list[dict]:
    """当日の既存記事と今回の記事をマージする（朝のみ出現した記事も日次アーカイブに保持）"""
    by_url: dict[str, dict] = {}
    for old in existing_today:
        norm = normalize_url(old.get("url", ""))
        if norm:
            by_url[norm] = dict(old)

    for art in current_articles:
        norm = normalize_url(art.get("url", ""))
        if not norm:
            continue
        prev = by_url.get(norm)
        if prev and prev.get("analysis_status") == "ok":
            art["first_seen"] = prev.get("first_seen") or prev.get("new_at") or run_at
            art["new_at"] = prev.get("new_at") or prev.get("first_seen") or run_at
            if prev.get("is_prev_seen") and "is_prev_seen" not in art:
                art["is_prev_seen"] = True
        elif art.get("is_prev_seen"):
            art["first_seen"] = art.get("first_seen") or art.get("new_at") or run_at
            art["new_at"] = art.get("new_at") or "past"
        else:
            art["first_seen"] = (prev.get("first_seen") if prev else None) or run_at
            if art.get("analysis_status") == "ok":
                art["new_at"] = art.get("new_at") or run_at
        by_url[norm] = art

    return list(by_url.values())


def _apply_article_analysis(art: dict, item: dict, run_at: str | None) -> None:
    """検証済みのバッチ解析結果1件分を記事辞書へ反映する"""
    if item.get("title"):
        art["title"] = str(item["title"])
    art["summary"] = str(item.get("summary", ""))
    if item.get("actionable_takeaway"):
        art["actionable_takeaway"] = str(item["actionable_takeaway"]).strip()
    gh_repo = str(item.get("github_repo", "")).strip()
    if "/" in gh_repo:
        owner, repo = gh_repo.split("/", 1)
        normalized_repo = _normalize_github_repo(owner, repo)
        if normalized_repo:
            art["github_repo"] = normalized_repo
    _backfill_github_repo(art)
    art["tags"] = [str(t) for t in item.get("tags", [])][:5]
    category = str(item.get("category", ""))
    art["category"] = category if category in CATEGORIES else "その他"
    art["score"] = max(0, min(int(item.get("score", 50)), SCORE_MAX))
    art["score_reason"] = str(item.get("score_reason", ""))
    art["analysis_status"] = "ok"
    if run_at:
        art["new_at"] = run_at


def analyze_with_gemini(
    articles: list[dict],
    start_time: float = 0,
    run_at: str | None = None,
) -> list[dict]:
    """Gemini REST API で記事をバッチ解析する（全体タイムアウト対応）

    1リクエストで ANALYSIS_BATCH_SIZE 件をまとめて解析することで
    リクエスト数を大幅に削減し、レート制限 (15 RPM) との衝突を防ぐ。
    """
    if not articles:
        return articles

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[WARN] GEMINI_API_KEY not set -- skipping Gemini analysis")
        for art in articles:
            _mark_analysis_failed(art, "APIキー未設定のため解析未実施", status="skipped")
        return articles

    print(f"[INFO] Using Gemini model: {GEMINI_MODEL} "
          f"(batch size: {ANALYSIS_BATCH_SIZE})")

    chunks = [
        articles[i:i + ANALYSIS_BATCH_SIZE]
        for i in range(0, len(articles), ANALYSIS_BATCH_SIZE)
    ]

    deadline = (start_time + GLOBAL_TIMEOUT_SEC) if start_time else None

    for ci, chunk in enumerate(chunks):
        # 全体タイムアウトチェック
        if start_time and (time.monotonic() - start_time) > GLOBAL_TIMEOUT_SEC:
            remaining = sum(len(c) for c in chunks[ci:])
            print(f"[WARN] Global timeout ({GLOBAL_TIMEOUT_SEC}s) reached. "
                  f"Skipping remaining {remaining} articles.")
            for c in chunks[ci:]:
                for art in c:
                    _mark_analysis_failed(art, "タイムアウトのため解析スキップ",
                                          status="skipped")
            break

        print(f"[INFO] Analyzing batch {ci + 1}/{len(chunks)} "
              f"({len(chunk)} articles) ...")
        result = call_gemini_rest(
            build_batch_prompt(chunk),
            api_key,
            response_schema=BATCH_ANALYSIS_SCHEMA,
            deadline=deadline,
        )

        if isinstance(result, list):
            by_id = {
                item["id"]: item for item in result
                if validate_analysis_item(item)
            }
            for i, art in enumerate(chunk):
                item = by_id.get(i)
                if item:
                    _apply_article_analysis(art, item, run_at)
                else:
                    _mark_analysis_failed(art, "解析エラー: バッチ応答に含まれず")
        else:
            err_detail = result.get("error", "不明") if isinstance(result, dict) else "不正な応答形式"
            for art in chunk:
                _mark_analysis_failed(art, f"解析エラー: {err_detail}")

        if ci < len(chunks) - 1:
            time.sleep(GEMINI_SLEEP_SEC)

    ok = sum(1 for a in articles if a.get("analysis_status") == "ok")
    elapsed = time.monotonic() - start_time if start_time else 0
    print(f"[INFO] Analysis complete: {ok}/{len(articles)} succeeded "
          f"(elapsed since start: {elapsed:.1f}s)")
    return articles


# ---------------------------------------------------------------------------
# インテリジェント・スコアリング
# ---------------------------------------------------------------------------
def _detect_hot_urls(articles: list[dict]) -> set[str]:
    """複数ソースに出現した正規化 URL 集合を返す（ソース未設定時は重複出現で判定）"""
    url_counts: dict[str, int] = {}
    url_sources: dict[str, set[str]] = {}
    has_sources = False
    for art in articles:
        norm = normalize_url(art.get("url", ""))
        if not norm:
            continue
        url_counts[norm] = url_counts.get(norm, 0) + 1
        src = str(art.get("source") or "").strip()
        if src:
            has_sources = True
            url_sources.setdefault(norm, set()).add(src)
    if has_sources:
        return {url for url, srcs in url_sources.items() if len(srcs) >= 2}
    return {url for url, cnt in url_counts.items() if cnt > 1}


def _apply_hot_flags(
    articles: list[dict],
    hot_urls: set[str],
    preserve_existing: bool = False,
) -> None:
    """HOT 対象 URL に該当する記事へ is_hot フラグ付与およびスコア加算を行う"""
    for art in articles:
        norm = normalize_url(art.get("url", ""))
        status = art.get("analysis_status", "ok")
        eligible = (status == "ok" and not art.get("is_hot")) if preserve_existing else (status not in ("error", "skipped"))
        if norm in hot_urls and eligible:
            art["is_hot"] = True
            art["score"] = min(art.get("score", 50) + SCORE_HOT_BONUS, SCORE_MAX)
        elif not preserve_existing or "is_hot" not in art:
            art["is_hot"] = False


def apply_hot_scoring(articles: list[dict]) -> list[dict]:
    """複数ソースに出現する URL を検知し、スコア加算 & is_hot フラグ付与"""
    hot_urls = _detect_hot_urls(articles)
    _apply_hot_flags(articles, hot_urls, preserve_existing=False)
    return articles


# ---------------------------------------------------------------------------
# 関連記事グルーピング（埋め込みベクトル）
# ---------------------------------------------------------------------------
def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _title_tokens(title: str) -> set[str]:
    """タイトルを比較用トークン集合に変換する（埋め込み失敗時のフォールバック用）"""
    tokens = re.findall(r"[A-Za-z0-9]+|[一-鿿゠-ヿ]{2,}", title.lower())
    return {t for t in tokens if len(t) >= 2}


def fetch_embeddings(texts: list[str], api_key: str) -> list[list[float]] | None:
    """Gemini batchEmbedContents で埋め込みベクトルを一括取得する（上限100件対策でチャンク分割）"""
    url = GEMINI_EMBED_URL.format(model=GEMINI_EMBED_MODEL, key=api_key)
    all_values: list[list[float]] = []
    max_attempts = 4  # 初回 + 最大3回リトライ
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        chunk = texts[i:i + EMBED_BATCH_SIZE]
        payload = {
            "requests": [
                {
                    "model": f"models/{GEMINI_EMBED_MODEL}",
                    "content": {"parts": [{"text": t[:1000]}]},
                    "outputDimensionality": EMBED_DIMENSIONS,
                }
                for t in chunk
            ]
        }
        chunk_values: list[list[float]] | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(url, json=payload, timeout=120)
                if resp.status_code == 200:
                    embeddings = resp.json().get("embeddings", [])
                    if len(embeddings) != len(chunk):
                        print("[WARN] Embedding count mismatch")
                        return None
                    chunk_values = [e.get("values", []) for e in embeddings]
                    break
                if resp.status_code == 429 and attempt < max_attempts:
                    wait = _calc_gemini_retry_wait(resp, attempt)
                    print(f"[WARN] Embedding 429 (attempt {attempt}/{max_attempts}), waiting {wait:.1f}s ...")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500 and attempt < max_attempts:
                    wait = _calc_gemini_retry_wait(resp, attempt)
                    print(f"[WARN] Embedding {resp.status_code} (attempt {attempt}/{max_attempts}), waiting {wait:.1f}s ...")
                    time.sleep(wait)
                    continue
                print(f"[WARN] Embedding API returned {resp.status_code}: {resp.text[:120]}")
                return None
            except (requests.RequestException, ValueError) as e:
                print(f"[WARN] Embedding request failed (attempt {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    wait = min(GEMINI_SLEEP_SEC * (2 ** attempt), GEMINI_BACKOFF_MAX_SEC)
                    time.sleep(wait)
                    continue
                return None
        if chunk_values is None:
            return None
        all_values.extend(chunk_values)
        if i + EMBED_BATCH_SIZE < len(texts):
            time.sleep(GEMINI_SLEEP_SEC)
    return all_values


def apply_topic_grouping(articles: list[dict]) -> list[dict]:
    """URL が異なる同一話題の記事を埋め込み類似度でグルーピングする。

    埋め込み取得に失敗した場合はタイトルトークンの Jaccard 係数で代替。
    グループに属する記事へ topic_group (int) と topic_size を付与する。
    """
    n = len(articles)
    if n < 2:
        return articles

    api_key = os.environ.get("GEMINI_API_KEY", "")
    pairs_similar: list[tuple[int, int]] = []

    vectors = None
    if api_key:
        texts = [
            f"{a.get('title', '')}\n{(a.get('summary') or '')[:200]}"
            for a in articles
        ]
        vectors = fetch_embeddings(texts, api_key)

    if vectors:
        print("[INFO] Topic grouping: using embeddings "
              f"({GEMINI_EMBED_MODEL}, dim={EMBED_DIMENSIONS})")
        for i in range(n):
            for j in range(i + 1, n):
                if _cosine(vectors[i], vectors[j]) >= TOPIC_SIM_THRESHOLD:
                    pairs_similar.append((i, j))
    else:
        print("[INFO] Topic grouping: falling back to title-token Jaccard")
        token_sets = [_title_tokens(a.get("title", "")) for a in articles]
        for i in range(n):
            for j in range(i + 1, n):
                si, sj = token_sets[i], token_sets[j]
                if not si or not sj:
                    continue
                jaccard = len(si & sj) / len(si | sj)
                if jaccard >= 0.5:
                    pairs_similar.append((i, j))

    # Union-Find でグループ化
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in pairs_similar:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    group_id = 0
    grouped_count = 0
    for members in groups.values():
        if len(members) > 1:
            group_id += 1
            for idx in members:
                articles[idx]["topic_group"] = group_id
                articles[idx]["topic_size"] = len(members)
            grouped_count += len(members)

    print(f"[INFO] Topic grouping: {group_id} groups, {grouped_count} articles grouped")
    return articles


# ---------------------------------------------------------------------------
# 通知（Discord / Slack / LINE）
# ---------------------------------------------------------------------------
def pick_top_articles(articles: list[dict]) -> list[dict]:
    """通知対象（スコア閾値以上の上位 N 件）を抽出する"""
    return sorted(
        [a for a in articles if a.get("score", 0) >= NOTIFY_SCORE_THRESHOLD],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:NOTIFY_TOP_N]


def _embed_char_count(embed: dict) -> int:
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    footer = embed.get("footer") or {}
    total += len(str(footer.get("text", "")))
    for f in embed.get("fields", []):
        total += len(str(f.get("name", ""))) + len(str(f.get("value", "")))
    return total


def send_discord_notification(articles: list[dict]) -> None:
    """スコア上位の重要記事を Discord Webhook で通知"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("[INFO] DISCORD_WEBHOOK_URL not set -- skipping Discord")
        return

    top_articles = pick_top_articles(articles)
    if not top_articles:
        print("[INFO] No articles above score threshold -- skipping Discord")
        return

    embeds = []
    total_chars = 0
    for art in top_articles:
        score = art.get("score", 0)
        color = 0xFFD700 if score >= 80 else 0x8E8E93
        if score >= 90:
            color = 0xFF4500

        tags_str = (" / ".join(art.get("tags", [])))[:256]
        hot_prefix = "[HOT] " if art.get("is_hot") else ""

        fields = [
            {"name": "Score", "value": str(score), "inline": True},
            {"name": "Tags", "value": tags_str or "-", "inline": True},
            {"name": "Source", "value": str(art.get("source", "") or "-")[:256], "inline": True},
        ]
        if art.get("github_repo"):
            fields.append({"name": "GitHub", "value": f"[{art['github_repo']}](https://github.com/{art['github_repo']})"[:1024], "inline": True})
        if art.get("actionable_takeaway"):
            fields.append({"name": "実務ポイント", "value": str(art["actionable_takeaway"])[:500], "inline": False})
        fields.append({"name": "Reason", "value": (str(art.get("score_reason", ""))[:500] or "-"), "inline": False})

        embed = {
            "title": f"{hot_prefix}{art.get('title', 'No Title')}"[:256],
            "url": art.get("url", ""),
            "color": color,
            "description": str(art.get("summary", "")).replace("\\n", "\n")[:800],
            "fields": fields,
            "footer": {"text": "IT Info Collector"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        embed_len = _embed_char_count(embed)
        if embeds and total_chars + embed_len > 5500:
            break
        embeds.append(embed)
        total_chars += embed_len

    payload = {
        "username": "IT Info Collector",
        "content": f"## 本日の最重要IT記事 TOP {len(top_articles)}",
        "embeds": embeds,
    }
    post_webhook(webhook_url, payload, f"Discord notification ({len(top_articles)} articles)")


def send_slack_notification(articles: list[dict]) -> None:
    """スコア上位の重要記事を Slack Incoming Webhook で通知"""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("[INFO] SLACK_WEBHOOK_URL not set -- skipping Slack")
        return

    top_articles = pick_top_articles(articles)
    if not top_articles:
        return

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text",
                 "text": f"本日の最重要IT記事 TOP {len(top_articles)}"},
    }]
    for art in top_articles:
        hot = "[HOT] " if art.get("is_hot") else ""
        summary = art.get("summary", "").replace("\\n", "\n")
        takeaway_line = f"\n*実務示唆*: {art['actionable_takeaway']}" if art.get("actionable_takeaway") else ""
        gh_line = f" · <https://github.com/{art['github_repo']}|GitHub: {art['github_repo']}>" if art.get("github_repo") else ""
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*<{art.get('url', '')}|{hot}{art.get('title', 'No Title')}>*"
                         f"  `{art.get('score', 0)}点`\n{summary}{takeaway_line}\n"
                         f"_{art.get('source', '')}{gh_line} / "
                         f"{' · '.join(art.get('tags', []))}_"),
            },
        })

    post_webhook(webhook_url, {"blocks": blocks[:50]}, f"Slack notification ({len(top_articles)} articles)")


def send_line_notification(articles: list[dict]) -> None:
    """スコア上位の重要記事を LINE Messaging API (broadcast) で通知

    ※ LINE Notify は 2025 年 3 月に終了したため Messaging API を使用。
    LINE_CHANNEL_ACCESS_TOKEN（チャネルアクセストークン）が必要。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("[INFO] LINE_CHANNEL_ACCESS_TOKEN not set -- skipping LINE")
        return

    top_articles = pick_top_articles(articles)[:5]
    if not top_articles:
        return

    lines = [f"本日の最重要IT記事 TOP {len(top_articles)}", ""]
    for art in top_articles:
        hot = "[HOT]" if art.get("is_hot") else "・"
        lines.append(f"{hot} [{art.get('score', 0)}点] {art.get('title', '')}")
        if art.get("actionable_takeaway"):
            lines.append(f"実務示唆: {art['actionable_takeaway']}")
        if art.get("github_repo"):
            lines.append(f"GH: https://github.com/{art['github_repo']}")
        lines.append(art.get("url", ""))
        lines.append("")
    text = "\n".join(lines)[:4900]

    post_webhook(
        "https://api.line.me/v2/bot/message/broadcast",
        {"messages": [{"type": "text", "text": text}]},
        f"LINE notification ({len(top_articles)} articles)",
        headers={"Authorization": f"Bearer {token}"},
    )


def wait_until_notify_time(
    now: datetime | None = None,
    max_wait_sec: float | None = None,
) -> float:
    """環境変数 NOTIFY_AT_JST (HH:MM または カンマ区切り HH:MM,HH:MM) の時刻まで待機する。

    GitHub Actions の schedule は遅延するため、早めに起動して通知時刻を揃える。
    複数指定時は現在時刻以降の最も近い目標時刻まで待機する。
    ただし、目標時刻までの待機時間が max_wait_sec (環境変数 NOTIFY_MAX_WAIT_SEC、
    デフォルト 3600秒 = 1時間) を超える場合は、遅延起動や時間外実行と判断して待たずに即時通知する。
    未設定なら待たない。戻り値は待機秒数。
    """
    notify_at = os.environ.get("NOTIFY_AT_JST", "").strip()
    if not notify_at:
        return 0.0

    if max_wait_sec is None:
        try:
            max_wait_sec = float(os.environ.get("NOTIFY_MAX_WAIT_SEC", "3600"))
        except ValueError:
            max_wait_sec = 3600.0

    slept, _ = wait_until_jst(
        notify_at,
        now=now,
        max_wait_sec=max_wait_sec,
        sleep_fn=time.sleep,
    )
    return slept


def send_notifications(articles: list[dict]) -> None:
    """全通知チャネルへ送信"""
    send_discord_notification(articles)
    send_slack_notification(articles)
    send_line_notification(articles)


# ---------------------------------------------------------------------------
# 週間ダイジェスト（日曜のみ）
# ---------------------------------------------------------------------------
DIGEST_PROMPT = """\
あなたはIT技術ニュースの編集者です。以下は直近1週間の重要IT記事のリストです。

{articles_block}

この1週間のITニュースを総括する「週間ダイジェスト」を作成し、JSON形式で回答してください:
- overview: 今週の全体傾向を日本語3〜4文で総括
- trends: 今週の注目トレンドを3〜5個の短いフレーズのリスト
- highlights: 特に重要な記事5件。各要素は {{"title": "...", "url": "...", "comment": "選定理由を1文"}}

回答は以下のJSON形式**のみ**出力してください:
{{"overview": "...", "trends": ["...", "..."], "highlights": [{{"title": "...", "url": "...", "comment": "..."}}]}}
"""


def load_recent_archives(days: int = 7) -> list[dict]:
    """直近 N 日分のアーカイブから記事を集めて重複除去する"""
    today = datetime.now(JST).date()
    collected: list[dict] = []
    for d in range(days):
        fname = os.path.join(
            ARCHIVE_DIR, f"{(today - timedelta(days=d)).strftime('%Y-%m-%d')}.json"
        )
        data = _load_json_file(fname, warn_prefix="Failed to read archive")
        if data:
            collected.extend(data.get("articles", []))
    return deduplicate_articles(collected)


def generate_weekly_digest(
    current_articles: list[dict],
    run_at: str | None = None,
) -> dict | None:
    """日曜日に直近7日間の高スコア記事から週間ダイジェストを生成する"""
    now = datetime.now(JST)
    if now.weekday() != 6 and os.environ.get("FORCE_DIGEST") != "1":
        return None
    if load_today_digest() is not None and os.environ.get("FORCE_DIGEST") != "1":
        print("[INFO] Weekly digest already generated today -- skipping")
        return None

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[INFO] Weekly digest skipped (no API key)")
        return None

    print("[INFO] Generating weekly digest ...")
    week_articles = load_recent_archives(7)
    # 今日の記事はまだアーカイブに無い可能性があるためマージ
    week_articles = deduplicate_articles(week_articles + current_articles)
    top = sorted(
        [a for a in week_articles if a.get("score", 0) >= 70],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:25]

    if len(top) < 3:
        print("[INFO] Weekly digest skipped (not enough high-score articles)")
        return None

    blocks = [
        f"- [{a.get('score', 0)}点] {a.get('title', '')} ({a.get('source', '')})\n"
        f"  URL: {a.get('url', '')}\n  要約: {(a.get('summary') or '').replace(chr(10), ' ')[:150]}"
        for a in top
    ]
    prompt = DIGEST_PROMPT.format(articles_block="\n".join(blocks))

    time.sleep(GEMINI_SLEEP_SEC)
    result = call_gemini_rest(prompt, api_key, response_schema=DIGEST_RESPONSE_SCHEMA)
    if not isinstance(result, dict) or "overview" not in result:
        print("[WARN] Weekly digest generation failed")
        return None

    digest = {
        "generated_at": run_at or now.isoformat(),
        "week_start": (now - timedelta(days=6)).strftime("%Y-%m-%d"),
        "week_end": now.strftime("%Y-%m-%d"),
        "article_count": len(week_articles),
        "overview": str(result.get("overview", "")),
        "trends": [str(t) for t in result.get("trends", [])][:5],
        "highlights": [
            {
                "title": str(h.get("title", "")),
                "url": str(h.get("url", "")),
                "comment": str(h.get("comment", "")),
            }
            for h in result.get("highlights", [])
            if isinstance(h, dict)
        ][:5],
    }

    with open(DIGEST_FILE, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {DIGEST_FILE}")
    return digest


def load_today_digest(run_at: str | None = None) -> dict | None:
    """digest.json が今日 (JST) 生成されたものなら返す（run_at 指定時は該当実行分のみ）"""
    digest = _load_json_file(DIGEST_FILE)
    if not digest:
        return None
    today = datetime.now(JST).strftime("%Y-%m-%d")
    gen_at = str(digest.get("generated_at", ""))
    if not gen_at.startswith(today):
        return None
    if run_at and gen_at != run_at:
        return None
    return digest


def send_digest_notification(digest: dict) -> None:
    """週間ダイジェストを Discord に通知"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook_url:
        # digest.json から読み込む場合もあるため欠損キーに備えて get を使う
        trends_str = "\n".join(f"・{t}" for t in digest.get("trends", []))
        highlights_str = "\n".join(
            f"[{h.get('title', '')}]({h.get('url', '')})\n└ {h.get('comment', '')}"
            for h in digest.get("highlights", [])
        )
        payload = {
            "username": "IT Info Collector",
            "embeds": [{
                "title": f"週間ダイジェスト ({digest.get('week_start', '')} 〜 {digest.get('week_end', '')})",
                "color": 0x2EAADC,
                "description": str(digest.get("overview", ""))[:2000] or "-",
                "fields": [
                    {"name": "今週のトレンド", "value": trends_str[:1024] or "-"},
                    {"name": "ハイライト", "value": highlights_str[:1024] or "-"},
                ],
            }],
        }
        post_webhook(webhook_url, payload, "Weekly digest Discord")


# ---------------------------------------------------------------------------
# RSS フィード出力
# ---------------------------------------------------------------------------
def build_rss_feed_xml(
    title: str,
    description: str,
    feed_url: str,
    items: list[dict],
    now: datetime,
) -> str:
    """RSS 2.0 XML 文字列を構築する（title/url/description/published/category を受け取る）"""
    rss_items: list[str] = []
    for item in items:
        pub_date = format_pub_date(item.get("published", ""))
        url = xml_escape(item.get("url", ""))
        cat = item.get("category")
        cat_tag = f"      <category>{xml_escape(str(cat))}</category>\n" if cat else ""
        rss_items.append(
            "    <item>\n"
            f"      <title>{xml_escape(item.get('title', ''))}</title>\n"
            f"      <link>{url}</link>\n"
            f"      <guid isPermaLink=\"false\">{url}</guid>\n"
            f"{cat_tag}"
            f"      <description>{xml_escape(item.get('description', ''))}</description>\n"
            f"{pub_date}"
            "    </item>"
        )

    items_block = "\n".join(rss_items) + "\n" if rss_items else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>{xml_escape(title)}</title>\n"
        f"    <link>{xml_escape(feed_url)}</link>\n"
        f"    <description>{xml_escape(description)}</description>\n"
        "    <language>ja</language>\n"
        f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n"
        f"{items_block}"
        "  </channel>\n"
        "</rss>\n"
    )


def _format_rss_channel(
    title: str,
    description: str,
    feed_url: str,
    articles: list[dict],
    now: datetime,
) -> str:
    """RSS 2.0 XML チャンネル文字列を構築する"""
    items: list[dict] = []
    for art in articles:
        summary = (art.get("summary") or "").replace("\\n", " / ").replace("\n", " / ")
        takeaway = f" [実務示唆: {art['actionable_takeaway']}]" if art.get("actionable_takeaway") else ""
        repo = f" [GitHub: {art['github_repo']}]" if art.get("github_repo") else ""
        items.append({
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "description": f"[Score: {art.get('score', 0)}] {summary}{takeaway}{repo} (Source: {art.get('source', '')})",
            "published": art.get("published", ""),
        })
    return build_rss_feed_xml(title, description, feed_url, items, now)


def generate_rss_feed(articles: list[dict]) -> None:
    """収集結果を総合およびカテゴリ別の RSS 2.0 フィードとして出力する (E-2)"""
    now = datetime.now(timezone.utc)
    # 総合フィード (feed.xml)
    top = sorted(articles, key=lambda x: x.get("score", 0), reverse=True)[:FEED_TOP_N]
    xml_overall = _format_rss_channel(
        title="IT Info Hub — AI技術ニュースダッシュボード",
        description="IT技術情報をAIで自動収集・分析し、重要度スコア付きで配信",
        feed_url=SITE_URL,
        articles=top,
        now=now,
    )
    with open(FEED_FILE, "w", encoding="utf-8") as f:
        f.write(xml_overall)
    print(f"[INFO] Saved {FEED_FILE} ({len(top)} items)")

    # カテゴリ別フィード (feed_ai.xml, feed_security.xml, feed_web.xml)
    for filename, conf in CATEGORY_FEEDS.items():
        allowed_cats = set(conf.get("categories", []))
        top_n = conf.get("top_n", 15)
        cat_articles = [
            a for a in articles
            if a.get("category") in allowed_cats
        ]
        cat_top = sorted(cat_articles, key=lambda x: x.get("score", 0), reverse=True)[:top_n]
        cat_xml = _format_rss_channel(
            title=conf["title"],
            description=conf["description"],
            feed_url=f"{SITE_URL.rstrip('/')}/{filename}",
            articles=cat_top,
            now=now,
        )
        with open(filename, "w", encoding="utf-8") as f:
            f.write(cat_xml)
        print(f"[INFO] Saved {filename} ({len(cat_top)} items)")


# ---------------------------------------------------------------------------
# ファイル出力
# ---------------------------------------------------------------------------
def rotate_archives() -> None:
    """保持期間を超えた古いアーカイブを削除する"""
    cutoff = datetime.now(JST).date() - timedelta(days=ARCHIVE_KEEP_DAYS)
    removed = 0
    for fn in _list_archive_date_files(ARCHIVE_DIR, reverse=False):
        m = _ARCHIVE_DATE_RE.match(fn)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            os.remove(os.path.join(ARCHIVE_DIR, fn))
            removed += 1
    if removed:
        print(f"[INFO] Archive rotation: removed {removed} files "
              f"(older than {ARCHIVE_KEEP_DAYS} days)")


def _strip_emojis(text: str) -> str:
    """文字列から絵文字コードポイントを除去する"""
    return re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F]",
        "",
        text or "",
    ).strip()


def generate_daily_briefing(
    articles: list[dict],
    run_at: str | None = None,
    sleep_before: bool = False,
) -> dict | None:
    """当日の上位記事から3行ブリーフィングと注目トピックタグを生成する"""
    ok_articles = sorted(
        [a for a in articles if a.get("analysis_status") == "ok" and not a.get("is_prev_seen")],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:10]
    if not ok_articles:
        ok_articles = sorted(
            [a for a in articles if a.get("analysis_status") == "ok"],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )[:10]
    if not ok_articles:
        return None

    now_dt = datetime.now(JST)
    if run_at:
        parsed_run_at = parse_iso_datetime(run_at, default_tz=JST)
        if parsed_run_at is not None:
            now_dt = parsed_run_at.astimezone(JST)
    edition = "朝刊" if now_dt.hour < 12 else "夕刊"

    # 頻出タグ上位をフォールバック用に集計
    tag_freq: dict[str, int] = {}
    for a in ok_articles:
        for t in a.get("tags", []):
            ts = str(t).strip()
            if ts:
                tag_freq[ts] = tag_freq.get(ts, 0) + 1
    fallback_tags = [
        k for k, _ in sorted(tag_freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        if sleep_before:
            time.sleep(GEMINI_SLEEP_SEC)
        blocks = [
            f"- [{a.get('score', 0)}点 / {a.get('category', '')}] {a.get('title', '')} "
            f"(タグ: {', '.join(str(t) for t in a.get('tags', []))})"
            for a in ok_articles
        ]
        prompt = (
            f"あなたはIT技術ニュースの編集長です。以下の上位記事リストから、"
            f"本日の「{edition}ブリーフィング」を作成してください。絵文字は一切使用しないでください。\n\n"
            f"{chr(10).join(blocks)}\n\n"
            f"以下のJSON形式で出力してください:\n"
            f"- edition: \"{edition}\"\n"
            f"- summary_lines: 今日の技術トレンド・注目動向を端的にまとめた日本語の箇条書き3文のリスト（各60文字前後、絵文字禁止）\n"
            f"- focus_tags: 注目キーワード・技術タグを3〜5個のリスト（絵文字禁止）"
        )
        result = call_gemini_rest(prompt, api_key, response_schema=BRIEFING_SCHEMA)
        if isinstance(result, dict) and isinstance(result.get("summary_lines"), list):
            lines = [_strip_emojis(str(s)) for s in result["summary_lines"] if str(s).strip()][:3]
            tags = [_strip_emojis(str(t)) for t in result.get("focus_tags", []) if str(t).strip()][:5]
            if lines:
                return {
                    "edition": edition,
                    "generated_at": run_at or now_dt.isoformat(),
                    "summary_lines": lines,
                    "focus_tags": tags or fallback_tags,
                }

    # APIキー未設定または呼び出し失敗時の決定論的フォールバック
    fallback_lines = [
        _strip_emojis(f"{a.get('title', '')}（{a.get('source', '')}・スコア{a.get('score', 0)}）")
        for a in ok_articles[:3]
    ]
    return {
        "edition": edition,
        "generated_at": run_at or now_dt.isoformat(),
        "summary_lines": fallback_lines,
        "focus_tags": fallback_tags,
    }


def build_search_index(archive_dir: str = ARCHIVE_DIR, out_file: str | None = None) -> dict:
    """全アーカイブを重複除去して1つにまとめた軽量検索インデックス (archive/search_index.json) を生成する"""
    out_file = out_file or os.path.join(archive_dir, "search_index.json")
    date_files = _list_archive_date_files(archive_dir, reverse=True)
    if not date_files and not os.path.exists(archive_dir):
        return {"days_count": 0, "total_count": 0, "articles": []}

    seen: set[str] = set()
    merged: list[dict] = []
    keep_keys = (
        "title", "url", "source", "published", "summary", "tags", "category",
        "score", "score_reason", "analysis_status", "is_hot", "hn_score",
        "hn_comments", "topic_group", "topic_size", "hatena_count",
        "reading_min", "is_prev_seen", "actionable_takeaway", "github_repo",
    )
    for fn in date_files:
        date_prefix = fn[:-5]
        data = _load_json_file(os.path.join(archive_dir, fn))
        if not data:
            continue
        for art in data.get("articles", []):
            norm = normalize_url(art.get("url", ""))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            compact = {k: art[k] for k in keep_keys if k in art}
            if compact.get("topic_group") is not None and "#" not in str(compact["topic_group"]):
                compact["topic_group"] = f"{date_prefix}#{compact['topic_group']}"
            merged.append(compact)

    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "days_count": len(date_files),
        "total_count": len(merged),
        "articles": merged,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[INFO] Updated {out_file} ({len(merged)} unique articles across {len(date_files)} days)")
    return payload


def build_trends_summary(
    archive_dir: str = ARCHIVE_DIR,
    out_file: str | None = None,
    days: int = 30,
) -> dict:
    """直近 N 日分のトレンド統計 (archive/trends_30d.json) を事前生成する"""
    out_file = out_file or os.path.join(archive_dir, "trends_30d.json")
    if not os.path.exists(archive_dir):
        return {"days_count": 0, "daily": [], "topTags": [], "sources": [], "categories": []}

    date_files = _list_archive_date_files(archive_dir, reverse=True)[:days]
    date_files.reverse()  # 古い順

    daily: list[dict] = []
    tag_counts: dict[str, int] = {}
    source_stats: dict[str, list[float]] = {}  # [sum, count]
    cat_counts: dict[str, int] = {}

    for fn in date_files:
        data = _load_json_file(os.path.join(archive_dir, fn))
        if not data:
            continue
        articles = data.get("articles", [])
        daily.append({"label": fn[:-5][5:], "value": len(articles)})
        for a in articles:
            for t in a.get("tags", []):
                key = str(t).strip()
                if key:
                    tag_counts[key] = tag_counts.get(key, 0) + 1
            src = a.get("source") or "不明"
            st = source_stats.setdefault(src, [0.0, 0.0])
            st[0] += float(a.get("score") or 0)
            st[1] += 1.0
            cat = a.get("category") or "その他"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1

    top_tags = [
        {"label": k, "value": v}
        for k, v in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ]
    sources = [
        {"label": k, "value": round(s[0] / s[1], 2), "display": f"{s[0] / s[1]:.1f}"}
        for k, s in sorted(source_stats.items(), key=lambda kv: -(kv[1][0] / kv[1][1]))
        if s[1] > 0
    ]
    categories = [
        {"label": k, "value": v, "display": f"{v}件"}
        for k, v in sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    payload = {
        "generated_at": datetime.now(JST).isoformat(),
        "days_count": len(date_files),
        "daily": daily,
        "topTags": top_tags,
        "sources": sources,
        "categories": categories,
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[INFO] Updated {out_file} ({len(daily)} days)")
    return payload


def save_results(
    articles: list[dict],
    run_at: str | None = None,
    briefing: dict | None = None,
) -> str:
    """data.json と archive/YYYY-MM-DD.json を生成し、生成時刻 (ISO文字列) を返す"""
    now = datetime.now(JST)
    target_dt = now
    if run_at:
        parsed_run_at = parse_iso_datetime(run_at)
        if parsed_run_at is not None:
            target_dt = parsed_run_at.astimezone(JST)
    generated_at = run_at or now.isoformat()

    # 一時フィールド (_body) を出力から除去
    for art in articles:
        art.pop("_body", None)

    error_count = sum(
        1 for a in articles if a.get("analysis_status") in ("error", "skipped")
    )
    success_count = len(articles) - error_count

    output: dict = {
        "schema_version": 2,
        "generated_at": generated_at,
        "total_count": len(articles),
        "success_count": success_count,
        "error_count": error_count,
        "gemini_model": GEMINI_MODEL,
        "articles": sorted(articles, key=lambda x: x.get("score", 0), reverse=True),
    }
    if briefing:
        output["briefing"] = briefing

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {DATA_FILE} ({len(articles)} articles, {error_count} errors)")

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_file = os.path.join(ARCHIVE_DIR, f"{target_dt.strftime('%Y-%m-%d')}.json")
    with open(archive_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {archive_file}")

    rotate_archives()

    archive_files = _list_archive_date_files(ARCHIVE_DIR, reverse=True)
    with open(os.path.join(ARCHIVE_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(archive_files, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Updated archive/index.json ({len(archive_files)} files)")

    build_search_index(ARCHIVE_DIR, SEARCH_INDEX_FILE)
    build_trends_summary(ARCHIVE_DIR, TRENDS_FILE)
    return generated_at


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def is_generated_today(data: dict, now: datetime | None = None) -> bool:
    """data.json が今日 (JST) 生成されたものか。

    収集に失敗した日に、リポジトリに残る前日分で同じ通知を再送しないためのガード。
    """
    now = now or datetime.now(JST)
    return str(data.get("generated_at", "")).startswith(now.strftime("%Y-%m-%d"))


def notify_from_saved(now: datetime | None = None) -> None:
    """保存済みの data.json / digest.json から通知だけを行う（今回新規解析された記事のみ対象）"""
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[ERROR] Failed to read {DATA_FILE}: {e}")
        sys.exit(1)

    articles = data.get("articles", [])
    run_at = data.get("generated_at", "")
    if run_at and not is_generated_today(data, now=now):
        print(f"[WARN] {DATA_FILE} is not from today (generated_at: {run_at}) -- skipping notifications")
        return
    # new_at がある場合は今回の実行 (run_at) で新規解析された記事のみ通知対象にする（旧形式は全件対象）
    fresh_articles = [
        a for a in articles
        if (not run_at or not a.get("new_at") or a.get("new_at") == run_at)
        and a.get("analysis_status", "ok") == "ok"
    ]
    print(f"[INFO] Loaded {len(articles)} articles ({len(fresh_articles)} new in this run, "
          f"generated_at: {run_at or '?'})")

    wait_until_notify_time()
    send_notifications(fresh_articles)
    digest = load_today_digest(run_at=run_at or None)
    if digest:
        send_digest_notification(digest)


def main() -> None:
    parser = argparse.ArgumentParser(description="IT Info Collector")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-notify", action="store_true",
                      help="収集・保存のみ行い通知しない")
    mode.add_argument("--notify-only", action="store_true",
                      help="保存済みデータから通知のみ行う")
    args = parser.parse_args()

    if args.notify_only:
        notify_from_saved()
        return

    start_time = time.monotonic()
    now = datetime.now(JST)
    run_at = now.isoformat()

    print("=" * 60)
    print("IT Info Collector -- Start")
    print(f"Time: {run_at}")
    print(f"Gemini model: {GEMINI_MODEL}")
    print(f"Global timeout: {GLOBAL_TIMEOUT_SEC}s ({GLOBAL_TIMEOUT_SEC // 60}min)")
    print("=" * 60)

    # 1. データ取得
    hn_articles = fetch_hackernews()
    rss_articles = fetch_rss_feeds()
    all_articles = hn_articles + rss_articles
    print(f"\n[INFO] Total raw articles: {len(all_articles)}")

    if not all_articles:
        print("[ERROR] No articles fetched from any source -- aborting")
        sys.exit(1)

    # 2. 複数ソース出現の検知用に当日既存分＋今回取得分で重複除去前 URL 別ソース集合を記録
    existing_today = load_today_existing_articles(now)
    hot_norms = _detect_hot_urls(existing_today + all_articles)

    before_dedup = len(all_articles)
    all_articles = deduplicate_articles(all_articles)
    if before_dedup != len(all_articles):
        print(f"[INFO] Deduplicated: {before_dedup} -> {len(all_articles)} articles")

    # 3. 当日および直近数日間に解析済みの記事があれば再利用（1日2回実行時の差分解析・既出抑制）
    past_seen_map = load_recent_past_articles(now)
    reused_articles, to_analyze = reuse_analyzed_articles(
        all_articles, existing_today, past_seen_map
    )
    retry_failed = collect_today_retry_articles(
        existing_today, reused_articles + to_analyze
    )
    if retry_failed:
        print(f"[INFO] Re-queueing {len(retry_failed)} previously failed articles from today")
        to_analyze.extend(retry_failed)
    if reused_articles:
        prev_cnt = sum(1 for a in reused_articles if a.get("is_prev_seen"))
        print(f"[INFO] Reused {len(reused_articles)} already-analyzed articles "
              f"({prev_cnt} from past days); {len(to_analyze)} new articles to analyze")

    # 4. 未解析記事のみ本文取得 & Gemini バッチ解析
    if to_analyze:
        fetch_article_bodies(to_analyze)
        analyze_with_gemini(to_analyze, start_time, run_at=run_at)

    # 5. 当日の既存記事とマージ（朝のみ出現した記事も日次アーカイブに保持）
    all_articles = merge_today_articles(existing_today, reused_articles + to_analyze, run_at)

    # 6. スコアリング（複数ソース出現の検知）& はてなブックマーク件数の一括取得
    _apply_hot_flags(all_articles, hot_norms, preserve_existing=True)

    fetch_hatena_counts(all_articles)

    # 7. 関連記事グルーピング & デイリーブリーフィング生成
    all_articles = apply_topic_grouping(all_articles)
    briefing = generate_daily_briefing(all_articles, run_at=run_at, sleep_before=True)

    # 8. 保存 + RSS フィード出力
    save_results(all_articles, run_at=run_at, briefing=briefing)
    generate_rss_feed(all_articles)

    # 9. 週間ダイジェスト（日曜のみ・1日1回）
    digest = generate_weekly_digest(all_articles, run_at=run_at)

    # 10. 通知（NOTIFY_AT_JST 設定時はその時刻まで待機）
    if args.no_notify:
        print("[INFO] --no-notify: skipping notifications")
    else:
        wait_until_notify_time()
        fresh = [
            a for a in all_articles
            if a.get("new_at") == run_at and a.get("analysis_status", "ok") == "ok"
        ]
        send_notifications(fresh)
        if digest:
            send_digest_notification(digest)

    elapsed = time.monotonic() - start_time
    error_count = sum(
        1 for a in all_articles if a.get("analysis_status") in ("error", "skipped")
    )
    print(f"\n{'=' * 60}")
    print(f"IT Info Collector -- Complete ({elapsed:.1f}s)")
    print(f"  Total: {len(all_articles)} articles, Errors: {error_count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
