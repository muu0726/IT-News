# 📡 IT Info Hub — AI技術ニュースダッシュボード

IT技術情報を複数ソースから自動収集し、**Gemini 1.5 Flash** で解析・スコアリングを行い、Notion風のPWAダッシュボードで閲覧できるシステムです。

## ✨ 主な機能

| 機能 | 説明 |
|------|------|
| **マルチソース収集** | Hacker News, Zenn, Qiita, Reddit, はてなブックマーク |
| **AI解析** | Gemini 1.5 Flash で3行要約・タグ・重要度スコアを自動生成 |
| **HOT検知** | 複数ソースに出現する記事を自動検知しスコア加算 |
| **Discord通知** | スコア80+の最重要記事をリッチ形式で自動投稿 |
| **ダークモード** | OS設定連動＋手動トグル |
| **検索 & フィルタ** | テキスト検索、タグクリック絞り込み |
| **お気に入り** | ★クリックでお気に入り登録（ブラウザに保存） |
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

| Secret名 | 説明 |
|-----------|------|
| `GEMINI_API_KEY` | Google AI Studio で取得した Gemini API キー |
| `DISCORD_WEBHOOK_URL` | Discord チャンネルの Webhook URL（任意） |

### 3. GitHub Pages 有効化
**Settings → Pages → Source** で `main` ブランチの `/ (root)` を選択。

### 4. ローカル実行（テスト用）
```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-api-key"
python gather.py
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
├── archive/                   # 日付別アーカイブ（自動生成）
│   ├── index.json
│   └── YYYY-MM-DD.json
├── .github/workflows/
│   └── update.yml             # GitHub Actions 定期実行
└── README.md
```

## ⏰ 自動実行スケジュール
- **毎日 午前6時（JST）** に GitHub Actions が自動実行
- 手動実行: Actions タブ → `workflow_dispatch` ボタン

## 🌐 デプロイ互換性
- **GitHub Pages**: そのまま動作
- **Vercel / Cloudflare Pages**: リポジトリ連携でデプロイ可能（ビルド不要の静的構成）

## ライセンス
MIT
