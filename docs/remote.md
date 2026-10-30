# 別の PC の Claude Code から使う（lao mcp）

lao で組んだ PC（以下「Spark」）を、LAN 内のメイン PC の Claude Code から道具として使う手順です。
目的は三つです。

- **トークンの節約**：長い文書の要約・下書き・翻訳・画像用プロンプト作り、作った画像や動画の確認を Spark の AI に任せ、Claude には結果の文章だけを返す
- **画像生成**：Spark の ComfyUI で作る（E2）
- **動画生成**：Spark の ComfyUI で作る（E4）。読み上げ音声（E3）と、音声・動画の聞き取り（E1）も使える

## しくみ

```
メイン PC の Claude Code
  └─ ssh spark lao mcp          （MCP を SSH の中で話す。鍵がないと入れない）
       └─ lao serve（Spark の 127.0.0.1:8710）
            ├─ llama-server（対話・理解・下請け）
            └─ ComfyUI（画像・動画）
```

- lao serve と ComfyUI は今まで通り 127.0.0.1 だけで待ち受けます。LAN に開くのは SSH（22 番、同じ LAN のプライベートネットワークからだけ）です。
- GPU・メモリ・推論ロックは lao serve が一手に持ちます。Spark の画面で作業中の依頼とメイン PC からの依頼は順番に動きます。
- 道具に合わせて環境（E1〜E4）を自動で切り替えます。モデルの入れ替えに時間がかかり、その分も生成時間に含まれます。実行中のジョブがあれば、終わるのを待ってから切り替えます。Spark の画面で作業中でも環境は切り替わるので、同時に使うときは気をつけてください。

## 道具

| 道具 | すること | 返すもの |
|---|---|---|
| `generate_image` / `edit_image` | 画像を作る・直す（E2）。`count` で seed を変えて最大 8 枚、`aspect` で縦横比、`style` で保存したスタイル | ref（id@版）、Spark 上の保存場所、seed・大きさ |
| `generate_video` | 画像から動画を作る（E4） | ref、保存場所、実際の秒数・音声の有無 |
| `generate_speech` | 読み上げ音声を作る（E3） | ref、保存場所 |
| `make_clip` | 画像→動画（E4）→読み上げ（E3）→重ねて 1 本の mp4 | ref、保存場所 |
| `combine` | 動画をつなぐ・音声を重ねる（GPU を使わない） | ref、保存場所 |
| `describe_media` | 画像・音声・動画を Spark の AI に見せて答えさせる | 答えの文章 |
| `ask_local` | 下請け（要約・下書き・翻訳・プロンプト作り）。素材は `text`・`refs`・受け取り箱の `inbox`（ファイルもフォルダも、PDF・Office も）。長い素材は区切って要点を抜き出してから答える | 結果の文章 |
| `view` | 縮小版（動画は 4 コマ、`refs` なら画像を並べた 1 枚） | 小さな JPEG |
| `job` | ジョブを待つ・取り消す | 状態を 1〜数行 |
| `inbox` / `import_file` | 受け取り箱を見る・ファイルかフォルダを成果物にする | 一覧、ref |
| `styles` / `save_style` | 画像のスタイル（前置き・避けたい内容・縦横比）の一覧・保存・削除 | 短い文章 |
| `health` | C・D の空き、GPU の温度、読み込み中のモデル、直近の失敗、注意 | 数行 |
| `cleanup` | 作業の残り・古い記録を片づける（既定は一覧だけ。成果物は消さない） | 種類ごとの数と大きさ |
| `usage` | Claude のトークンの記録（月ごと。下の「トークンの記録」） | 数行 |
| `backup` / `unload` / `update` | 会話や設定の zip、モデルを今すぐ下ろす、lao を最新にする（既定は確認だけ。動かなければ元に戻す） | 短い文章 |
| `list_artifacts` / `status` / `switch_environment` | 一覧、状態（健康の要点つき）、構成を指定した切り替え（`preload` で先に読み込む） | 短い文章 |

トークンを節約するため、生成の結果に画像や動画の中身は入れません。中身の確認はまず `describe_media`、見る必要があるときだけ `view` を使います。
生成は既定で 240 秒まで結果を待ち、終わらなければ `job=…` を返します。続きは `job` で待ちます（最長 600 秒ずつ）。

## 任せる順番（重なったとき）

Spark で処理が重なったときは、推論ロックを次の順に渡します（利用者の決まり、`src/lao/lock.py`）。同じ順位の中では来た順で、動いている処理を途中で止めることはしません。

1. **Claude にできないこと**：画像・動画・音声の生成、画像・音声・動画の理解（describe_media）
2. **Claude のトークンを多く使う下請け**：ask_local
3. **それ以外**：Spark の画面での対話、定期実行、先読み

## 会話モデルの扱い

- **生成だけなら会話モデルは動かしません：** モデルは使うときに読み込むので、画像・動画・音声の生成を頼むと生成用のモデルだけが読み込まれます。
- **残っていた会話モデルも生成の前に下ろします：** ask_local などで会話モデルが読み込まれていても、別の PC からの生成（generate_image・edit_image・generate_video・generate_speech）の前に下ろし、生成にメモリを空けます。`[memory] free_chat_for_remote_generation = false` で止められます。Spark の画面での作業（音声会話の読み上げなど）では下ろしません。
- **ask_local が使うモデル：** 既定では今の環境の AI が答えます（E2〜E4 では小さめの会話モデル。モデルの入れ替えが起きないので速い）。`best=true` を付けると E1 の大きい推論モデルに切り替えて答えます。答えたモデル名は結果の末尾に付きます。いつも E1 にしたいときは `[server] ask_local_best = true`。

## 熱・電気・容量の手入れ

- **使わないモデルは下ろす：** `[memory] idle_unload_minutes`（既定 30 分）使われなければ下ろし、次の依頼で読み直します。
- **熱いときの小休止：** 生成の区切りの前に GPU が `[health] cooldown_above_c`（既定 85℃）を超えていたら、`cooldown_to_c`（75℃）まで下がるのを待ちます（最長 10 分、生成の途中では止めない）。休んでいる間は `health` と `status` に出ます。値は実機で測って調整します。
- **容量：** C・D のどちらかが `disk_warn_pct`（90%）を超えると `status`・`health` で知らせます。片づけは `cleanup`（画面では設定 →「片づける…」）。
- **バックアップ：** 会話・プロジェクト・記憶・設定・スキル・スタイルを毎日 `D:\lao\backups` に zip で残し（14 個）、メイン PC が毎日 12:30 に `~/Documents/lao-backup` へ取ってきます（30 個）。

## 準備（Spark 側）

**利用者の「開始」指示の後に行います。** 到着後の作業をまとめて流す `scripts\setup-spark.ps1`（[arrival.md](arrival.md)）がこの準備も行います。以下は手で行うときの手順です。

1. lao を導入し、`lao serve` が動くようにする（README の手順）。
2. メイン PC の公開鍵（`id_ed25519.pub`）を Spark にコピーする（USB メモリなど）。
3. 管理者の PowerShell で、まず確認だけ：
   ```
   powershell -ExecutionPolicy Bypass -File scripts\enable-remote.ps1 -PublicKeyFile .\id_ed25519.pub
   ```
   内容を見てから `-Execute` を付けて実行します。ログオン時に lao serve を起動したいときは `-AutostartServe` も付けます。
4. ネットワークの種類が「プライベート」になっているか確かめます（パブリックのままだと SSH は通りません）。

スクリプトがすること：OpenSSH サーバーの導入と自動起動、パスワードでのログイン禁止（鍵だけ）、ログインできる利用者をこの人だけにする、ファイアウォールを同じ LAN のプライベートネットワークからだけにする、公開鍵の登録、受け取り箱（`D:\lao\inbox`）の作成、lao の画面を開くショートカット「lao」。`-AutostartServe` では PC の起動時（サインイン不要、S4U）とログオン時に lao serve を起動し、落ちたら 1 分おきに 3 回まで起動し直します（起動時の形で GPU が使えないときは `-ServeAtLogonOnly`）。

## トークンの記録

lao がどれだけ Claude のトークンを節約したかを、あとで見返せるように残します（どこにも送りません）。

- **lao の側：** 道具の呼び出しごとに、Claude に返した量・Spark が代わりに読んだ量・節約できた量（＝代わりに読んだ量 − 返した量）の見積もりを `D:\lao\usage\lao-mcp-YYYY-MM.jsonl` に残します。ask_local は受け取り箱や成果物から Spark が読んだ素材（Claude が自分で書いて送った text は数えない）、describe_media は Claude が画像を見る分（1 枚約 1,600、動画は 8 コマ分）を節約として数えます。生成と、音声・動画の理解は「Claude にできないこと」として回数と作業時間を数えます
- **メイン PC の側：** `spark.ps1 usage` が Claude Code の会話記録（`~/.claude/projects`）から日ごとの入力・出力・キャッシュのトークンと lao の呼び出し回数を数え、`D:\lao\usage\claude-code\` へ送ります。送るのは数だけで、会話の中身は送りません。毎日 12:30 のタスクと `/spark-usage` が流します
- **見方：** Claude Code で `/spark-usage`、lao の画面の「ふりかえり」（グラフつき）、Spark で `lao usage --month 2026-11`（`--json` で動画やグラフ用の数字）
- トークンは見積もりです（英数字 4 文字で 1、日本語は 1 文字で 1）。Claude Code の使用量はキャッシュから読んだ分を料金に合わせて 1/10 で数えます

## 準備（メイン PC 側）

Windows なら `scripts\prepare-main-pc.ps1` がすべて行います（[arrival.md](arrival.md)）。利用者がメイン PC で使うのは Claude Code と lao の画面だけで、ssh や scp は Claude が下の `spark.ps1` で行います。

| すること | 中身 |
|---|---|
| 鍵 | `~/.ssh/id_ed25519` が無ければ作る |
| 接続 | `~/.ssh/config` に `Host spark`、初回のホスト鍵の登録、`lao mcp --check` |
| Claude Code | lao の登録（`claude mcp add lao --scope user -- ssh -o ClearAllForwardings=yes spark C:/lao/venv/Scripts/lao.exe mcp`）、MCP の待ち時間 15 分、`~/.claude/CLAUDE.md` に使い方（[main-pc/CLAUDE-lao.md](../scripts/main-pc/CLAUDE-lao.md)）、コマンド `/spark-image` `/spark-video` `/spark-summary` `/spark-status` `/spark-get` `/spark-open` `/spark-usage`、lao の道具と spark.ps1 を確認なしで使う許可 |
| やりとりの道具 | `~/.lao/spark.ps1`（put・get・check・wake・open・tunnel・backup・usage）と `~/.lao/spark.json`（PC 名・IP・MAC） |
| 常駐 | ログオン時に lao の画面用のトンネル（127.0.0.1:8710）を張り続けるタスク `lao-spark-tunnel`、毎日バックアップを取ってきて Claude Code の使用量を送るタスク `lao-spark-backup` |
| 画面 | デスクトップとスタートメニューの「lao（Spark）」（押すと Edge のアプリ窓で lao の画面が開く。API キーは保存せず、開くたびに Spark から受け取る） |

## ファイルのやりとり（Claude が行う）

- メイン PC → Spark：`spark.ps1 put <ファイルかフォルダ>` で受け取り箱（`D:\lao\inbox`）へ送り、表示された名前を `ask_local` の `inbox`（要約など。Claude は中身を読まない）か `import_file`（画像・動画の素材）に渡します。
- Spark → メイン PC：`spark.ps1 get "<生成の結果に出た保存場所>"` で `~/Downloads/lao` に取ってきます。
- つながらないとき：`spark.ps1 check` が、名前が引けない・眠っている・sshd・lao serve のどこが悪いかを出します。眠っていそうなら `spark.ps1 wake`（有線 LAN で Wake-on-LAN。Wi-Fi ではほぼ使えません）。

## lao の画面をメイン PC で使う

デスクトップの「lao（Spark）」を押すか、Claude Code に「lao の画面を開いて」（`/spark-open`）と頼みます。トンネルはログオン時のタスクが張り続けるので、LAN には何も開きません。Edge のアプリ窓で開くので、録音や通知も使えます。

- 添付したファイルはメイン PC から Spark に送られ、成果物は Spark（D:\lao\library）に残ります。ダウンロードはメイン PC に保存されます
- 画面の「PC のフォルダ」で選べるのは Spark 側のフォルダです
- Spark の画面とメイン PC の画面、Claude Code からの依頼は、同じ lao を使うので上の順番で動きます

## ComfyUI の画面をメイン PC で開く

ワークフローを作る・直すときだけ、Claude に `ssh -N -o ClearAllForwardings=yes -L 8730:127.0.0.1:8730 spark` を流してもらい、`http://127.0.0.1:8730` を開きます（LAN には開きません）。ComfyUI は lao が必要なときに起動するので、開けないときは先に画像か動画を一度作ってください。

## 安全のための決まり

- LAN に開くのは SSH だけ。lao serve と ComfyUI を 0.0.0.0 で待ち受けにしない
- パスワードでのログインは禁止。鍵を持つメイン PC だけが入れる
- `D:\lao\server.key`（lao serve の API キー）と `config\machine.toml` は他の利用者に読ませない
- 受け取り箱から取り込めるのは箱の中のファイルだけ（フォルダの外は指定できない）
