# local-ai-orchestrator（lao）

RTX Spark（128GB 統合メモリ、Windows 11 on Arm）1台で、四つの目的別ローカルAI環境を構築し、一つの対話画面から使うためのツールです。

| 環境 | できること |
|---|---|
| E1 推論 | 画像・音声・動画（音声つき）を読み、相談・推論する |
| E2 画像生成 | 参考画像を見ながら画像を作り、修正する |
| E3 音声生成 | 参考音声の話し方で日本語の文章を読み上げる |
| E4 動画生成 | 画像を元に短い動画を作る（音声つき構成あり） |

企画と前提は [docs/plan.md](docs/plan.md)、実機入手前の準備は [docs/prep.md](docs/prep.md)、届いた後の手順は [docs/arrival.md](docs/arrival.md) にあります。

## 方針

- 対話モデルは llama.cpp（llama-server）を直接使います。**Ollama は一切使いません。**
- ロード・アンロード・推論ロック・メモリ予算は本ツールが管理します。推論は補助モデルを含めて一度に一つです。
- 予算を超えそうな依頼は**実行前に**止めます。無断で量子化したり、品質を下げたりはしません。
- 環境はユーザーが選びます。勝手に別の環境やクラウドへ切り替えません。
- 素材や会話を外部へ送りません。API はループバック（127.0.0.1）だけで待ち受けます。

## 実機が届いたらやること

コードは揃っています。実機では「検証して、設定値を埋める」だけです。開封から、画面を閉じたままメイン PC の Claude Code で使えるようになるまでの通しの手順は [docs/arrival.md](docs/arrival.md) にあります。そこでは以下を `scripts\setup-spark.ps1` で Spark 上の Claude がまとめて流します。以下は手で行うときの手順です。**ユーザーの「開始」指示があるまで、以下は実行しません。**

1. **導入（確認モードで内容を見てから実行）**
   - ネイティブ：`powershell -ExecutionPolicy Bypass -File scripts\install-native.ps1` → 内容を確認 → `-Execute` を付けて実行
   - WSL2 案：`bash scripts/install-wsl2.sh` → `--execute`
   - どちらを主に使うかは手順 3 の結果で決めます（[docs/plan.md](docs/plan.md) 2.2 節）。
2. **点検**：`lao doctor`（Arm64、メモリ、ドライバ、llama-server、ffmpeg、Ollama 不使用、カタログの整合）
3. **測定**：`lao p0 run --gpu-python <PyTorch入りのPython>`
   - GPU が実際に確保できるメモリ量、各モデルのロード時間・速度・ピークメモリを測り、`p0-results/` に保存します。
4. **設定値の提案**：`lao p0 propose` → `config/machine.proposed.toml` ができるので、確認して `config/machine.toml` に反映します。
5. **カタログの TODO を埋める**（`grep -rn "TODO(P0)" catalog config scripts`）
   - モデルの配布元・ファイル・ライセンスを確認し、`status = "verified"` にします。
   - ComfyUI で画像・動画のワークフローを作り、「API 形式で書き出し」て `catalog/workflows/` に置き、`bindings` を埋めます（[docs/comfyui-workflows.md](docs/comfyui-workflows.md)）。
   - TTS エンジンを選び、[docs/tts-contract.md](docs/tts-contract.md) の形の中継を用意します。
6. **モデル取得**：`lao plan e1`（確認のみ）→ `lao install e1 --execute`
7. **起動**：`lao serve --env e1` → 表示された URL を開きます。
8. **意味検索（使うときだけ）**：`lao install embedding --execute` → `[embedding]` で `enabled = true`
9. **コード実行（使うときだけ）**：`scripts\install-sandbox.ps1`（確認のみ）→ `-Execute` → `lao sandbox-check` がすべて合格したら、`machine.toml` の `[sandbox]` で `enabled = true` にします。

## 設定するパラメータ

| 場所 | 値 | 決め方 |
|---|---|---|
| `config/machine.toml` `[machine] runtime` | `native` か `wsl2` | P0 でエンジンが揃った方 |
| `[memory] budget_gb` | AI 用の予算（初期 96GB） | ユーザーが決める |
| `[memory] gpu_allocatable_gb` | GPU が実際に確保できた量 | `lao p0 propose` |
| `[measured]` | 部品ごとのピーク・速度 | `lao p0 propose` |
| `[gpu] watchdog_seconds` | 推論・生成の打ち切り時間（GPU タイムアウト対策） | `lao p0 propose` |
| `[gpu] video_segment_seconds`、`tts_chunk_chars` | 長い生成の区切り | P0 のタイムアウト頻度を見て決める |
| `[engines.*]` | 各エンジンの場所とコマンド | 導入スクリプトの結果 |
| `[policy] allow_third_party_quantized` | 第三者の量子化版を使うか（初期は不可） | ユーザーが決める |
| `catalog/*.toml` | モデル、配布元、ハッシュ、ワークフローの差し込み先 | P0 で確認 |

## 本物のモデルなしで試す

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/lao serve --fake --env e2     # ダミーエンジンで画面と流れを確認
.venv/bin/pytest                        # テスト
```

## 画面

`lao serve` を起動し、表示された `http://127.0.0.1:8710/?key=…` を Edge で開きます。Claude アプリに近い作りです。

- 左：新しい会話、会話の一覧（環境ごとの印つき）、成果物ライブラリ、メモリ使用量
- 中央：会話。作業の記録（折りたたみ）と、作った画像・音声・動画のカードが返答の中に出ます。添付はドラッグ＆ドロップか貼り付け。生成中は停止ボタンで取り消せます
- 右：成果物の表示、版の切り替え、作った条件（モデル・プロンプト・シード）、添付と保存
- 会話は作った環境に結び付きます。別の環境の会話を開くと、切り替えてよいか確かめます（勝手に切り替えません）
- 返答は文字が順に出ます（ストリーミング）。思考するモデルは「思考の過程」を折りたたみで見られ、思考の強さ（軽め・普通・深く）を選べます（gpt-oss のみ）
- 会話の操作：発言の編集と送り直し、作り直し、ここから分岐、コピー、ピン留め、名前の変更、プロジェクトへの移動、Markdown で書き出し、検索（Ctrl+K）
- シークレット会話（◌）：保存せず、メモリも使いません
- 長い会話：文脈の 85% を超えると前半を自動で要約して続けます。上部に文脈の使用量が出ます
- コードと文書：HTML・SVG・Markdown・長いコードは右に開き、HTML は外部に通信できない枠の中で動かして表示します。編集して新しい版として保存、ダウンロード、エラーの直しを頼む、選んだ部分の直しを頼む
- ふりかえり：月ごとに、発言の数・使った日と連続日数・時間帯と曜日・環境・作った成果物・よく使ったコマンドと会話をまとめます（この PC の記録だけ。シークレットの会話は入りません）。「この月を AI とふりかえる」で、数字をもとに AI がまとめます
- 休憩の声かけ：続けて使った時間が設定の分数（既定 90 分）を超えたとき、夜中（0〜5 時）に使ったときに、入力欄の上で休憩をすすめます。設定で「しない」にもできます
- 音声会話：入力欄の「声」で始めます。話し終わると（1.3 秒黙ると）自動で送り、AI が録音を聞いて話し言葉で短く答え、E3 ではその返答を読み上げてまた聞き取りに戻ります。E1 では返答を文字で表示します。録音と音声はこの PC の中だけで扱い、ブラウザの音声認識（外部に送るもの）は使いません。一往復ずつの会話で、同時に話す通話には対応しません。聞き取りと読み上げの速さ・日本語の質は P0 で確かめます
- 動く成果物：React（```jsx・tsx）、Mermaid の図、HTML を右で動かします。React・Recharts・lucide-react・Chart.js・d3・three.js・Tailwind などの部品は `src/lao/web/vendor` に同梱してあり、ネットなしで使えます（HTML の CDN 読み込みも手元の部品に置き換えます）。成果物の中から `window.claude.complete("問い")` で AI に問い合わせでき、最初の 1 回は画面で許可を求めます。部品の版は `scripts/fetch-vendor.py` で管理します
- 添付：画像・音声・動画・テキスト・PDF・Word・Excel・PowerPoint、画面の取り込み、録音（E1）。長い資料は質問に近い部分だけを抜き出して渡します
- ファイルを作る：「Word で報告書を」「Excel で集計表を」「スライドにして」「PDF で」と頼むと、AI が中身を書き、この PC の中で docx・xlsx・pptx・PDF に変換します（AI にプログラムは実行させません）。直しを頼むと同じファイルの新しい版になります。PDF の日本語は PC のフォント（游ゴシック・メイリオなど、`LAO_PDF_FONT` で指定も可）を埋め込みます
- コード実行：計算・集計・グラフ・ファイルの変換を、AI が Python で行います。コードはネットにつながらない箱（bubblewrap）の中で動き、PC のファイルも見えません。渡した成果物だけが入り、作ったファイルは成果物として残ります。スキルに .py などのファイルを付けておくと、箱の中で使えます（プラグインのスキルは文章だけ）
- PC のフォルダ：設定で許可したフォルダだけを AI が一覧・読み取りできます（読むだけ／読み書きをフォルダごとに選択）。書き込み・保存・移動・削除は AI が提案し、返答の中の「実行する」を押したときだけ行います。削除は消さずに lao のごみ箱へ移します
- 定期実行：「毎朝 8 時に〜して」と頼むか、定期実行の画面で登録すると、その時刻に依頼を送り、結果を新しい会話に残して知らせます。PC とこのツールが動いている間だけで、登録した環境が選ばれていないときは切り替えずに見送ります
- 資料を探す・リサーチ：AI は成果物ライブラリとプロジェクトの資料から関係する部分を探して答えます（`search_documents`）。`/リサーチ 問い` で、問いを分けて何度も探し、出典つきの報告にまとめます。ライブラリの検索欄でも中身を探せます。インターネットは使いません。意味検索（言葉が違っても意味が近いものを探す）は、埋め込みモデルを `lao install embedding --execute` で取得し、`machine.toml` の `[embedding]` を有効にすると加わります
- プロジェクト：指示と資料をまとめ、その中の会話すべてに効かせます
- スキル・コマンド・担当（サブエージェント）・プラグイン：手順書（SKILL.md）を AI が必要な時に読みます。`/名前` で決まった依頼文を呼べます。調べものなどを担当に任せられます（GPU が 1 枚のため順番に動きます）。プラグインは zip で取り込み・書き出しでき、文章だけを扱い、プログラムは取り込みも実行もしません
- 設定：あなたについて、応答スタイル（標準・簡潔・説明的・学習・丁寧と自作）、出力の上限、メモリ（一覧・編集・取り込み・書き出し・全消去）、テーマ、文字の大きさ、終わった時の通知、ショートカット一覧
- アプリとして入れる：Edge のアドレスバーの「アプリを使用できます」→ インストール。スタートメニューとタスクバーから独立した窓で開けます（PWA。中身はこの PC 内のサーバーで、外部には送りません）

## 別の PC の Claude Code から使う

メイン PC の Claude Code から、SSH 越しに `lao mcp` を道具として使えます。画像・動画・読み上げ音声の生成と、要約・翻訳・仕上がりの確認などの下請けをこの PC に任せ、Claude のトークンを節約します。LAN に開くのは SSH だけで、lao と ComfyUI は 127.0.0.1 のままです。同じ SSH のトンネルで、この画面そのものをメイン PC から使うこともできます。準備（`scripts\enable-remote.ps1`、`scripts\prepare-main-pc.ps1`）と使い方は [docs/remote.md](docs/remote.md) にあります。

利用者が使うのは、Spark では lao の画面だけ、メイン PC では Claude Code と lao の画面だけです。ファイルの受け渡し（`spark.ps1 put`・`get`）、つながらないときの点検と Wake-on-LAN、更新（`update`）、片づけ（`cleanup`）、毎日のバックアップは Claude かタスクが行います。

- 重なったときの順番：Claude にできないこと（生成・理解）→ Claude のトークンを多く使う下請け（ask_local）→ それ以外
- 熱・電気：使わないモデルは 30 分で下ろし、GPU が熱いときは生成の区切りで小休止します
- トークンの記録：lao に任せた分の節約の見積もりと、メイン PC の Claude Code の実際の使用量を月ごとにまとめます（`/spark-usage`、画面のふりかえり、`lao usage`）
- 画像：`count` でまとめて作って並べて比べ、絵柄は名前つきのスタイルで呼べます。動画には `make_clip` で読み上げを重ねられます

## Open WebUI などから使う

`lao serve` は OpenAI 互換の `/v1/chat/completions` を出します。既存の UI から「OpenAI 互換の接続先」として `http://127.0.0.1:8710/v1` と API キーを登録できます（Ollama 連携は無効にしてください）。環境の切り替えは本ツールの画面か `/api/environment` で行います。

## 構成

```
catalog/        構成カタログ（E1〜E4。モデル・配布元・ライセンス・メモリ見積もり）
config/         実機の設定（machine.example.toml をコピーして使う）
scripts/        導入スクリプト（既定は確認のみ）。spark.ps1 と main-pc/ はメイン PC に置くもの
src/lao/
  config.py       設定とカタログの読み込み、必須能力・量子化ルールの検査
  memory.py       統合メモリの予算と受付判定
  lock.py         共通の推論ロック（優先度つき）
  maintenance.py  健康状態、片づけ、バックアップ
  usage.py        Claude のトークンの記録（節約の見積もりと Claude Code の使用量）
  updater.py      lao update（git pull、再起動、失敗したら元に戻す）
  image_styles.py 画像のスタイル
  runtime.py      部品のロード・アンロード（予算内で、構成の中だけ）
  orchestrator.py 会話→ツール呼出し→ジョブ→説明の流れ
  tools.py        ツールの定義と引数検証
  jobs.py         ジョブ（状態の保存、取消、再起動時の中断扱い）
  library.py      四環境共通の成果物フォルダ（版と由来）
  media.py        ffmpeg による検査・分割
  installer.py    モデル取得（再開・ハッシュ照合）
  server.py       API と画面の配信
  mcp.py          別の PC の Claude Code 向け MCP サーバー（lao mcp）
  conversations.py 会話の保存
  web/            画面（index.html、PWA の manifest と service worker）
  backends/       llama-server、ComfyUI、TTS、ダミー
  p0/probe.py     実機の測定と設定値の提案
```
