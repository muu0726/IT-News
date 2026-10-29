# IT Info Hub — AI技術ニュース & ガジェット情報ダッシュボード

IT技術情報を 9 つの主要ソースから自動収集し、**Gemini 2.5 Flash**（thinking 無効・Structured Outputs対応）で本文解析・スコアリング・自動要約を行い、絵文字を排したミニマルな PWA ダッシュボードで閲覧できるサーバーレスシステムです。

## 主な機能

### 収集・解析（バックエンド）
| 機能 | 説明 |
|------|------|
| **マルチソース並列収集** | Hacker News, Zenn, Qiita, Reddit, はてなブックマーク, Publickey, Dev.to, GitHub Blog, arXiv cs.AI を `ThreadPoolExecutor` で高速並列取得 |
| **本文ベースのAI解析 & 読了時間算出** | 記事本文（`<article>/<main>` + OGP / JSON-LD フォールバック）を取得し、推定読了時間（`reading_min`）の算出と Gemini 2.5 Flash による3行要約・タグ・カテゴリ・重要度スコア（0〜100）を生成 |
| **AIデイリーブリーフィング** | 朝刊・夕刊ごとに上位記事から3行のトレンド総括と注目トピックタグ（`briefing`）を自動生成 |
| **はてなブックマーク数連携** | はてなブックマーク件数取得APIで最大50件ずつ一括取得（`hatena_count`）し、社会的注目度を可視化 |
| **Structured Outputs** | Gemini REST API の `responseSchema` により JSON スキーマ準拠の出力を保証 |
| **差分解析・過去3日既出抑制** | 当日および直近3日間のアーカイブと照合して解析結果をキャッシュ再利用し、既出記事（`is_prev_seen`）の重複通知を自動抑制 |
| **バッチ解析** | 6記事を1リクエストでバッチ処理し、APIレート制限（15 RPM）との衝突を回避 |
| **英語記事の日本語化** | 英語タイトル・要約を自動で自然な日本語に翻訳 |
| **HOT検知** | 複数ソースに出現する注目記事を自動検知しスコア加点（+20点） |
| **関連記事グルーピング** | 埋め込みベクトル（`gemini-embedding-001`）のコサイン類似度で同一話題の記事を自動グループ化 |
| **重複除去** | URL正規化（トラッキングパラメータ除去・IDパラメータ保持）で同一記事の重複を自動排除 |
| **全期間検索インデックス生成** | 全アーカイブを統合・重複排除した軽量インデックス `archive/search_index.json` を自動生成 |
| **週間ダイジェスト** | 毎週日曜に1週間の総括をAI生成（`digest.json` + Discord投稿） |
| **2系統RSSフィード出力** | ITニュース上位20件を `feed.xml`、ガジェット新製品上位20件を `gadget_feed.xml`（RSS 2.0）として自動配信 |
| **マルチ通知** | 新着の高重要度記事（スコア80+）を Discord / Slack / LINE に自動投稿 |
| **アーカイブローテーション** | 180日を超えた古い日次アーカイブを自動削除 |

### 閲覧（フロントエンド PWA: `index.html` / `gadget.html`）
| 機能 | 説明 |
|------|------|
| **AIブリーフィングバナー** | 朝刊・夕刊の3行サマリーと注目トピックタグを上部に表示（タグクリックで即座に絞り込み） |
| **パーソナライズ「おすすめ順」** | お気に入り・既読履歴・購読キーワードからローカルで関心プロファイルを構築し、AIスコアに親和性ボーナスを加算してソート |
| **検索 & 多軸ソート/フィルタ** | 複数語 AND・除外検索（`-word`）・一言メモ横断検索、スコア順 / おすすめ順 / 新着順 / はてブ数順 / HN注目度順 / ソース順、「既出を隠す」フィルタ、1リクエスト全期間検索 |
| **記事ごとの一言メモ & 読了時間** | 各記事にインラインで個人メモ（`n` キー対応）を記録・検索・エクスポート可能。本文長に基づく推定読了時間（`読了 約N分`）と `B!` 件数を表示 |
| **トピック集約モード** | 同一話題（`topic_group`）の関連記事を代表カードの折りたたみスレッドに集約表示 |
| **ガジェット横並びスペック比較** | `Gadget Hub` で最大4製品を選択し、メーカー・分類・価格・発売日・注目度・要約を横並びテーブルで比較＆Markdown表コピー（`b` キー対応） |
| **ガジェット名寄せ & 価格帯フィルタ** | 同一製品の複数メディア記事を名寄せ表示、価格帯フィルタ・価格順ソートに対応 |
| **お気に入り・既読管理** | ブックマーク登録、既読/未読の手動切替、「表示中を既読」一括処理（Undo トースト付き） |
| **キーワード購読 & ミュート機能** | 登録キーワードのハイライト・絞り込み、NGワード・ドメイン・タグのミュート除外 |
| **Markdownコピー & 一括出力** | 個別記事または表示中記事一覧をワンクリック（または `y` キー）で Markdown コピー |
| **表示密度切替 & キーボード操作** | 標準 / コンパクト表示切替、`j`/`k`/`o`/`f`/`b`/`n`/`m`/`c`/`y`/`x`/`u`/`v`/`d`/`/`/`?` ショートカット |
| **インタラクティブ統計** | 直近30日の記事数推移・頻出タグTOP10・ソース別平均スコア・カテゴリ別件数を SVG チャート表示（クリックで連動フィルタ） |
| **PWA・テーマ対応** | ダーク/ライトテーマ切替、Service Worker（Network First）によるオフライン対応 |

## セットアップ & デプロイ

### 1. リポジトリ作成 & プッシュ
```bash
git init
git add .
git commit -m "feat: 初期構築"
git remote add origin https://github.com/muu0726/IT-News.git
git push -u origin main
```

### 2. GitHub Secrets 設定
リポジトリの **Settings → Secrets and variables → Actions** で以下を設定します:

| Secret名 | 必須 | 説明 |
|-----------|------|------|
| `GEMINI_API_KEY` | 必須 | Google AI Studio で取得した Gemini API キー |
| `DISCORD_WEBHOOK_URL` | 任意 | Discord チャンネルの Webhook URL（高スコア通知・失敗時アラート用） |
| `SLACK_WEBHOOK_URL` | 任意 | Slack Incoming Webhook URL |
| `LINE_CHANNEL_ACCESS_TOKEN` | 任意 | LINE Messaging API のチャネルアクセストークン（broadcast 送信） |

### 3. GitHub Pages 有効化
リポジトリの **Settings → Pages → Build and deployment → Source** で **GitHub Actions** を選択。
`update.yml` ワークフローにより、自動的にデプロイが行われます。

### 4. ローカル実行・テスト
```bash
# 依存ライブラリのインストール
pip install -r requirements.txt

# ユニットテストの実行
pytest

# データ収集 & 解析の実行
export GEMINI_API_KEY="your-api-key"
python gather.py                 # FORCE_DIGEST=1 を付けると週間ダイジェストも強制生成

# ローカルWebサーバーでフロントエンド動作確認
python -m http.server 8000
# http://localhost:8000 で確認
```

## ファイル構成
```
├── gather.py                  # バックエンド（収集・本文取得・差分解析・グルーピング・保存・通知）
├── gadget.py                  # ガジェット新製品バックエンド（RSS収集・解析・30日蓄積・通知）
├── test_gather.py             # gather.py ユニットテスト
├── test_gadget.py             # gadget.py ユニットテスト
├── index.html                 # IT News フロントエンド（PWA SPA、バニラJS）
├── gadget.html                # Gadget Hub フロントエンド（名寄せ・価格帯フィルタ対応）
├── manifest.json              # PWA マニフェスト
├── sw.js                      # Service Worker（キャッシュ・オフライン対応）
├── requirements.txt           # Python 依存パッケージ（feedparser, requests, pytest）
├── data.json                  # 最新の収集データ（自動生成・スコア降順）
├── gadgets.json               # ガジェット収集データ（直近30日分・自動生成）
├── feed.xml                   # ITニュース RSS 2.0 フィード（スコア上位20件・自動生成）
├── gadget_feed.xml            # ガジェット新製品 RSS 2.0 フィード（新製品上位20件・自動生成）
├── digest.json                # 週間ダイジェスト（毎週日曜に自動生成）
├── archive/                   # 日付別アーカイブ（自動生成・180日保持）
│   ├── index.json             # アーカイブ一覧
│   ├── search_index.json      # 全期間横断検索用統合インデックス
│   └── YYYY-MM-DD.json
├── docs/
│   └── SPEC.md                # 詳細仕様書
├── .github/workflows/
│   └── update.yml             # GitHub Actions 定期実行 & Pages デプロイ
└── README.md                  # 本ファイル
```

## 自動実行スケジュール
- **毎日 2回（JST 7:00 / 15:00）** に定期実行・通知を行います。
  - 朝の部: **JST 6:12（UTC 21:12）** に GitHub Actions が前倒し起動 → **JST 7:00** に通知送信
  - 昼の部: **JST 14:12（UTC 5:12）** に GitHub Actions が前倒し起動 → **JST 15:00** に通知送信
- 実行フロー:
  1. collect ジョブ: `pytest` → `gather.py --no-notify`（9 ソース並列収集・差分解析・既出抑制・ブリーフィング生成）→ `gadget.py --no-notify`（ガジェット収集・専用RSS生成）
  2. 生成データ (`data.json`, `archive/`, `feed.xml`, `gadgets.json`, `gadget_feed.xml` 等) を Git コミット & プッシュし、GitHub Pages へデプロイ
  3. notify ジョブ: 7:00 または 15:00 まで待機して今回新着の IT ニュース通知 → ガジェット通知（最大3件、別メッセージ）
  4. 実行失敗・タイムアウト時は Discord Webhook へ失敗アラートを通知
- **毎週日曜** は週間ダイジェスト (`digest.json`) を自動生成し Discord へ投稿。
- **手動実行**: GitHub の Actions タブ → `IT Info Collector - 定期実行` → `Run workflow`

## デプロイ互換性
- **GitHub Pages**: GitHub Actions 連携による即時自動デプロイ（推奨）
- **Vercel / Cloudflare Pages / Netlify**: リポジトリ連携で静的ホスティング可能（ビルドステップ不要）

## ライセンス
MIT

