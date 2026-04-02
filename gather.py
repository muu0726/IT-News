#!/usr/bin/env python3
"""
IT情報収集・分析・通知システム - バックエンドエンジン
Hacker News API / RSS / Gemini REST API / Discord Webhook
"""

import json
import os
import sys
import io
import time
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import feedparser
import requests


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
HN_FETCH_COUNT = 5

RSS_FEEDS = {
    "Zenn": "https://zenn.dev/feed",
    "Qiita": "https://qiita.com/popular-items/feed.atom",
    "Reddit": "https://www.reddit.com/r/technology/.rss",
    "はてなブックマーク": "https://b.hatena.ne.jp/hotentry/it.rss",
}
RSS_FETCH_COUNT = 5

# Gemini API 設定
# gemini-2.0-flash: 安定版・非 thinking モデル・JSON出力モード完全対応
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
GEMINI_SLEEP_SEC = 4          # リクエスト間隔（15 RPM対応: 4秒間隔）
GEMINI_MAX_RETRIES = 5        # リトライ上限（429対策強化）
GEMINI_BACKOFF_MAX_SEC = 60   # バックオフ上限（長めに設定）
GLOBAL_TIMEOUT_SEC = 600      # 全体タイムアウト: 10分

SCORE_HOT_BONUS = 20
SCORE_MAX = 100
DISCORD_TOP_N = 10
DISCORD_SCORE_THRESHOLD = 80

DATA_FILE = "data.json"
ARCHIVE_DIR = "archive"

JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """URL を正規化して重複比較に使う"""
    if not url:
        return ""
    parsed = urlparse(url)
    # クエリパラメータとフラグメントを除去して正規化
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()


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
    """Gemini レスポンスからテキストを安全に抽出する。
    thinking モデルの場合、thought パーツをスキップして
    実際の回答テキストのみを返す。
    """
    try:
        candidate = data["candidates"][0]

        # finishReason チェック（SAFETY 等で中断された場合）
        finish_reason = candidate.get("finishReason", "")
        if finish_reason not in ("", "STOP", "MAX_TOKENS"):
            print(f"[WARN] Gemini finishReason: {finish_reason}")
            return None

        parts = candidate["content"]["parts"]

        # 全 parts をイテレートし、thought でないテキストを返す
        for part in parts:
            if part.get("thought", False):
                continue
            if "text" in part:
                return part["text"]

        # フォールバック: 最後のパーツのテキストを返す
        for part in reversed(parts):
            if "text" in part:
                return part["text"]

    except (KeyError, IndexError, TypeError) as e:
        print(f"[WARN] Gemini response structure error: {e}")

    return None


def parse_json_safely(text: str) -> dict | None:
    """JSON テキストを安全にパースする。
    コードフェンス（```json ... ```）やBOMを除去してからパースを試行。
    """
    if not text:
        return None

    # BOM除去
    text = text.strip().lstrip("\ufeff")

    # コードフェンス除去 (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[WARN] JSON parse failed: {e}")
        print(f"[DEBUG] Raw text (first 200 chars): {text[:200]}")
        return None


def validate_gemini_result(result: dict) -> bool:
    """Gemini が返した解析結果の必須フィールドを検証する"""
    if not isinstance(result, dict):
        return False
    required_keys = ("title", "summary", "tags", "score")
    for key in required_keys:
        if key not in result:
            print(f"[WARN] Missing required key in Gemini result: {key}")
            return False
    if not isinstance(result["tags"], list):
        return False
    if not isinstance(result["score"], (int, float)):
        return False
    return True


# ---------------------------------------------------------------------------
# データソース取得
# ---------------------------------------------------------------------------
def fetch_hackernews() -> list[dict]:
    """Hacker News Top Stories 上位 N 件を取得"""
    print("[INFO] Fetching Hacker News Top Stories ...")
    resp = safe_get(HN_TOP_URL)
    if not resp:
        return []

    story_ids = resp.json()[:HN_FETCH_COUNT]
    articles = []
    for sid in story_ids:
        item_resp = safe_get(HN_ITEM_URL.format(sid))
        if not item_resp:
            continue
        item = item_resp.json()
        articles.append({
            "title": item.get("title", ""),
            "url": item.get("url", f"https://news.ycombinator.com/item?id={sid}"),
            "source": "Hacker News",
            "published": datetime.fromtimestamp(
                item.get("time", 0), tz=timezone.utc
            ).isoformat(),
            "hn_score": item.get("score", 0),
            "hn_comments": item.get("descendants", 0),
        })
    print(f"[INFO] Hacker News: {len(articles)} articles fetched")
    return articles


def fetch_rss_feeds() -> list[dict]:
    """各 RSS/Atom フィードから最新 N 件を取得"""
    all_articles = []
    for source_name, feed_url in RSS_FEEDS.items():
        print(f"[INFO] Fetching RSS: {source_name} ...")
        try:
            # feedparser に User-Agent を設定（Reddit 等のブロック対策）
            feed = feedparser.parse(
                feed_url,
                agent="IT-Info-Collector/1.0 (GitHub Actions Bot)"
            )

            # フィードエラーチェック
            if feed.bozo and not feed.entries:
                print(f"[WARN] RSS feed error ({source_name}): {feed.bozo_exception}")
                continue

            entries = feed.entries[:RSS_FETCH_COUNT]
            for entry in entries:
                link = entry.get("link", "")
                title = entry.get("title", "")
                # HTML タグ除去
                if hasattr(title, "replace"):
                    title = re.sub(r"<[^>]+>", "", title).strip()
                all_articles.append({
                    "title": title,
                    "url": link,
                    "source": source_name,
                    "published": entry.get("published", entry.get("updated", "")),
                })
            print(f"[INFO] {source_name}: {len(entries)} articles fetched")
        except Exception as e:
            print(f"[ERROR] RSS fetch failed ({source_name}): {e}")
    return all_articles


# ---------------------------------------------------------------------------
# Gemini 解析 (REST API 直接呼び出し)
# ---------------------------------------------------------------------------
ANALYSIS_PROMPT = """\
あなたはIT技術ニュースの分析AIです。以下の記事情報を分析してください。

記事タイトル: {title}
記事URL: {url}
情報ソース: {source}

以下の指示に厳密に従い、JSON形式で回答してください。
- 英語の記事の場合は、タイトルと要約の両方を必ず日本語に翻訳してください。
- title: 記事のタイトル（英語の場合は自然な日本語に翻訳。日本語ならそのまま）
- summary: 記事の内容を推測し、日本語で3行の要約を作成（改行は\\nで区切る）
- tags: 技術タグを3つ（日本語または英語）のリスト
- score: 重要度スコア（0-100の整数）。
  IT業界への影響度、技術的新規性、実用性を総合的に評価してください。
- score_reason: スコアの理由を日本語で1文

回答は以下のJSON形式**のみ**出力してください（余分なテキストは不要です）:
{{"title": "日本語タイトル", "summary": "1行目\\n2行目\\n3行目", "tags": ["tag1", "tag2", "tag3"], "score": 75, "score_reason": "..."}}
"""


def call_gemini_rest(prompt: str, api_key: str) -> dict | None:
    """Gemini REST API を直接呼び出し (リトライ付き)"""
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                text = extract_text_from_gemini_response(data)
                if text is None:
                    return {"error": "Gemini returned no usable text"}
                result = parse_json_safely(text)
                if result is None:
                    return {"error": f"JSON parse failed: {text[:100]}"}
                if not validate_gemini_result(result):
                    return {"error": f"Invalid result structure: {result}"}
                return result

            elif resp.status_code == 429:
                # レート制限 -- 指数バックオフでリトライ（上限付き）
                wait = min(GEMINI_SLEEP_SEC * (2 ** attempt), GEMINI_BACKOFF_MAX_SEC)
                print(f"[WARN] 429 Rate limited (attempt {attempt}/{GEMINI_MAX_RETRIES}), waiting {wait}s ...")
                time.sleep(wait)
                continue

            elif resp.status_code >= 500:
                # サーバーエラー -- 一時的な障害として再試行
                wait = min(GEMINI_SLEEP_SEC * (2 ** attempt), GEMINI_BACKOFF_MAX_SEC)
                print(f"[WARN] {resp.status_code} Server error (attempt {attempt}/{GEMINI_MAX_RETRIES}), waiting {wait}s ...")
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
                print(f"[WARN] Retrying in {wait}s (attempt {attempt}/{GEMINI_MAX_RETRIES}) ...")
                time.sleep(wait)
            continue

    print(f"[ERROR] Gemini API: all {GEMINI_MAX_RETRIES} retries exhausted")
    return {"error": "All retries exhausted"}


def analyze_with_gemini(articles: list[dict], start_time: float = 0) -> list[dict]:
    """Gemini REST API で各記事を解析（全体タイムアウト対応）"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[WARN] GEMINI_API_KEY not set -- skipping Gemini analysis")
        for art in articles:
            art.update({
                "summary": "(Gemini APIキー未設定のため解析スキップ)",
                "tags": [],
                "score": 50,
                "score_reason": "解析未実施",
            })
        return articles

    print(f"[INFO] Using Gemini model: {GEMINI_MODEL} (REST API)")

    analyzed = []
    for i, art in enumerate(articles):
        # 全体タイムアウトチェック
        if start_time and (time.monotonic() - start_time) > GLOBAL_TIMEOUT_SEC:
            remaining = len(articles) - i
            print(f"[WARN] Global timeout ({GLOBAL_TIMEOUT_SEC}s) reached. "
                  f"Skipping remaining {remaining} articles.")
            for skip_art in articles[i:]:
                skip_art.update({
                    "summary": "(タイムアウトのため解析スキップ)",
                    "tags": [],
                    "score": 50,
                    "score_reason": "タイムアウト",
                })
                analyzed.append(skip_art)
            break

        title_short = art["title"][:60] if art.get("title") else "No Title"
        print(f"[INFO] Analyzing ({i + 1}/{len(articles)}): {title_short} ...")
        prompt = ANALYSIS_PROMPT.format(
            title=art["title"],
            url=art["url"],
            source=art["source"],
        )

        result = call_gemini_rest(prompt, api_key)

        if result and "error" not in result:
            if result.get("title"):
                art["title"] = result["title"]
            art["summary"] = result.get("summary", "")
            art["tags"] = result.get("tags", [])
            art["score"] = int(result.get("score", 50))
            art["score_reason"] = result.get("score_reason", "")
        else:
            err_detail = result["error"] if result else "解析エラー"
            art.update({
                "summary": "(解析中にエラーが発生しました)",
                "tags": [],
                "score": 50,
                "score_reason": f"解析エラー: {err_detail}",
            })
        analyzed.append(art)

        # レート制限対策: 次のリクエストまで待機
        if i < len(articles) - 1:
            time.sleep(GEMINI_SLEEP_SEC)

    return analyzed


# ---------------------------------------------------------------------------
# インテリジェント・スコアリング
# ---------------------------------------------------------------------------
def apply_hot_scoring(articles: list[dict]) -> list[dict]:
    """複数ソースに出現する URL を検知し、スコア加算 & is_hot フラグ付与"""
    url_map: dict[str, list[int]] = {}
    for idx, art in enumerate(articles):
        norm = normalize_url(art.get("url", ""))
        if norm:
            url_map.setdefault(norm, []).append(idx)

    hot_urls = {url for url, indices in url_map.items() if len(indices) > 1}

    for art in articles:
        norm = normalize_url(art.get("url", ""))
        if norm in hot_urls:
            art["is_hot"] = True
            art["score"] = min(art.get("score", 50) + SCORE_HOT_BONUS, SCORE_MAX)
        else:
            art["is_hot"] = False

    return articles


# ---------------------------------------------------------------------------
# Discord 通知
# ---------------------------------------------------------------------------
def send_discord_notification(articles: list[dict]) -> None:
    """スコア上位の重要記事を Discord Webhook で通知"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("[INFO] DISCORD_WEBHOOK_URL not set -- skipping notification")
        return

    top_articles = sorted(
        [a for a in articles if a.get("score", 0) >= DISCORD_SCORE_THRESHOLD],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )[:DISCORD_TOP_N]

    if not top_articles:
        print("[INFO] No articles above score threshold -- skipping notification")
        return

    embeds = []
    for art in top_articles:
        score = art.get("score", 0)
        color = 0xFFD700 if score >= 80 else 0x8E8E93
        if score >= 90:
            color = 0xFF4500

        tags_str = " / ".join(art.get("tags", []))
        hot_icon = "\U0001f525 " if art.get("is_hot") else ""

        embeds.append({
            "title": f"{hot_icon}{art.get('title', 'No Title')}",
            "url": art.get("url", ""),
            "color": color,
            "description": art.get("summary", "").replace("\\n", "\n"),
            "fields": [
                {"name": "\U0001f4ca Score", "value": str(score), "inline": True},
                {"name": "\U0001f3f7\ufe0f Tags", "value": tags_str or "-", "inline": True},
                {"name": "\U0001f4f0 Source", "value": art.get("source", ""), "inline": True},
                {"name": "\U0001f4a1 Reason", "value": art.get("score_reason", ""), "inline": False},
            ],
            "footer": {"text": "IT Info Collector"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    payload = {
        "username": "\U0001f4e1 IT Info Collector",
        "content": f"## \U0001f680 本日の最重要IT記事 TOP {len(top_articles)}",
        "embeds": embeds,
    }

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 204):
            print(f"[INFO] Discord notification sent ({len(top_articles)} articles)")
        else:
            print(f"[WARN] Discord webhook returned {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"[ERROR] Discord notification failed: {e}")


# ---------------------------------------------------------------------------
# ファイル出力
# ---------------------------------------------------------------------------
def save_results(articles: list[dict]) -> None:
    """data.json と archive/YYYY-MM-DD.json を生成"""
    now = datetime.now(JST)

    # エラー・成功の集計
    error_count = sum(1 for a in articles if "解析エラー" in a.get("score_reason", ""))
    success_count = len(articles) - error_count

    output = {
        "generated_at": now.isoformat(),
        "total_count": len(articles),
        "success_count": success_count,
        "error_count": error_count,
        "gemini_model": GEMINI_MODEL,
        "articles": sorted(articles, key=lambda x: x.get("score", 0), reverse=True),
    }

    # data.json
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {DATA_FILE} ({len(articles)} articles, {error_count} errors)")

    # archive/YYYY-MM-DD.json
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_file = os.path.join(ARCHIVE_DIR, f"{now.strftime('%Y-%m-%d')}.json")
    with open(archive_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {archive_file}")

    # archive/index.json -- フロントエンドがアーカイブ一覧を取得するために使用
    archive_files = sorted(
        [fn for fn in os.listdir(ARCHIVE_DIR) if fn.endswith(".json") and fn != "index.json"],
        reverse=True,
    )
    with open(os.path.join(ARCHIVE_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(archive_files, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Updated archive/index.json ({len(archive_files)} files)")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> None:
    start_time = time.monotonic()

    print("=" * 60)
    print("IT Info Collector -- Start")
    print(f"Time: {datetime.now(JST).isoformat()}")
    print(f"Gemini model: {GEMINI_MODEL}")
    print(f"Global timeout: {GLOBAL_TIMEOUT_SEC}s ({GLOBAL_TIMEOUT_SEC // 60}min)")
    print("=" * 60)

    # 1. データ取得
    hn_articles = fetch_hackernews()
    rss_articles = fetch_rss_feeds()
    all_articles = hn_articles + rss_articles
    print(f"\n[INFO] Total raw articles: {len(all_articles)}")

    if not all_articles:
        print("[WARN] No articles fetched -- exiting")
        return

    # 2. 重複記事の除去（URL正規化で判定）
    before_dedup = len(all_articles)
    all_articles = deduplicate_articles(all_articles)
    if before_dedup != len(all_articles):
        print(f"[INFO] Deduplicated: {before_dedup} -> {len(all_articles)} articles")

    # 3. Gemini 解析（タイムアウト対応）
    all_articles = analyze_with_gemini(all_articles, start_time)

    # 4. スコアリング (重複検知)
    all_articles = apply_hot_scoring(all_articles)

    # 5. 保存
    save_results(all_articles)

    # 6. Discord 通知
    send_discord_notification(all_articles)

    elapsed = time.monotonic() - start_time
    error_count = sum(1 for a in all_articles if "解析エラー" in a.get("score_reason", ""))
    print(f"\n{'=' * 60}")
    print(f"IT Info Collector -- Complete ({elapsed:.1f}s)")
    print(f"  Total: {len(all_articles)} articles, Errors: {error_count}")
    print("=" * 60)


if __name__ == "__main__":
    main()
