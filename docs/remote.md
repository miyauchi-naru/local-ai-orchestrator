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

**利用者の「開始」指示の後に行います。**

1. lao を導入し、`lao serve` が動くようにする（README の手順）。
2. メイン PC の公開鍵（`id_ed25519.pub`）を Spark にコピーする（USB メモリなど）。
3. 管理者の PowerShell で、まず確認だけ：
   ```
   powershell -ExecutionPolicy Bypass -File scripts\enable-remote.ps1 -PublicKeyFile .\id_ed25519.pub
   ```
   内容を見てから `-Execute` を付けて実行します。ログオン時に lao serve を起動したいときは `-AutostartServe` も付けます。
4. ネットワークの種類が「プライベート」になっているか確かめます（パブリックのままだと SSH は通りません）。

スクリプトがすること：OpenSSH サーバーの導入と自動起動、パスワードでのログイン禁止（鍵だけ）、ログインできる利用者をこの人だけにする、ファイアウォールを同じ LAN のプライベートネットワークからだけにする、公開鍵の登録、受け取り箱（`C:\lao\inbox`）の作成。

## 準備（メイン PC 側）

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
   claude mcp add lao --scope user -- ssh spark C:/lao/venv/Scripts/lao.exe mcp
   ```
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

- メイン PC → Spark：`scp ./start.png spark:C:/lao/inbox/` の後、Claude に「start.png を取り込んで動画にして」と頼む（`import_file`）
- Spark → メイン PC：結果に出た保存場所を `scp "spark:C:/lao/library/<id>/<ファイル名>" .` で取ってくる

## ComfyUI の画面をメイン PC で開く

ワークフローを作る・直すときは、SSH のトンネルで開きます（LAN には開きません）。

```
ssh -N -L 8730:127.0.0.1:8730 spark
```

の後、メイン PC のブラウザで `http://127.0.0.1:8730` を開きます。ComfyUI は lao が必要なときに起動するので、開けないときは先に画像か動画を一度作るか、Spark で E2・E4 を選んでください。

## 安全のための決まり

- LAN に開くのは SSH だけ。lao serve と ComfyUI を 0.0.0.0 で待ち受けにしない
- パスワードでのログインは禁止。鍵を持つメイン PC だけが入れる
- `C:\lao\server.key`（lao serve の API キー）と `config\machine.toml` は他の利用者に読ませない
- 受け取り箱から取り込めるのは箱の中のファイルだけ（フォルダの外は指定できない）
