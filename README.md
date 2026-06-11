# Amazon 会員登録自動化ツール (PyAutoGUI + CDP)

Amazon の会員登録フロー（8 ページ）を、nodriver + CDP で DOM を読み取り、
OS レベル（pyautogui / Quartz）で人間的に実入力してステルス自動化する CLI ツール。

`pco-lottery-pyautogui` のアーキテクチャ流用 + `amazon-regist-bot`（Chrome 拡張）の
ページフロー（`content.js`）を移植したもの。

## フロー

```
/register            メール入力         (自動・開始URL)
/ax/claim/intent     インテント確認      (自動)
/register            氏名 + パスワード   (自動)
/ap/cvf/request      ARKOSE CAPTCHA     (★手動: 通知 → 解決待ち)
/ap/cvf/request      メール OTP          (自動: Gmail API から取得)
/ap/cvf/verify       電話番号登録        (自動)
/ap/cvf/verify       SMS OTP            (★手動: 通知 → 入力待ち)
/gp/yourstore 等     完了
```

## セットアップ

```bash
pip install -r requirements.txt
cp config.yaml.template config.yaml   # 初回のみ
```

1. `appscript.gs` を Google Apps Script にコピーしてウェブアプリとしてデプロイ。
2. `config.yaml` を編集:
   - `gas_webapp_url`: デプロイした GAS WebApp URL
   - `sheet_name`: アカウントシート名
   - `notifications.discord_webhook_url`: 任意

### スプレッドシート列構造

| 列 | フィールド | 内容 |
|---|---|---|
| A | `email` | Amazon 登録メール（必須） |
| B | `password` | 登録パスワード（必須） |
| C | `name` | 氏名（必須） |
| D | `phone_num` | 電話番号・国番号なし 例:`9012345678`（電話認証で必須） |
| E | `status` | 処理ステータス（ツールが自動書込み・処理日時を内包） |

- 1 行目はヘッダー、2 行目以降がデータ。国番号は日本固定（+81）のため列に持たない。
- 結果は E 列に `完了 (2026/06/11 12:34)` / `エラー: ...` の形で書き込まれる。
- `resume` は E 列に「完了 / ✓」を含まない行のみ再処理。
2. Gmail OAuth 用 `credentials.json`（Google Cloud Console の OAuth クライアント）を配置。
   初回実行時にブラウザ認証 → `token.json` が生成される。

## 実行

```bash
python main.py bulk      # 全アカウントを処理
python main.py resume    # status に「完了」を含まない未完了のみ処理
```

- 進捗 JSON 出力: `--status-file path.json`
- CAPTCHA / SMS OTP 到達時は OS 通知が出るので手動で解決すると自動継続。

## 動作確認のコツ

- `config.yaml` の `dryrun.enabled: true` にすると、実クリック/入力をログ出力のみにして
  ページ検出・遷移ロジックだけを安全に検証できる。

## モジュール構成

| ファイル | 役割 |
|---|---|
| `main.py` | CLI / オーケストレーション（アカウントループ・リトライ・進捗・通知） |
| `amazon_flow.py` | 8 ページ状態機械（ページ判定 + 各ハンドラ）★中核 |
| `gas_client.py` | GAS（スプレッドシート）連携（fetch / incomplete / record） |
| `gmail_client.py` | メール OTP 自動取得（Gmail API） |
| `chrome_launcher.py` | Chrome 起動 / プロファイルローテーション |
| `cdp_reader.py` | CDP で DOM 読取り |
| `human_input.py` | OS レベル入力合成（マウス / キーボード） |
| `guard.py` | 汎用 WAF/403 検知ゲート + OS 通知 |
| `config_loader.py` | `config.yaml` パース |
| `notifier.py` / `wake_lock.py` / `paths.py` / `utils.py` | 通知 / スリープ防止 / パス解決 / 雑用 |
```
# amazon-regist-pyautogui
