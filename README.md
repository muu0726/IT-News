# 📡 IT Info Hub — AI技術ニュースダッシュボード

IT技術情報を複数ソースから自動収集し、**Gemini 2.5 Flash-Lite** で解析・スコアリングを行い、Notion風のPWAダッシュボードで閲覧できるシステムです。

## ✨ 主な機能

### 収集・解析（バックエンド）
| 機能 | 説明 |
|------|------|
| **マルチソース収集** | Hacker News, Zenn, Qiita, Reddit, はてなブックマーク, Publickey, Dev.to, GitHub Blog, arXiv cs.AI |
| **本文ベースのAI解析** | 記事本文を取得し、Gemini 2.5 Flash-Lite で3行要約・タグ・カテゴリ・重要度スコアを生成 |
| **バッチ解析** | 6記事を1リクエストで解析し、レート制限（15 RPM）との衝突を回避 |
| **英語記事の日本語化** | 英語のタイトル・要約を自動で日本語に翻訳 |
| **HOT検知** | 複数ソースに出現する記事を自動検知しスコア加算 |
| **関連記事グルーピング** | 埋め込みベクトル（gemini-embedding-001）で同一話題の記事を自動グループ化 |
| **重複除去** | URL正規化で同一記事の重複を自動排除 |
| **週間ダイジェスト** | 毎週日曜に1週間の総括をAI生成（digest.json + Discord投稿） |
| **RSSフィード出力** | 収集結果を `feed.xml` として配信（RSSリーダーで購読可能） |
| **マルチ通知** | スコア80+の記事を Discord / Slack / LINE に自動投稿 |
| **アーカイブローテーション** | 180日を超えた古いアーカイブを自動削除 |

### 閲覧（フロントエンド PWA）
| 機能 | 説明 |
|------|------|
| **検索 & フィルタ** | テキスト検索、タグ・カテゴリ・ソース絞り込み、全アーカイブ横断検索（🌐） |
| **並び替え** | スコア順 / 新着順 / ソース順 |
| **お気に入り・既読管理** | ★登録、既読記事の自動淡色化、未読のみ表示 |
| **キーワード購読** | 登録キーワードにマッチする記事を 📌 でハイライト・絞り込み |
| **トレンド統計** | 直近30日の記事数推移・頻出タグ・ソース別平均スコアをチャート表示 |
| **週間ダイジェスト表示** | AIが生成した週次総括をバナー表示 |
| **新着通知** | ページ表示時に新着高スコア記事をブラウザ通知（オプトイン） |
| **設定の書き出し/読み込み** | お気に入り・キーワード・既読を他ブラウザへ移行 |
| **ダークモード** | OS設定連動＋手動トグル |
| **アーカイブ** | 過去データをプルダウンで切替閲覧 |
| **PWA対応** | オフラインでも閲覧可能 |

## 🚀 セットアップ

### 1. リポジトリ作成 & プッシュ
```bash
git init
git add .
git commit -m "feat: 初期構築"
git remote add origin https://github.com/muu0726/IT-News.git
git push -u origin main
```

### 2. GitHub Secrets 設定
リポジトリの **Settings → Secrets and variables → Actions** で以下を追加:

| Secret名 | 必須 | 説明 |
|-----------|------|------|
| `GEMINI_API_KEY` | ✅ | Google AI Studio で取得した Gemini API キー |
| `DISCORD_WEBHOOK_URL` | — | Discord チャンネルの Webhook URL |
| `SLACK_WEBHOOK_URL` | — | Slack Incoming Webhook URL |
| `LINE_CHANNEL_ACCESS_TOKEN` | — | LINE Messaging API のチャネルアクセストークン（broadcast 送信） |

### 3. GitHub Pages 有効化
**Settings → Pages → Source** で `main` ブランチの `/ (root)` を選択。

### 4. ローカル実行（テスト用）
```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-api-key"
python gather.py                 # FORCE_DIGEST=1 を付けると週間ダイジェストも生成
python -m http.server 8000
# http://localhost:8000 でフロントエンド確認
```

## 📁 ファイル構成
```
├── gather.py                  # バックエンド（収集・解析・通知）
├── index.html                 # フロントエンド（PWA SPA）
├── manifest.json              # PWAマニフェスト
├── sw.js                      # Service Worker
├── requirements.txt           # Python依存パッケージ
├── data.json                  # 最新データ（自動生成）
├── feed.xml                   # RSSフィード（自動生成）
├── digest.json                # 週間ダイジェスト（毎週日曜に自動生成）
├── archive/                   # 日付別アーカイブ（自動生成・180日保持）
│   ├── index.json
│   └── YYYY-MM-DD.json
├── docs/
│   └── SPEC.md                # 仕様書
├── .github/workflows/
│   └── update.yml             # GitHub Actions 定期実行
└── README.md
```

## ⏰ 自動実行スケジュール
- **毎日 午前7時（JST）** に GitHub Actions が自動実行
- **毎週日曜** は週間ダイジェストも生成
- 手動実行: Actions タブ → `workflow_dispatch` ボタン

## 🌐 デプロイ互換性
- **GitHub Pages**: そのまま動作
- **Vercel / Cloudflare Pages**: リポジトリ連携でデプロイ可能（ビルド不要の静的構成）

## ライセンス
MIT
