# IT Info Hub — 仕様書

- **バージョン**: 3.0（2026-10-29 アップデート）
- **リポジトリ**: https://github.com/muu0726/IT-News
- **ライセンス**: MIT
- **UIデザイン方針**: 絵文字不使用（SVGアイコン・タイポグラフィ・テキストバッジに統一）

---

## 1. 概要

IT 技術ニュースを 9 ソースから1日2回（朝・昼）並列自動収集し、記事本文・OGP/JSON-LD・はてなブックマーク件数を取得したうえで **Gemini 2.5 Flash**（Structured Outputs対応）により要約・翻訳・タグ付け・カテゴリ分類・重要度スコアリング・デイリーブリーフィング生成を行い、静的サイト（PWA）として GitHub Pages 上で閲覧できるシステム。高スコアの新着記事は Discord / Slack / LINE に自動通知され、収集結果は ITニュース用 (`feed.xml`) とガジェット新製品用 (`gadget_feed.xml`) の2系統の RSS フィードとして配信される。

## 2. システム構成

```
┌──────────────────────────────────────────────────────────────┐
│ GitHub Actions (.github/workflows/update.yml)                 │
│   毎日2回（JST 06:12→07:00通知 / 14:12→15:00通知）+ dispatch │
│                                                               │
│  gather.py                                                    │
│   1. 並列収集   Hacker News API + RSS×8 (ThreadPoolExecutor)  │
│   2. 重複除去   URL正規化（トラッキング除去・IDクエリ保持）     │
│   3. 差分再利用 当日分 + 過去3日分の解析済み記事を再利用        │
│   4. 本文取得   並列8ワーカーでHTML・OGP・読了時間を抽出        │
│   5. AI解析     Gemini バッチ解析（responseSchema 強制）       │
│   6. 指標付加   複数ソースHOT加点 + はてなブックマーク一括取得  │
│   7. 関連記事   埋め込みベクトルで同一話題をグルーピング        │
│   8. 総括生成   朝刊/夕刊 AIデイリーブリーフィング生成          │
│   9. 保存       data.json / archive/ / search_index / feed.xml│
│  10. 通知       Discord / Slack / LINE（新規かつ score>=80）   │
└──────────────────────────────────────────────────────────────┘
```

## 3. ファイル構成

| パス | 役割 |
|---|---|
| `gather.py` | バックエンド一式（収集・本文取得・解析・グルーピング・保存・通知） |
| `index.html` | フロントエンド SPA（HTML/CSS/JS 単一ファイル、フレームワークなし） |
| `sw.js` | Service Worker（HTML/JSON は Network First、静的アセットは Cache First） |
| `manifest.json` | PWA マニフェスト |
| `requirements.txt` | Python 依存（feedparser, requests） |
| `data.json` | 最新の収集結果（自動生成・コミット、スキーマ v2） |
| `feed.xml` | RSS 2.0 フィード（スコア上位20件、自動生成） |
| `digest.json` | 週間ダイジェスト（毎週日曜に自動生成） |
| `archive/YYYY-MM-DD.json` | 日次アーカイブ（自動生成、180日で自動削除） |
| `archive/index.json` | アーカイブファイル名一覧（降順） |
| `gadget.py` | 最新ガジェット情報の収集・解析・保存・通知（§4.12） |
| `gadget.html` | 最新ガジェット情報ページ（§5.5） |
| `gadgets.json` | ガジェット情報（直近30日分を蓄積、自動生成・コミット） |
| `.github/workflows/update.yml` | 定期実行ワークフロー |
| `docs/SPEC.md` | 本仕様書 |

## 4. バックエンド仕様（gather.py）

### 4.1 データソース

| ソース | 取得方法 | 件数 |
|---|---|---|
| Hacker News | Firebase API（topstories → item） | 上位 8 件 |
| Zenn | `https://zenn.dev/feed` | 5 件 |
| Qiita | 人気記事 Atom フィード | 5 件 |
| Reddit | `r/technology` RSS | 5 件 |
| はてなブックマーク | ホットエントリー（IT）RSS | 5 件 |
| Publickey | `https://www.publickey1.jp/atom.xml` | 5 件 |
| Dev.to | `https://dev.to/feed` | 5 件 |
| GitHub Blog | `https://github.blog/feed/` | 5 件 |
| arXiv cs.AI | `https://rss.arxiv.org/rss/cs.AI` | 5 件（※土日は配信なし＝0件） |

- HTTP は共通で User-Agent `IT-Info-Collector/1.0 (GitHub Actions Bot)`、タイムアウト 15 秒。
- 取得失敗はソース単位でスキップ（他ソースには影響しない）。
- `published` は `feedparser` の `published_parsed` / `updated_parsed` から **ISO8601 (UTC) に正規化**。パース不能な場合のみ生文字列。

### 4.2 重複除去

`normalize_url()` で `scheme://netloc/path`（クエリ・フラグメント除去、小文字化、末尾 `/` 除去）に正規化し、最初に出現した記事を保持する。

### 4.3 記事本文の取得

- `ThreadPoolExecutor`（8 ワーカー）で全記事の URL を並列取得（タイムアウト 10 秒）。
- HTML から `<script>/<style>/<nav>` 等を除去し、`<p>` ブロックを優先して本文テキストを抽出（依存パッケージなしの簡易抽出）。先頭 1,500 文字を解析に使用。
- 取得失敗時はタイトルベース解析にフォールバック（処理は止まらない）。

### 4.4 Gemini バッチ解析

- **モデル**: `gemini-2.5-flash`（REST API、`responseMimeType: application/json`、temperature 0.2、`thinkingConfig.thinkingBudget: 0` で thinking 無効。flash 系モデルのときのみ付与）
- **6 記事を 1 リクエストにまとめて解析**。約 45 記事でもリクエスト数は 8 回程度となり、無料枠 15 RPM と衝突しない（v1.0 では 1 記事 1 リクエストでエラー率 54% に達していた）。
- 出力（記事ごと）: `id` / `title`（英語なら日本語訳）/ `summary`（3 行）/ `tags`（3 個）/ `category`（固定リストから 1 つ）/ `score`（0–100、クランプ済み）/ `score_reason`
- カテゴリ固定リスト: AI・機械学習 / Web・フロントエンド / バックエンド・インフラ / セキュリティ / プログラミング言語 / モバイル / ハードウェア・ガジェット / ビジネス・業界動向 / その他
- **解析状態は `analysis_status` フィールド**（`ok` / `error` / `skipped`）で管理（文字列マッチによる判定は廃止）。
- レート制限対策: リクエスト間 4 秒、429/5xx は指数バックオフ（上限 30 秒）で最大 3 回リトライ。
- 全体タイムアウト 840 秒。超過分は `analysis_status: "skipped"` で埋める。

### 4.5 HOT スコアリング

正規化 URL が複数ソースに出現した記事に `is_hot: true` を付与し、スコアに +20（上限 100）。

### 4.6 関連記事グルーピング

- `gemini-embedding-001`（`batchEmbedContents`、256 次元）でタイトル＋要約の埋め込みを一括取得し、コサイン類似度 ≥ 0.82 のペアを Union-Find でグループ化。
- グループ（2 件以上）に属する記事へ `topic_group`（int）と `topic_size` を付与。
- 埋め込み取得に失敗した場合はタイトルトークンの Jaccard 係数（≥ 0.5）にフォールバック。

### 4.7 出力スキーマ（v2）

`data.json` / `archive/YYYY-MM-DD.json`（同一内容、スコア降順ソート済み）:

```jsonc
{
  "schema_version": 2,
  "generated_at": "2026-07-05T07:58:33+09:00",
  "total_count": 45,
  "success_count": 43,
  "error_count": 2,            // analysis_status が error/skipped の合計
  "gemini_model": "gemini-2.5-flash",
  "articles": [
    {
      "title": "…",             // 日本語化済みタイトル
      "url": "…",
      "source": "Hacker News",
      "published": "…",         // ISO8601 (UTC) に正規化済み
      "hn_score": 48,           // HN のみ
      "hn_comments": 4,         // HN のみ
      "summary": "1行目\n2行目\n3行目",
      "tags": ["AI", "LLM", "GPT"],
      "category": "AI・機械学習",
      "score": 85,              // 0-100
      "score_reason": "…",
      "analysis_status": "ok",  // ok | error | skipped
      "is_hot": false,
      "topic_group": 1,         // 関連記事グループID（グループ所属時のみ）
      "topic_size": 3           // グループ内の記事数（同上）
    }
  ]
}
```

フロントエンドは旧スキーマ（v1: `analysis_status`/`category` なし）のアーカイブも文字列マッチへのフォールバックで表示できる。

### 4.8 RSS フィード出力（feed.xml）

スコア上位 20 件を RSS 2.0 として出力。タイトル・リンク・要約（スコア付き）・pubDate を含み、全テキストは XML エスケープ済み。`index.html` の `<link rel="alternate">` からも参照される。

### 4.9 週間ダイジェスト（digest.json）

- **毎週日曜（JST）** に生成（ローカル検証用に `FORCE_DIGEST=1` で強制実行可）。
- 直近 7 日分のアーカイブ＋当日分をマージ・重複除去し、スコア 70 以上の上位 25 件を Gemini に渡して総括を生成。
- 出力: `overview`（3〜4 文の総括）/ `trends`（3〜5 個）/ `highlights`（5 件、選定理由付き）/ 期間・件数メタデータ。
- Discord にも同内容を embed 投稿。

### 4.10 通知（Discord / Slack / LINE）

score ≥ 80 の上位 10 件を対象に、設定済みのチャネルすべてへ送信:

| チャネル | 環境変数 | 形式 |
|---|---|---|
| Discord | `DISCORD_WEBHOOK_URL` | embeds（スコアで色分け: 90+ 赤 / 80+ 金） |
| Slack | `SLACK_WEBHOOK_URL` | Block Kit（mrkdwn セクション） |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN` | Messaging API broadcast（テキスト、上位 5 件）※LINE Notify は 2025 年 3 月終了のため Messaging API を使用 |

### 4.11 アーカイブローテーション

`archive/YYYY-MM-DD.json` のうち 180 日より古いものを毎回削除し、`index.json` を再生成する。

### 4.12 最新ガジェット情報（gadget.py）

`gather.py` の共通関数（本文取得・Gemini 呼び出し・URL 正規化・通知時刻待機）を import して動く別スクリプト。
`python gadget.py [--no-notify | --notify-only]`（モードは gather.py と同じ）。

| ソース | フィード |
|---|---|
| ITmedia Mobile / ITmedia PC USER | `rss.itmedia.co.jp/rss/2.0/{mobile,pcuser}.xml` |
| PC Watch / ケータイ Watch / AV Watch | `*.watch.impress.co.jp/data/rss/1.0/{pcw,ktw,avw}/feed.rdf` |
| GIZMODO Japan | `www.gizmodo.jp/index.xml` |

1. **収集**: 各フィード先頭 10 件のうち公開 48 時間以内のもの。
2. **新着抽出**: `gadgets.json` で解析済み（`analysis_status: ok`）の URL は除外。前回失敗した記事は再解析。1 回あたり最大 48 件。
3. **解析**: 6 件/リクエストのバッチで `title` / `summary` / `product_name` / `brand` / `category`（スマートフォン・PC・タブレット・ウェアラブル・オーディオ・カメラ・ゲーム・スマートホーム・その他）/ `is_new_product`（新製品の発表・発売・予約開始なら true。レビュー・セール・アップデート等は false）/ `price` / `release_date` / `score`（注目度）/ `score_reason` を生成。解析に初めて成功した記事に `new_at`（その実行の `generated_at`）を付与。
4. **保存**: `gadgets.json`（`schema_version: 1`、`generated_at`、`items` 新着順）。`first_seen` から 30 日を超えた記事は削除。
5. **通知**: `new_at == generated_at` かつ `is_new_product` かつ `score >= 60` の記事を注目度順に最大 **3 件**（`product_name` を正規化して同一製品は 1 件に集約）。IT ニュースとは別メッセージで Discord / Slack / LINE に送信し、該当 0 件なら送らない。同日に再実行しても新着が無いため二重通知にならない。`generated_at` が今日（JST）でない `gadgets.json`（収集失敗日にリポジトリに残る前日分）からは通知しない。

## 5. フロントエンド仕様（index.html）

依存フレームワークなしの Vanilla JS SPA。Google Fonts のみ外部依存。

### 5.1 セキュリティ（XSS 対策）

- 記事データ由来の全テキスト（タイトル・要約・タグ・カテゴリ・スコア理由・ソース名）は `esc()` で HTML エスケープしてから挿入。
- URL は `safeUrl()` で `http(s)://` 以外を `#` に置換。
- イベントハンドラは URL 等を DOM 属性に埋め込まず、描画済み配列のインデックス（`data-idx`）参照で解決。
- 設定パネル・ダイジェスト・タブ等の動的 UI は `textContent` / `createElement` で構築。

### 5.2 機能一覧

| 機能 | 実装 |
|---|---|
| 記事カード | スコアバッジ、金/銀ボーダー、HOT・NEW・既出・FOR YOU・解析エラー・未解析・KW・関連 バッジ、カテゴリ、読了時間、B!数、3行要約、一言メモ、タグ、ソース、公開日、スコア理由 |
| AIブリーフィング | `data.briefing`（朝刊/夕刊・3行要約・注目トピックタグ）を折りたたみバナーで表示 |
| ソースタブ | **データから動的生成**（件数順、件数バッジ付き）。ソース追加は gather.py の 1 行変更で反映 |
| 並び替え | スコア順 / おすすめ順（ローカル親和性スコア加算）/ 新着順 / はてブ数順 / HN注目度順 / ソース順 |
| カテゴリフィルタ | プルダウン（件数付き、データから動的生成） |
| テキスト検索 | タイトル・要約・タグ・カテゴリ・ドメイン・個人メモの AND / 除外（`-word`）検索 |
| 全期間検索 | `archive/search_index.json` を 1 リクエストで読み込み（フォールバック時は日次ファイル並列取得） |
| タグ / 関連グループ絞り込み | クリックで絞り込み、ツールバーのチップから解除。トピック集約モード対応 |
| お気に入り & 一言メモ | ブックマーク登録 + インライン一言メモ（`n` キー対応、Markdown出力・設定エクスポート対応） |
| 既読・既出管理 | 既読記録（上限3,000件）、「未読のみ」フィルタ、「既出を隠す」フィルタ |
| キーワード購読 & ミュート | 最大20語のキーワード購読（KWバッジ・絞り込み）、最大30件のNGワード・ドメイン・タグミュート |
| トレンド統計 | 直近30日のアーカイブから日別記事数・頻出タグ TOP10・ソース別平均スコア・カテゴリ別記事数を SVG 描画 |
| 週間ダイジェスト | digest.json（8日以内のもの）をバナー表示。総括・トレンドチップ・ハイライト5件 |
| 新着通知 | オプトイン（Notification API）。ページ表示時に未通知の高スコア記事（80+）をブラウザ通知 |
| 設定の書き出し/読み込み | お気に入り・キーワード・ミュート・一言メモ・既読を Base64 コードでエクスポート/インポート |
| テーマ | OS 連動 + 手動トグル、チャートも追随 |
| アーカイブ切替 | プルダウンで過去日データ読み込み（旧スキーマ互換） |

### 5.3 localStorage キー

| キー | 内容 |
|---|---|
| `itinfohub_favorites` | お気に入り URL の配列 |
| `itinfohub_memos` | URL ごとの一言メモ連想配列（各最大300文字） |
| `itinfohub_theme` | `"light"` / `"dark"` |
| `itinfohub_read` | 既読 URL の配列（上限 3,000） |
| `itinfohub_keywords` | 購読キーワードの配列（上限 20） |
| `itinfohub_muted` | ミュート語の配列（上限 30） |
| `itinfohub_hide_seen` | 既出記事非表示フラグ（bool） |
| `itinfohub_topic_collapse` | トピック集約表示フラグ（bool） |
| `itinfohub_briefing_open` | デイリーブリーフィング展開状態（bool） |
| `itinfohub_notify` | 通知オプトイン（bool） |
| `itinfohub_notified` | 通知済み URL の配列（上限 500） |
| `itinfohub_gadget_compare` | ガジェット比較選択 URL の配列（最大 4） |

### 5.4 PWA / Service Worker

- キャッシュ名 `itinfohub-v7`。
- **HTML（ナビゲーション）・JSON・XML**: Network First — フロント更新・データ更新が即時反映され、オフライン時のみキャッシュを返す。
- その他静的アセット: Cache First。

### 5.5 最新ガジェット情報ページ（gadget.html）

- `gadgets.json` を読み込む単一ファイルの Vanilla JS ページ。テーマは `itinfohub_theme` を index.html と共有し、XSS 対策（`esc()` / `safeUrl()`）も同方式。
- カード: 注目度バッジ、製品名（記事リンク）、新製品 / 関連記事 バッジ、今回の新着に NEW、カテゴリ・ブランド・価格・発売日、3 行要約、複数メディア記事の名寄せ表示。
- 横並びスペック比較: 最大 4 製品を選択し、メーカー・分類・価格・発売日・注目度・メディア・要約をモーダル表で比較＆Markdownテーブルとしてコピー可能。
- 絞り込み・ソート: カテゴリタブ、「新製品のみ」、「製品名寄せ」、ブランド・価格帯・メディアフィルタ、新着順 / 注目度順 / ブランド順 / 価格順ソート。専用 RSS (`gadget_feed.xml`) 配信対応。
- index.html ヘッダーのナビゲーションスイッチから遷移。SW のプリキャッシュ対象。

## 6. CI/CD（.github/workflows/update.yml）

- トリガー: cron `12 21 * * *`（JST 06:12 起動 → 07:00 通知）および `12 5 * * *`（JST 14:12 起動 → 15:00 通知）の 1 日 2 回 + `workflow_dispatch`
- Python 3.12、pip キャッシュあり。3 ジョブ構成:
  - **collect**（timeout 20 分、concurrency `pages`）: `pytest` → `gather.py --no-notify` → `gadget.py --no-notify`（continue-on-error）→ 通知用データを artifact 保存 → `data.json` / `archive/` / `feed.xml` / `digest.json` / `gadgets.json` / `gadget_feed.xml` を自動コミット & push → Pages デプロイ
  - **notify**（timeout 90 分）: artifact を取得 → `gather.py --notify-only`（定期実行時は `NOTIFY_AT_JST=07:00,15:00` かつ最大 1 時間ガード付きで待機。遅延時は即時送信）→ `gadget.py --notify-only`（二重待機なし即時送信・continue-on-error）
  - **alert**: collect / notify のいずれかが失敗またはキャンセルされた場合に確実に Discord アラート送信
- Secrets: `GEMINI_API_KEY`（必須）, `DISCORD_WEBHOOK_URL` / `SLACK_WEBHOOK_URL` / `LINE_CHANNEL_ACCESS_TOKEN`（任意）

## 7. 残存する制約

1. **要約の正確性**: SPA サイトやペイウォール記事では OGP / JSON-LD またはタイトルベースの解析にフォールバックする。
2. **プッシュ通知**: 静的ホスティングのみで購読管理サーバーがないため、ブラウザのバックグラウンド Web Push は不可（Discord / Slack / LINE 通知で補完）。
3. **お気に入り・メモの同期**: 自動クラウド同期ではなく、Base64 設定コードのエクスポート/インポート方式。
