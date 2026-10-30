# 実機が届いたら（RTX Spark 到着後の手順書）

RTX Spark 128GB（Windows 11 on Arm、ノート型）が届いてから、蓋を閉じたままメイン PC の Claude Code で使えるようになるまでの手順です。

- **手作業は最小限にします。** 利用者が行うのは、Windows の初期設定と、Spark に Claude Code を入れて Remote Control でつなぐところまでです。残りは Spark 上の Claude が `scripts\setup-spark.ps1` で流し、判断が要るところだけ利用者に聞きます。
- **利用者の「開始」指示があるまで、どの手順も実行しません。**（メイン PC の準備も同じです）
- 置き場所：C（1TB）にモデル・エンジン・Python 環境、D（2TB）に成果物・会話・ジョブ・ログ（[prep.md](prep.md) 1.2 節）
- 「Spark」は届いた PC、「メイン PC」はいつも使っている PC です。

## 全体の流れ

| 章 | いつ | 誰が | すること |
|---|---|---|---|
| 0 | 届く前 | 利用者／Claude | アカウントと素材の用意。メイン PC の鍵の用意（「開始」の後） |
| 1 | 届いた日 | 利用者 | Windows の初期設定、Claude Code の導入、Remote Control でつなぐ（30〜60 分） |
| 2 | 届いた日 | Spark の Claude | 電源・蓋の設定、導入、SSH、点検を一度に流す（ビルドの待ちで数時間） |
| 3 | 1〜2 週 | Spark の Claude（判断は利用者） | モデルの取得と測定、画像・動画・音声の準備、評価 |
| 4 | 2 の後 | メイン PC の Claude | メイン PC から Spark につなぎ、蓋を閉じて確かめる |

## 0 届く前に済ませておくこと

### 0.1 利用者が行うこと

- [ ] Hugging Face のアカウントを作り、利用条件への同意が要るモデル（Gemma 4 など）の同意を済ませる。**読み取り用のアクセストークン**を作っておく（1 章で Spark に入れる。会話には貼らない）
- [ ] 評価用の素材を集める（自分の声の録音、非発話音を含む短い動画など。[prep.md](prep.md) 4 章）。他人の声や顔を使うなら本人の同意を得る
- [ ] できれば USB-C の有線 LAN アダプタ。蓋を閉じて長く使うなら Wi-Fi より安定します
- [ ] Spark の利用者名を決めておく（**半角英数字、空白なし**。例：`naru`）

### 0.2 メイン PC の準備（「開始」の後なら、届く前に済ませてよい）

メイン PC（Windows）の Claude Code か利用者が、リポジトリの `scripts\prepare-main-pc.ps1` を流します。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -AddClaudeMd            # 確認のみ
powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -AddClaudeMd -Execute   # 実行
```

- SSH の鍵が無ければ作り、**公開鍵の 1 行**を表示します（クリップボードにも入ります）。公開鍵は秘密ではないので、このプロジェクトの会話に貼ってください。2 章で Spark の Claude が使います。
- Claude Code の MCP の待ち時間を 15 分に延ばし（動画の生成を待てるように）、`~/.claude/CLAUDE.md` に lao の使い方を書き、コマンド（`/spark-image` など）と Spark とのやりとりの道具 `~/.lao/spark.ps1` を入れます（[remote.md](remote.md) の「準備（メイン PC 側）」）。

### 0.3 リポジトリで済ませたこと

- 到着後の作業を一度に流すスクリプト（`scripts\setup-spark.ps1`）と、メイン PC 側のスクリプト（`scripts\prepare-main-pc.ps1`）。どちらも既定は確認のみで、済んだ手順は飛ばすので、途中で止まっても同じコマンドで続きから進みます。
- 設定ファイルの自動記入（`lao configure`）。ffmpeg と llama-server の場所、固定の API キー、P0 の測定値をコメントを残したまま書き込みます。
- 導入の順番の修正：CUDA Toolkit が無いまま llama.cpp をビルドしないようにしました（Build Tools → CUDA → llama.cpp の順）。
- カタログの配布元を公開情報で確かめました（2026-10-29）。結果は 3.1 節にあります。

## 1 届いた日に利用者が行うこと

1. **AC 電源につないで**初期設定を進める。利用者名は 0.1 で決めた半角英数字にする。
2. BitLocker（デバイスの暗号化）が有効なら、回復キーを Microsoft アカウントか紙に保存する。
3. Windows Update をすべて済ませ、再起動する（ドライバもここで入る分だけにし、別に更新しない）。
4. 家の LAN につなぐ（有線があれば有線）。
5. PowerShell（普通の権限）で、Git と Claude Code を入れ、Hugging Face のトークンを入れる：
   ```powershell
   winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
   # 新しい PowerShell を開いてから（Claude Code の公式の手順。Arm64 版の手順が別にあればそちらに従う）
   irm https://claude.ai/install.ps1 | iex
   # Hugging Face のトークン（この PC の利用者の環境変数に入れる。会話やファイルには書かない）
   [Environment]::SetEnvironmentVariable("HF_TOKEN", "<0.1 で作ったトークン>", "User")
   New-Item -ItemType Directory -Force C:\lao | Out-Null
   ```
6. Windows Terminal を**右クリック →「管理者として実行」**で開き、Claude Code にサインインしてから Remote Control でつなぐ。導入スクリプトが管理者の権限を要るためです（OpenSSH サーバー、電源設定、ファイアウォール）。
   ```powershell
   cd C:\lao
   claude            # 初回のサインイン。済んだら /exit
   claude remote-control
   ```
7. このプロジェクトの会話で「開始」と伝える。ここから先は Spark の Claude が進めます。

**2 章の途中で利用者の手が要るところ：** リポジトリの取得の時にブラウザで GitHub にサインインする（非公開のリポジトリのため、初回だけ）、管理者の確認画面が出たら「はい」、WSL2 を試す場合の再起動の了承。

## 2 Spark の Claude が行うこと（setup-spark.ps1）

1. リポジトリを `C:\lao\local-ai-orchestrator` に取得する。
2. **確認モード**で流し、表示を読む。CPU が ARM64、メモリが 128GB 前後、C が約 1TB・D が約 2TB、ネットワーク名が家の LAN であることを確かめ、違えば利用者に聞く。
   ```powershell
   cd C:\lao\local-ai-orchestrator
   powershell -ExecutionPolicy Bypass -File scripts\setup-spark.ps1 -PublicKey "<0.2 の公開鍵の 1 行>"
   ```
3. 同じコマンドに `-Execute` を付けて実行する。記録は `D:\lao\logs\setup-<日時>.txt` に残ります。
4. 結果（PC 名、利用者名、IP、GPU が確保できた量、`lao doctor` の NG）を会話で報告する。

### 2.1 setup-spark.ps1 がすること

| 段階 | すること |
|---|---|
| 記録 | PC 名・利用者名・Windows の版・メモリ・C/D の容量・ネットワーク・IP を表示する |
| power | 下の 2.2 節（蓋を閉じても動き続ける設定） |
| install | `scripts\install-native.ps1`：フォルダ（C:\lao、D:\lao）、Python 3.12（Arm64）、ffmpeg、Git・CMake・Build Tools、CUDA Toolkit（`-CudaInstaller` を渡したとき）、lao（`C:\lao\venv`）、llama.cpp の CUDA ビルド、ComfyUI と PyTorch（CUDA）、`config\machine.toml` の作成と自動記入 |
| remote | `scripts\enable-remote.ps1`：OpenSSH サーバー（鍵だけ、この利用者だけ、同じ LAN のプライベートネットワークからだけ）、公開鍵の登録、受け取り箱 `D:\lao\inbox`、PC の起動時（サインイン不要）とログオン時に lao serve を起動して落ちたら起動し直すタスク、デスクトップの「lao」（[remote.md](remote.md)） |
| check | `lao doctor`、GPU が確保できる量の測定（モデルなし）、lao serve の起動と `lao mcp --check` |

### 2.2 蓋を閉じても動き続ける設定（power の段階）

Spark は蓋を閉じて置いたまま、メイン PC から使う前提です。**AC 電源のときだけ**眠らないようにし、バッテリーのときは今まで通り眠ります。

- 蓋を閉じたときの動作（AC）を「何もしない」、スリープと休止状態（AC）を「なし」、画面は 5 分で消す（`powercfg`）
- 今のネットワークを「プライベート」にする（パブリックのままだと SSH が通らない。**家の LAN で実行すること**）
- ネットワーク アダプターの「電力の節約のために電源をオフにできる」を外す（変えられない機種もある）
- 有線 LAN のアダプターで Wake-on-LAN（マジック パケット）を有効にする（メイン PC の `spark.ps1 wake` で起こせるように）
- Windows Update のアクティブ時間を 8 時〜2 時にする（この間は勝手に再起動しない）

lao の側で行うことと、スクリプトで変えられないことは次のとおりです。

- **再起動の後：** lao serve は PC の起動時にサインインなしで起動します（タスクの S4U）。この形で GPU が使えるかは 3 章で確かめ、使えなければ `-ServeAtLogonOnly` で流し直します（その場合は再起動の後に一度サインインが要ります）。長い生成の前は Windows Update の「更新の一時停止」を使うと安全です。
- **熱：** 蓋を閉じると放熱が落ちる機種があります。硬く平らな所に置き、吸気口・排気口をふさがない（布団や鞄の中に置かない）。lao は生成の区切りごとに GPU の温度を見て、85℃ を超えていたら 75℃ まで下がるのを待ってから次へ進みます（[remote.md](remote.md) の「熱・電気・容量の手入れ」）。使わないモデルは 30 分で下ろします。
- **バッテリー：** つなぎっぱなしで使うので、メーカーのアプリに充電の上限（80% 前後）があれば設定します。機種ごとのアプリなので、Spark の Claude が場所を調べて利用者に伝えます。

### 2.3 止まったとき

- **CUDA Toolkit が無い：** llama.cpp のビルドを飛ばして先へ進みます。Claude が NVIDIA の開発者サイトから CUDA Toolkit 13.4（Windows Arm64）のインストーラーを取り、`-CudaInstaller <ファイル>` を付けてもう一度流します（ドライバは更新しません）。
- **llama.cpp のビルドが失敗した：** Windows Arm64 の CUDA ビルドは未確認の部分です（[prep.md](prep.md) 2 章）。表示を記録し、WSL2 案（[plan.md](plan.md) 2.2 節）を試すかを利用者に聞きます。了承があれば `wsl --install`（再起動あり）→ WSL の中で `nvidia-smi` を確かめ → `bash scripts/install-wsl2.sh --execute`、`lao configure --set machine.runtime=wsl2`。
- **途中でエラー：** 原因を直して同じコマンドを流し直します。済んだ手順は飛ばします。

## 3 モデルと測定（Spark の Claude が行い、決めることは利用者に聞く）

[prep.md](prep.md) 5 章の P0 にあたります。決めることの一覧は [plan.md](plan.md) 15 章です。

1. **カタログを確かめる：** `git grep -n "TODO(P0)" catalog`。配布元・ファイル・ライセンスを確かめ、`lao plan e1` で取得するファイルと大きさを見ます。リポジトリに量子化の違うファイルが並んでいるときは `files` を使う 1 つに絞ります。
2. **モデルを取得して測る：**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup-spark.ps1 -Phase models -Envs e1 -Execute
   ```
   取得（C:\lao\models）→ 測定（`p0-results\`）→ `lao p0 propose` → `lao configure --apply-proposed`（GPU の上限、部品ごとのピーク・速度、打ち切り時間を machine.toml に採用）。E2〜E4 も同じです。全構成で約 400GB あるので、使う環境から順に取ります。
3. **画像・動画（E2・E4）：** ComfyUI でワークフローを作り、API 形式で書き出して `catalog/workflows/` に置き、`bindings` を埋めます（[comfyui-workflows.md](comfyui-workflows.md)）。
4. **読み上げ（E3）：** TTS の候補を選び、[tts-contract.md](tts-contract.md) の形の中継を用意して `[engines.tts]` に書きます。
5. **評価：** 日本語の評価課題（[prep.md](prep.md) 4 章）を各候補で行います。仕上がりの良し悪しは利用者が見て決めます。
   - **会話モデル＋生成モデルの組み合わせ：** `lao p0 pairs`。会話モデルが lao のツールで ComfyUI を使い、日本語の依頼どおりの絵・動画にできるかを、メモリと合わせて比べます（課題と候補は `catalog/shared/pairs.toml`）。見るのは、初回の再現度、引数の選び方（縦横比・避けたい内容・修正と新規の使い分け）、出来た画像を見て直す力、修正指示への追従、動画の動きの指定です。組み合わせごとのピークメモリと読み直しの回数も測り、予算（`reserve_gb` で他のために空ける量を引いたもの）に収まる中から、品質が最上位から `tie_points` 以内で一番軽い組み合わせを推します。結果は `p0-results\pairs-*\report.md`。
   - 判定は、生成が全部終わった後に、画像を見られるモデル全部（`judge = "all"`）が一つずつ「はい／いいえ」で答えます。判定役ごとに採点して中央値で順位を付け、組み合わせ自身の会話モデルはその組み合わせの判定から外します。判定役どうしの一致率と、判定役ごとの点のばらつきも表に出ます。Claude のトークンは使いません。
   - 外部に判定させたいときは `--no-judge` で生成だけ行い、`judge-sheet.json` の答えを `answers.json`（`{"鍵": [true, false, ...]}`）に書いて `lao p0 pairs --score <フォルダ> --judge <名前>` で採点します。
   - 組み合わせ数 × 課題 20 件 × 最大 4 枚なので数時間かかります。`--only gemma` のように名前で絞れます。
6. **使い勝手の確認（実機でしか分からないこと）：**
   - Spark を再起動し、**サインインせずに**メイン PC から `lao の status を見て` が通るか。通っても画像を 1 枚作り、GPU が使えているか（遅すぎないか）を見る。だめなら `setup-spark.ps1 -Phase remote -ServeAtLogonOnly -Execute`
   - `nvidia-smi --query-gpu=temperature.gpu,clocks_event_reasons.hw_thermal_slowdown --format=csv` が値を返すか（返さなければ小休止と温度の知らせは働かない）。蓋を閉じて 30 分の連続生成を行い、温度と速度の変化から `[health] cooldown_above_c`・`cooldown_to_c` を決める
   - 使わないモデルを下ろしたとき、次の依頼での読み直しに何秒かかるか。長すぎれば `[memory] idle_unload_minutes` を延ばす
   - メイン PC から `spark.ps1 wake`（有線 LAN のとき。バッテリーで眠らせてから試す）

**利用者に聞いて決めること：** AI 用の予算（`[memory] budget_gb`、初期 96GB）、主に使う実行基盤（native か wsl2）、各環境の標準構成、第三者の量子化版を使うか（`[policy] allow_third_party_quantized`、初期は使わない）、UI を既存 OSS で済ませるか。決まった値は `lao configure --set 表.鍵=値` で書きます。

### 3.1 事前に確かめた配布元（2026-10-29、Hugging Face の公開ページ）

| カタログ | リポジトリ | 結果 |
|---|---|---|
| e1・e3・e4 の gemma-4-E4B-it | [ggml-org/gemma-4-E4B-it-GGUF](https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF) | ある。Q4_0 などの量子化版と mmproj が並ぶので、`files` を絞る（BF16 か、配布元の量子化か） |
| e2 の gemma-4-26B-A4B-it | [ggml-org/gemma-4-26B-A4B-it-GGUF](https://huggingface.co/ggml-org/gemma-4-26B-A4B-it-GGUF) | ある。mmproj と mtp のファイルもあるので `files` を絞る |
| e1-omni の Qwen3-Omni-30B-A3B-Instruct | [ggml-org/Qwen3-Omni-30B-A3B-Instruct-GGUF](https://huggingface.co/ggml-org/Qwen3-Omni-30B-A3B-Instruct-GGUF) | ある |
| e2 の Qwen-Image | [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/tree/main/split_files) | ある。`split_files/` に diffusion_models（bf16・fp8・nvfp4、2512 版）と text_encoders（qwen_2.5_vl_7b） |
| e4-wan の Wan2.2-I2V-A14B | [Comfy-Org/Wan_2.2_ComfyUI_Repackaged](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged) | ある。`split_files/diffusion_models/` に i2v の high_noise・low_noise（14B、fp8_scaled）など |
| e4-ltx の LTX | カタログは `Lightricks/LTX-2.5` | **見つからない。** 公式は [Lightricks/LTX-2](https://huggingface.co/Lightricks/LTX-2)（19B、fp8 あり）と [Lightricks/LTX-2.3](https://huggingface.co/Lightricks/LTX-2.3)・[LTX-2.3-fp8](https://huggingface.co/Lightricks/LTX-2.3-fp8)（22B）。到着後にどちらにするかを決めてカタログを直す |
| 意味検索の Qwen3-Embedding-0.6B | [Qwen/Qwen3-Embedding-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF) | ある。`Qwen3-Embedding-0.6B-f16.gguf` |
| e1-lfm3-omni の LFM3-Omni | 未定 | 配布元を到着後に確かめる |

## 4 メイン PC から使う（最後につなぐ）

メイン PC の Claude Code か利用者が、2 章の報告にある PC 名と利用者名で流します。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\prepare-main-pc.ps1 -SparkHost <PC 名か IP> -SparkUser <利用者名> -Execute
```

- `~/.ssh/config` に `Host spark` を足し、初回の接続で Spark のホスト鍵を登録し、`lao mcp --check` でつながるか確かめ、Spark の IP と MAC を記録し、Claude Code に lao を登録し、lao の画面用のトンネルと毎日のバックアップのタスク、デスクトップの「lao（Spark）」を作ります。
- 確かめ方：Claude Code で「lao の status を見て」→ `/spark-image 猫` → `/spark-get さっきの画像`。
- lao の画面：デスクトップの「lao（Spark）」か `/spark-open`。以後、メイン PC で使うのは Claude Code と lao の画面だけです。
- **蓋を閉じて確かめる：** Spark の蓋を閉じ、5 分ほど待ってから、もう一度「lao の status を見て」。通れば 2.2 節の設定が効いています。

使い方とファイルのやりとりは [remote.md](remote.md) にあります。

## 5 必要なときだけ

- **意味検索：** `lao install embedding --execute` → `lao configure --set embedding.enabled=true`
- **コード実行：** `scripts\install-sandbox.ps1`（確認のみ）→ `-Execute` → `lao sandbox-check` がすべて合格したら `lao configure --set sandbox.enabled=true`（WSL が要ります）

## 困ったとき

| 起きたこと | 見るところ |
|---|---|
| メイン PC から Spark につながらない | Claude Code に「/spark-status」。`spark.ps1 check` が、名前が引けない（ルーターで IP を固定し `-SparkHost <IP>` で流し直す）・眠っている（`spark.ps1 wake`）・sshd・lao serve のどれかを出します |
| `mcp --check` が lao serve につながらないと言う | Spark で lao serve が動いているか（再起動の後はサインインが要る。2.2 節）。`Get-ScheduledTask lao-serve` |
| 蓋を閉じるとしばらくして切れる | `powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION` の AC が 0 か、スリープが「なし」か、アダプターの省電力。有線 LAN を試す |
| 蓋を閉じると生成が遅くなる | 熱で絞られています（2.2 節）。置き方を変えるか、蓋を開けて使う |
| lao の画面が開かない | タスク `lao-spark-tunnel` が動いているか（`Get-ScheduledTask lao-spark-tunnel`）。`spark.ps1 check` |
| モデルの取得で 401・403 | Hugging Face で利用条件に同意したか、Spark の環境変数 `HF_TOKEN` があるか（入れた後に開いたターミナルで `claude remote-control` を起動し直す） |
| `lao` が見つからない | フルパス `C:\lao\venv\Scripts\lao.exe` |

## 結果の残し方

- 測定結果：`p0-results\`（lao が書きます）。導入の記録：`D:\lao\logs\setup-*.txt`
- 機種・ドライバの版・各エンジンの動作可否・主経路の判断：[plan.md](plan.md) 15 章と [prep.md](prep.md) 2 章の表を更新します
- 動作を確かめたエンジンの版：`install-native.ps1` の `-LlamaCppRef`・`-ComfyUIRef` の既定値をその版に固定します
- スクリプトを使わずに手で行うときの手順は [README](../README.md) の「実機が届いたらやること」と [remote.md](remote.md) にあります
