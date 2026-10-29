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
| `generate_image` / `edit_image` | 画像を作る・直す（E2） | ref（id@版）、Spark 上の保存場所、seed・大きさ |
| `generate_video` | 画像から動画を作る（E4） | ref、保存場所、実際の秒数・音声の有無 |
| `generate_speech` | 読み上げ音声を作る（E3） | ref、保存場所 |
| `describe_media` | 画像・音声・動画を Spark の AI に見せて答えさせる | 答えの文章 |
| `ask_local` | 下請け（要約・下書き・翻訳・プロンプト作り） | 結果の文章 |
| `view` | 画像・動画の縮小版（動画は 4 コマを並べた 1 枚、長辺 256px） | 小さな JPEG |
| `job` | ジョブを待つ・取り消す | 状態を 1〜数行 |
| `import_file` | 受け取り箱のファイルを成果物にする | ref |
| `list_artifacts` / `status` / `switch_environment` | 一覧、状態、構成を指定した切り替え | 短い文章 |

トークンを節約するため、生成の結果に画像や動画の中身は入れません。中身の確認はまず `describe_media`、見る必要があるときだけ `view` を使います。
生成は既定で 240 秒まで結果を待ち、終わらなければ `job=…` を返します。続きは `job` で待ちます（最長 600 秒ずつ）。

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

スクリプトがすること：OpenSSH サーバーの導入と自動起動、パスワードでのログイン禁止（鍵だけ）、ログインできる利用者をこの人だけにする、ファイアウォールを同じ LAN のプライベートネットワークからだけにする、公開鍵の登録、受け取り箱（`D:\lao\inbox`）の作成。

## 準備（メイン PC 側）

Windows なら `scripts\prepare-main-pc.ps1` が 1〜5 をまとめて行います（[arrival.md](arrival.md)）。

1. 鍵を作る（無ければ）：`ssh-keygen -t ed25519`
2. `~/.ssh/config` に Spark を登録する：
   ```
   Host spark
     HostName <Spark の PC 名か IP アドレス>
     User <Spark の利用者名>
     IdentityFile ~/.ssh/id_ed25519
     BatchMode yes
     ServerAliveInterval 30
   ```
3. つながるか確かめる：
   ```
   ssh spark C:/lao/venv/Scripts/lao.exe mcp --check
   ```
   `OK：lao serve に接続できました` と出れば成功です。
4. Claude Code に登録する：
   ```
   claude mcp add lao --scope user -- ssh -o ClearAllForwardings=yes spark C:/lao/venv/Scripts/lao.exe mcp
   ```
   `ClearAllForwardings` は、下の「lao の画面をメイン PC で使う」で `LocalForward` を足したときに、ブラウザ用の `ssh -N spark` と 8710 番を取り合わないようにするためです。
5. 動画は数分かかるので、Claude Code の MCP の待ち時間を延ばします（`~/.claude/settings.json`）：
   ```json
   { "env": { "MCP_TOOL_TIMEOUT": "900000" } }
   ```

### Claude に使い方を伝える（任意）

メイン PC の `~/.claude/CLAUDE.md` に書いておくと、Claude が自分から任せるようになります。

```markdown
## Spark（lao）の使い方
- 画像・動画・読み上げ音声は lao の道具で作る。結果は ref と保存場所だけ受け取る
- 作った画像・動画の確認は、まず lao の describe_media に任せる。自分で見るのは view で、必要なときだけ
- 長い文書の要約・下書き・翻訳・画像用の英語プロンプト作りは lao の ask_local に任せ、結果だけ使う
```

## ファイルのやりとり

- メイン PC → Spark：`scp ./start.png spark:D:/lao/inbox/` の後、Claude に「start.png を取り込んで動画にして」と頼む（`import_file`）
- Spark → メイン PC：結果に出た保存場所を `scp "spark:D:/lao/library/<id>/<ファイル名>" .` で取ってくる

## lao の画面をメイン PC で使う（Claude を通さずに）

Spark の対話画面（Claude アプリに近い画面）は、メイン PC のブラウザからも使えます。SSH のトンネルで Spark の 127.0.0.1:8710 をメイン PC の 127.0.0.1:8710 につなぐだけで、LAN には開きません。

1. Spark の `config/machine.toml` の `[server] api_key` に固定の値を入れる（空だと lao serve を起動するたびに変わる）
2. メイン PC の `~/.ssh/config` の `Host spark` に 1 行足す：
   ```
     LocalForward 8710 127.0.0.1:8710
   ```
3. メイン PC で `ssh -N spark` を動かしたままにし、Edge で `http://127.0.0.1:8710/?key=<api_key>` を開く
4. Edge の「アプリとしてインストール」で入れると、メイン PC のスタートメニューから開ける（127.0.0.1 なので録音や通知も使える）

- 添付したファイルはメイン PC から Spark に送られ、成果物は Spark（D:\lao\library）に残ります。ダウンロードはメイン PC に保存されます
- 画面の「PC のフォルダ」で選べるのは Spark 側のフォルダです
- Spark の画面とメイン PC の画面、Claude Code からの依頼は、同じ lao を使うので順番に動きます

## ComfyUI の画面をメイン PC で開く

ワークフローを作る・直すときは、SSH のトンネルで開きます（LAN には開きません）。

```
ssh -N -L 8730:127.0.0.1:8730 spark
```

の後、メイン PC のブラウザで `http://127.0.0.1:8730` を開きます。ComfyUI は lao が必要なときに起動するので、開けないときは先に画像か動画を一度作るか、Spark で E2・E4 を選んでください。

## 安全のための決まり

- LAN に開くのは SSH だけ。lao serve と ComfyUI を 0.0.0.0 で待ち受けにしない
- パスワードでのログインは禁止。鍵を持つメイン PC だけが入れる
- `D:\lao\server.key`（lao serve の API キー）と `config\machine.toml` は他の利用者に読ませない
- 受け取り箱から取り込めるのは箱の中のファイルだけ（フォルダの外は指定できない）
