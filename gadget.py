#!/usr/bin/env python3
"""
最新ガジェット情報 収集・分析・通知

処理フロー:
  1. 収集     国内ガジェットメディア RSS (公開48時間以内)
  2. 新着抽出 gadgets.json に未登録 (または前回解析失敗) の記事だけを対象にする
  3. 本文取得 gather.fetch_article_bodies を流用
  4. Gemini 解析 (製品名・ブランド・カテゴリ・新製品判定・価格・発売日・注目度)
  5. 保存     gadgets.json に直近30日分を蓄積
  6. 通知     今回新しく見つかった新製品のうち注目度上位 最大3件 (Discord / Slack / LINE)

実行モード:
  python gadget.py               収集 → 通知
  python gadget.py --no-notify   収集・保存のみ (GitHub Actions の collect ジョブ)
  python gadget.py --notify-only 保存済み gadgets.json から通知のみ (notify ジョブ)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from gather import (
    ANALYSIS_BATCH_SIZE,
    GEMINI_MODEL,
    GEMINI_SLEEP_SEC,
    JST,
    SITE_URL,
    call_gemini_rest,
    deduplicate_articles,
    fetch_article_bodies,
    normalize_url,
    parse_published,
    wait_until_notify_time,
)


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
GADGET_FEEDS = {
    "ITmedia Mobile": "https://rss.itmedia.co.jp/rss/2.0/mobile.xml",
    "ITmedia PC USER": "https://rss.itmedia.co.jp/rss/2.0/pcuser.xml",
    "PC Watch": "https://pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf",
    "ケータイ Watch": "https://k-tai.watch.impress.co.jp/data/rss/1.0/ktw/feed.rdf",
    "AV Watch": "https://av.watch.impress.co.jp/data/rss/1.0/avw/feed.rdf",
    "GIZMODO Japan": "https://www.gizmodo.jp/index.xml",
}
GADGET_FETCH_PER_FEED = 10    # フィードごとの取得上限（ケータイ Watch は 300 件返る）
GADGET_MAX_AGE_HOURS = 48     # この時間より古い記事は収集しない
GADGET_MAX_ANALYZE = 48       # 1回の実行で解析する最大件数（超過分は次回）
GADGET_TIMEOUT_SEC = 360      # 解析の全体タイムアウト
GADGET_KEEP_DAYS = 30         # gadgets.json の保持日数

GADGET_NOTIFY_MAX = 3
GADGET_NOTIFY_SCORE = 60

GADGET_FILE = "gadgets.json"
GADGET_PAGE_URL = SITE_URL + "gadget.html"

GADGET_CATEGORIES = [
    "スマートフォン",
    "PC",
    "タブレット",
    "ウェアラブル",
    "オーディオ",
    "カメラ",
    "ゲーム",
    "スマートホーム",
    "その他",
]


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------
def is_recent(published: str, now: datetime, hours: int = GADGET_MAX_AGE_HOURS) -> bool:
    """公開日時が指定時間以内か。日時が読めない記事は URL 重複除去に任せて残す"""
    try:
        dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt <= timedelta(hours=hours)


def fetch_gadget_feeds(now: datetime | None = None) -> list[dict]:
    """ガジェット系 RSS から公開48時間以内の記事を取得"""
    now = now or datetime.now(timezone.utc)
    articles = []
    for source_name, feed_url in GADGET_FEEDS.items():
        print(f"[INFO] Fetching gadget RSS: {source_name} ...")
        try:
            feed = feedparser.parse(
                feed_url, agent="IT-Info-Collector/1.0 (GitHub Actions Bot)"
            )
            if feed.bozo and not feed.entries:
                print(f"[WARN] RSS feed error ({source_name}): {feed.bozo_exception}")
                continue

            count = 0
            for entry in feed.entries[:GADGET_FETCH_PER_FEED]:
                published = parse_published(entry)
                if not is_recent(published, now):
                    continue
                title = re.sub(r"<[^>]+>", "", str(entry.get("title", ""))).strip()
                articles.append({
                    "title": title,
                    "url": entry.get("link", ""),
                    "source": source_name,
                    "published": published,
                })
                count += 1
            print(f"[INFO] {source_name}: {count} recent articles")
        except Exception as e:
            print(f"[ERROR] RSS fetch failed ({source_name}): {e}")
    return deduplicate_articles(articles)


def load_gadgets(path: str = GADGET_FILE) -> dict:
    """保存済み gadgets.json を読み込む（無ければ空）"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"items": []}


def select_new_articles(fetched: list[dict], existing_items: list[dict]) -> list[dict]:
    """未登録、または前回解析に失敗した記事だけを返す"""
    analyzed_ok = {
        normalize_url(it.get("url", ""))
        for it in existing_items
        if it.get("analysis_status") == "ok"
    }
    return [a for a in fetched if normalize_url(a.get("url", "")) not in analyzed_ok]


# ---------------------------------------------------------------------------
# Gemini 解析
# ---------------------------------------------------------------------------
def build_gadget_prompt(chunk: list[dict]) -> str:
    """ガジェット記事をまとめて解析するプロンプトを構築する"""
    blocks = []
    for i, art in enumerate(chunk):
        body = art.get("_body", "")
        body_line = f"本文抜粋: {body}" if body else "本文抜粋: (取得できず。タイトルから推測)"
        blocks.append(
            f"### 記事ID: {i}\n"
            f"タイトル: {art.get('title', '')}\n"
            f"ソース: {art.get('source', '')}\n"
            f"{body_line}"
        )
    categories = " / ".join(GADGET_CATEGORIES)

    return (
        f"あなたはガジェット専門メディアの編集者です。以下の{len(chunk)}件の記事を分析してください。\n\n"
        f"各記事について、以下のフィールドを持つオブジェクトを生成してください:\n"
        f"- id: 入力の記事IDと同じ整数\n"
        f"- title: 記事タイトル（日本語。英語なら自然な日本語に翻訳）\n"
        f"- summary: 日本語で3行の要約（改行は\\nで区切る）\n"
        f"- product_name: 記事の主な製品名（例: \"iPhone 18 Pro\"）。特定の製品がなければ null\n"
        f"- brand: メーカー・ブランド名。不明なら null\n"
        f"- category: 次のリストから必ず1つだけ選択: {categories}\n"
        f"- is_new_product: 新製品の「発表」または「発売・予約開始」を伝える記事なら true。"
        f"レビュー、セール・値下げ、ソフトウェアアップデート、イベントレポート、"
        f"サービス・料金プラン、業界動向の記事は false\n"
        f"- price: 価格（例: \"19万8800円\"）。記載がなければ null\n"
        f"- release_date: 発売日・発売時期（例: \"2026年10月3日\"）。記載がなければ null\n"
        f"- score: 注目度（0-100の整数）。話題性、新規性、一般ユーザーへの影響を総合評価\n"
        f"- score_reason: 注目度の理由を日本語で1文\n\n"
        f"{chr(10).join(blocks)}\n\n"
        f"回答は全{len(chunk)}件分のオブジェクトを含むJSON配列**のみ**を出力してください:\n"
        f'[{{"id": 0, "title": "...", "summary": "1行目\\n2行目\\n3行目", "product_name": "...", '
        f'"brand": "...", "category": "...", "is_new_product": true, "price": null, '
        f'"release_date": null, "score": 70, "score_reason": "..."}}, ...]'
    )


def validate_gadget_item(item) -> bool:
    """バッチ解析結果の1件分を検証する"""
    if not isinstance(item, dict):
        return False
    for key in ("id", "title", "summary", "is_new_product", "score"):
        if key not in item:
            return False
    if not isinstance(item["id"], int) or isinstance(item["id"], bool):
        return False
    if not isinstance(item["score"], (int, float)) or isinstance(item["score"], bool):
        return False
    return True


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in ("null", "none", "不明") else None


def apply_gadget_analysis(art: dict, item: dict, run_at: str) -> None:
    """検証済みの解析結果を記事に反映する"""
    if item.get("title"):
        art["title"] = str(item["title"])
    art["summary"] = str(item.get("summary", ""))
    art["product_name"] = _optional_str(item.get("product_name"))
    art["brand"] = _optional_str(item.get("brand"))
    category = str(item.get("category", ""))
    art["category"] = category if category in GADGET_CATEGORIES else "その他"
    flag = item.get("is_new_product")
    art["is_new_product"] = flag is True or str(flag).lower() == "true"
    art["price"] = _optional_str(item.get("price"))
    art["release_date"] = _optional_str(item.get("release_date"))
    art["score"] = max(0, min(int(item.get("score", 0)), 100))
    art["score_reason"] = str(item.get("score_reason", ""))
    art["analysis_status"] = "ok"
    art["new_at"] = run_at


def _mark_failed(art: dict, reason: str, status: str = "error") -> None:
    art.update({
        "summary": "", "product_name": None, "brand": None, "category": "その他",
        "is_new_product": False, "price": None, "release_date": None,
        "score": 0, "score_reason": reason, "analysis_status": status,
    })


def analyze_gadgets(articles: list[dict], run_at: str) -> list[dict]:
    """Gemini でガジェット記事をバッチ解析する"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[WARN] GEMINI_API_KEY not set -- skipping gadget analysis")
        for art in articles:
            _mark_failed(art, "APIキー未設定のため解析未実施", status="skipped")
        return articles

    chunks = [
        articles[i:i + ANALYSIS_BATCH_SIZE]
        for i in range(0, len(articles), ANALYSIS_BATCH_SIZE)
    ]
    start = time.monotonic()
    for ci, chunk in enumerate(chunks):
        if time.monotonic() - start > GADGET_TIMEOUT_SEC:
            print(f"[WARN] Gadget analysis timeout ({GADGET_TIMEOUT_SEC}s) -- skipping rest")
            for c in chunks[ci:]:
                for art in c:
                    _mark_failed(art, "タイムアウトのため解析スキップ", status="skipped")
            break

        print(f"[INFO] Analyzing gadget batch {ci + 1}/{len(chunks)} ({len(chunk)} articles) ...")
        result = call_gemini_rest(build_gadget_prompt(chunk), api_key)
        if isinstance(result, list):
            by_id = {it["id"]: it for it in result if validate_gadget_item(it)}
            for i, art in enumerate(chunk):
                if i in by_id:
                    apply_gadget_analysis(art, by_id[i], run_at)
                else:
                    _mark_failed(art, "解析エラー: バッチ応答に含まれず")
        else:
            detail = result.get("error", "不明") if isinstance(result, dict) else "不正な応答形式"
            for art in chunk:
                _mark_failed(art, f"解析エラー: {detail}")

        if ci < len(chunks) - 1:
            time.sleep(GEMINI_SLEEP_SEC)

    ok = sum(1 for a in articles if a.get("analysis_status") == "ok")
    print(f"[INFO] Gadget analysis complete: {ok}/{len(articles)} succeeded "
          f"({time.monotonic() - start:.1f}s)")
    return articles


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------
def merge_gadgets(existing_items: list[dict], analyzed: list[dict],
                  run_at: str, now: datetime | None = None) -> list[dict]:
    """解析結果を既存データへマージし、保持期間外を削除して新着順に並べる"""
    now = now or datetime.now(JST)
    merged: dict[str, dict] = {}
    for it in existing_items:
        merged[normalize_url(it.get("url", ""))] = it
    for art in analyzed:
        art.pop("_body", None)
        key = normalize_url(art.get("url", ""))
        art["first_seen"] = merged.get(key, {}).get("first_seen") or run_at
        merged[key] = art

    cutoff = now - timedelta(days=GADGET_KEEP_DAYS)
    kept = []
    for it in merged.values():
        try:
            if datetime.fromisoformat(it.get("first_seen", "")) < cutoff:
                continue
        except ValueError:
            pass
        kept.append(it)
    return sorted(kept, key=lambda x: x.get("published") or x.get("first_seen", ""), reverse=True)


def save_gadgets(items: list[dict], run_at: str, path: str = GADGET_FILE) -> None:
    output = {
        "schema_version": 1,
        "generated_at": run_at,
        "gemini_model": GEMINI_MODEL,
        "total_count": len(items),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] Saved {path} ({len(items)} items)")


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
def _product_key(item: dict) -> str:
    name = item.get("product_name") or item.get("title", "")
    return re.sub(r"[\s\W_]+", "", str(name)).lower()


def pick_gadget_notifications(data: dict) -> list[dict]:
    """今回新しく見つかった新製品のうち、注目度上位を最大3件（同一製品は1件に集約）"""
    run_at = data.get("generated_at")
    if not run_at:
        return []
    candidates = sorted(
        [
            it for it in data.get("items", [])
            if it.get("new_at") == run_at
            and it.get("analysis_status") == "ok"
            and it.get("is_new_product") is True
            and it.get("score", 0) >= GADGET_NOTIFY_SCORE
        ],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )
    picked, seen = [], set()
    for it in candidates:
        key = _product_key(it)
        if key in seen:
            continue
        seen.add(key)
        picked.append(it)
        if len(picked) >= GADGET_NOTIFY_MAX:
            break
    return picked


def _detail_line(it: dict) -> str:
    parts = [p for p in (it.get("brand"), it.get("price"), it.get("release_date")) if p]
    return " / ".join(parts)


def send_gadget_discord(items: list[dict]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("[INFO] DISCORD_WEBHOOK_URL not set -- skipping Discord")
        return

    embeds = []
    for it in items:
        fields = [
            {"name": "🏷️ カテゴリ", "value": it.get("category") or "-", "inline": True},
            {"name": "📊 注目度", "value": str(it.get("score", 0)), "inline": True},
            {"name": "📰 ソース", "value": it.get("source") or "-", "inline": True},
        ]
        if it.get("price"):
            fields.append({"name": "💴 価格", "value": str(it["price"])[:1024], "inline": True})
        if it.get("release_date"):
            fields.append({"name": "📅 発売", "value": str(it["release_date"])[:1024], "inline": True})
        name = it.get("product_name")
        embeds.append({
            "title": (f"📱 {name}" if name else it.get("title", "No Title"))[:256],
            "url": it.get("url", ""),
            "color": 0x2EAADC,
            "description": (f"**{it.get('title', '')}**\n" if name else "")
                           + it.get("summary", "").replace("\\n", "\n")[:3500],
            "fields": fields,
            "footer": {"text": "IT Info Hub — Gadget"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    payload = {
        "username": "\U0001f4e1 IT Info Collector",
        "content": f"## 📱 最新ガジェット情報 {len(items)}件\n{GADGET_PAGE_URL}",
        "embeds": embeds,
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            print(f"[INFO] Gadget Discord notification sent ({len(items)} items)")
        else:
            print(f"[WARN] Discord webhook returned {resp.status_code}: {resp.text[:120]}")
    except requests.RequestException as e:
        print(f"[ERROR] Gadget Discord notification failed: {e}")


def send_gadget_slack(items: list[dict]) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("[INFO] SLACK_WEBHOOK_URL not set -- skipping Slack")
        return

    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"📱 最新ガジェット情報 {len(items)}件"},
    }]
    for it in items:
        name = it.get("product_name") or it.get("title", "")
        detail = _detail_line(it)
        summary = it.get("summary", "").replace("\\n", "\n")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*<{it.get('url', '')}|{name}>*  `{it.get('score', 0)}点`\n"
                         + (f"{detail}\n" if detail else "")
                         + f"{summary}\n_📰 {it.get('source', '')} / 🏷️ {it.get('category', '')}_"),
            },
        })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"<{GADGET_PAGE_URL}|ガジェット情報ページを開く>"}],
    })
    try:
        resp = requests.post(webhook_url, json={"blocks": blocks}, timeout=15)
        if resp.status_code == 200:
            print(f"[INFO] Gadget Slack notification sent ({len(items)} items)")
        else:
            print(f"[WARN] Slack webhook returned {resp.status_code}: {resp.text[:120]}")
    except requests.RequestException as e:
        print(f"[ERROR] Gadget Slack notification failed: {e}")


def send_gadget_line(items: list[dict]) -> None:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    if not token:
        print("[INFO] LINE_CHANNEL_ACCESS_TOKEN not set -- skipping LINE")
        return

    lines = [f"📱 最新ガジェット情報 {len(items)}件", ""]
    for it in items:
        lines.append(f"・{it.get('product_name') or it.get('title', '')}")
        detail = _detail_line(it)
        if detail:
            lines.append(f"  {detail}")
        lines.append(it.get("url", ""))
        lines.append("")
    lines.append(GADGET_PAGE_URL)
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            json={"messages": [{"type": "text", "text": "\n".join(lines)[:4900]}]},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"[INFO] Gadget LINE notification sent ({len(items)} items)")
        else:
            print(f"[WARN] LINE API returned {resp.status_code}: {resp.text[:120]}")
    except requests.RequestException as e:
        print(f"[ERROR] Gadget LINE notification failed: {e}")


def is_generated_today(data: dict, now: datetime | None = None) -> bool:
    """gadgets.json が今日 (JST) 生成されたものか。

    収集に失敗した日に、リポジトリに残る前日分で同じ通知を再送しないためのガード。
    """
    now = now or datetime.now(JST)
    return str(data.get("generated_at", "")).startswith(now.strftime("%Y-%m-%d"))


def send_gadget_notifications(data: dict) -> None:
    if not is_generated_today(data):
        print(f"[WARN] {GADGET_FILE} is not from today "
              f"(generated_at: {data.get('generated_at', '?')}) -- skipping gadget notifications")
        return
    items = pick_gadget_notifications(data)
    if not items:
        print("[INFO] No new gadget products to notify")
        return
    send_gadget_discord(items)
    send_gadget_slack(items)
    send_gadget_line(items)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def collect() -> dict:
    run_at = datetime.now(JST).isoformat()
    data = load_gadgets()
    existing = data["items"]

    fetched = fetch_gadget_feeds()
    targets = select_new_articles(fetched, existing)
    print(f"[INFO] Gadget articles: {len(fetched)} recent, {len(targets)} to analyze")
    if len(targets) > GADGET_MAX_ANALYZE:
        # フィード順で切ると後ろのソースだけが毎回落ちるため、新しい記事を優先する
        print(f"[INFO] Limiting analysis to {GADGET_MAX_ANALYZE} newest (rest next run)")
        targets.sort(key=lambda a: a.get("published", ""), reverse=True)
        targets = targets[:GADGET_MAX_ANALYZE]

    if targets:
        fetch_article_bodies(targets)
        analyze_gadgets(targets, run_at)

    items = merge_gadgets(existing, targets, run_at)
    save_gadgets(items, run_at)
    return load_gadgets()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gadget Info Collector")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-notify", action="store_true", help="収集・保存のみ行い通知しない")
    mode.add_argument("--notify-only", action="store_true", help="保存済みデータから通知のみ行う")
    args = parser.parse_args()

    if args.notify_only:
        if not os.path.exists(GADGET_FILE):
            print(f"[ERROR] {GADGET_FILE} not found")
            sys.exit(1)
        data = load_gadgets()
        wait_until_notify_time()
        send_gadget_notifications(data)
        return

    print("=" * 60)
    print("Gadget Info Collector -- Start")
    print("=" * 60)
    data = collect()
    if args.no_notify:
        print("[INFO] --no-notify: skipping notifications")
        return
    wait_until_notify_time()
    send_gadget_notifications(data)


if __name__ == "__main__":
    main()
