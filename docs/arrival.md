# 実機が届いたら（RTX Spark 到着後の手順書）

RTX Spark 128GB（Windows 11 on Arm、ノート型）が届いてから、メイン PC の Claude Code で使えるようになるまでを、上から順に並べた手順書です。
細かい説明は各文書にあり、ここではつなぎ方と順番、確かめる点だけを書きます。

- **利用者の「開始」指示があるまで、どの手順も実行しません。**
- 置き場所：C（1TB）にモデル・エンジン・Python 環境、D（2TB）に成果物・会話・ジョブ・ログ（[prep.md](prep.md) 1.2 節）
- 「Spark」と書いたものは届いた PC、「メイン PC」はいつも使っている PC です。
- 時間の目安：1〜6 章で 1〜2 日（ビルドとモデル取得の待ちが大半）、P0 の詳しい評価は 2 週ほど（[prep.md](prep.md) 5 章）

## 全体の流れ

| 章 | すること | 終わった印 |
|---|---|---|
| 0 | 届く前の用意（メイン PC 側） | 鍵と Hugging Face の準備ができている |
| 1 | 開封と Windows の初期設定 | 128GB・C/D・ドライバの版を記録した |
| 2 | 画面を閉じても動き続ける設定 | 蓋を閉じたままメイン PC から SSH が通る（6 章で確認） |
| 3 | lao の導入 | `lao doctor` が通る |
| 4 | P0 の測定と設定値 | `config/machine.toml` に測定値が入った |
| 5 | Spark 上で起動の確認 | Spark の Edge で対話画面が開く |
| 6 | メイン PC から使う | Claude Code から lao の道具が呼べる。ブラウザで画面が開く |
| 7 | 必要なときだけ（意味検索・コード実行） | — |

## 0 届く前の用意（メイン PC 側）

- [ ] メイン PC に SSH の鍵があるか確かめる。無ければ `ssh-keygen -t ed25519`。公開鍵 `~/.ssh/id_ed25519.pub` を USB メモリに入れておく（秘密鍵は入れない）
- [ ] Hugging Face のアカウントと、利用条件への同意が要るモデル（Gemma 4 など）の同意。読み取り用のアクセストークンを作っておく
- [ ] 評価用の素材（自分の声の録音、非発話音を含む短い動画など。[prep.md](prep.md) 4 章）
- [ ] できれば有線 LAN（USB-C の LAN アダプタ）。画面を閉じて長く使うなら Wi-Fi より安定します

## 1 開封と Windows の初期設定

1. **AC 電源につないで**初期設定を進める。
2. **利用者名は半角英数字、空白なし**にする（例：`naru`）。SSH の `User`、パス、ビルドで日本語や空白が問題を起こすため。Microsoft アカウントでも、作られるフォルダ名（`C:\Users\<名前>`）を確かめておく。
3. Windows Update をすべて済ませ、再起動する。
4. 記録する（P0 の結果と一緒に残す）：
   - 機種、メモリ（設定 → システム → バージョン情報で 128GB）、Windows の版
   - `nvidia-smi` の GPU 名とドライバの版
   - タスクマネージャー → パフォーマンス → GPU の「共有 GPU メモリ」の上限
5. ドライブを確かめる：PowerShell で `Get-PSDrive C,D`。C が約 1TB、D が約 2TB あること。D が見えなければ「ディスクの管理」で NTFS に初期化して D を割り当てる。
6. BitLocker（デバイスの暗号化）が有効なら、回復キーを Microsoft アカウントか紙に保存しておく。
7. ネットワークを「プライベート」にする（設定 → ネットワークとインターネット → 使っている接続 → ネットワーク プロファイルの種類）。パブリックのままだと 6 章の SSH が通りません。
8. ルーターで Spark の IP を固定（DHCP の予約）しておくと、メイン PC の設定が変わらずに済みます。PC 名（`$env:COMPUTERNAME`）で届く家庭なら不要です。

## 2 画面を閉じても動き続ける設定

Spark は蓋を閉じて置いたまま、メイン PC から使う前提です。**AC 電源のときだけ**眠らないようにし、バッテリーのときは普通に眠るようにします。

### 2.1 蓋を閉じても何もしない・眠らない

管理者の PowerShell で：

```powershell
# AC 電源のとき：蓋を閉じても何もしない（0 = 何もしない）
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
# AC 電源のとき：スリープ・休止状態にしない（画面だけは消してよい）
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 5
powercfg /setactive SCHEME_CURRENT
# 確かめる：LIDACTION の「現在の AC 電源設定のインデックス」が 0x00000000 なら OK
powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION
```

画面からも同じことができます：コントロール パネル → 電源オプション → 「カバーを閉じたときの動作の選択」→「電源に接続」を「何もしない」。設定 → システム → 電源とバッテリーで、「電源に接続時」のスリープを「なし」、電源モードを「最適なパフォーマンス」にします。

### 2.2 ネットワークを切らさない

- 有線 LAN がいちばん確実です。Wi-Fi の場合は、デバイス マネージャー → ネットワーク アダプター → Wi-Fi のプロパティ →「電源の管理」で「電力の節約のために、コンピューターでこのデバイスの電源をオフにできるようにする」を外します（項目が無い機種もあります）。
- 2.1 でスリープしないようにしていれば、蓋を閉じても Wi-Fi はつながったままです。

### 2.3 勝手に再起動されて止まらないように

- 設定 → Windows Update →「アクティブ時間」を使う時間帯に合わせる。長い生成をする前は「更新の一時停止」を使う。
- 再起動の後は、**一度 Spark にサインインするまで lao serve が起動しません**（6 章の `-AutostartServe` はサインイン時に起動するため）。再起動されたら蓋を開けてサインインしてください。

### 2.4 熱について

- 蓋を閉じると放熱が落ちる機種があります。机など硬く平らな所に置き、吸気口・排気口をふさがない（布団や鞄の中に置かない）。縦置きのスタンドも効果があります。
- 4 章の 30 分以上の連続生成は、**蓋を閉じた状態でも一度**行い、速度が大きく落ちないか（熱で絞られていないか）を見ます。落ちるなら蓋を開けて使うか、置き方を変えます。

確かめるのは 6 章の最後（蓋を閉じたままメイン PC から使えること）です。

## 3 lao の導入

1. Git を入れて、リポジトリを取得する（非公開のリポジトリなので、初回はブラウザで GitHub にサインインします）：
   ```powershell
   winget install --id Git.Git --accept-package-agreements --accept-source-agreements
   # 新しい PowerShell を開いてから
   New-Item -ItemType Directory -Force C:\lao | Out-Null
   git clone https://github.com/miyauchi-naru/local-ai-orchestrator C:\lao\local-ai-orchestrator
   cd C:\lao\local-ai-orchestrator
   ```
2. **CUDA Toolkit を先に入れる。** 導入スクリプトは同じ実行の中で llama.cpp を CUDA でビルドするため、その前に必要です。
   1. Visual Studio Build Tools（C++、Arm64）を先に入れる（CUDA の導入時に VS との連携が入るように）：
      ```powershell
      winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.ARM64 --includeRecommended"
      ```
   2. NVIDIA の開発者サイトから CUDA Toolkit 13.4（Windows Arm64）を入れる。ドライバは 1 章の Windows Update の版のままにし、勝手に更新しない。
3. 導入スクリプトを**確認モード**で動かし、表示を読む：
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install-native.ps1
   ```
   CPU が `ARM64`、メモリが 128GB 前後、C と D の空き容量が出ていることを確かめます。
4. 問題なければ実行する：`... install-native.ps1 -Execute`
   - Python 3.12（Arm64）、ffmpeg、Git・CMake・Build Tools、lao（`C:\lao\venv`）、llama.cpp（`C:\lao\engines\llama.cpp`）、ComfyUI と PyTorch（`C:\lao\engines\comfyui`）が入り、`config\machine.toml` が作られます。
   - 途中の PyTorch の行で `True` と出れば、GPU が使えています。
5. `lao` を短く呼べるようにする（PowerShell を開くたび。常に使うならユーザーの PATH に `C:\lao\venv\Scripts` を足す）：
   ```powershell
   Set-Alias lao C:\lao\venv\Scripts\lao.exe
   ```
6. ffmpeg の場所を設定に合わせる。winget は `C:\lao\engines\ffmpeg` ではなく別の場所に入れるので、新しい PowerShell で `(Get-Command ffmpeg).Source` と `(Get-Command ffprobe).Source` を調べ、`config\machine.toml` の `[engines.ffmpeg]` に書く（区切りは `/` でも `\\` でも可）。
7. 点検：`lao doctor`。NG の行を直してから次へ進みます。

**llama.cpp のビルドが失敗したとき：** Windows Arm64 の CUDA ビルドは未確認の部分です（[prep.md](prep.md) 2 章）。失敗の表示を記録し、WSL2 案を試します（[plan.md](plan.md) 2.2 節）。`wsl --install` で Ubuntu（Arm64）を入れ、WSL の中で `nvidia-smi` が動くことを確かめてから `bash scripts/install-wsl2.sh`（確認のみ）→ `--execute`。WSL2 を主に使うときは `machine.toml` の `[machine] runtime = "wsl2"` にします。

## 4 P0 の測定と設定値

手順の意味と記録の残し方は [prep.md](prep.md) 5 章、決めることの一覧は [plan.md](plan.md) 15 章にあります。ここでは lao の操作の順番だけ書きます。

1. **GPU が確保できる量だけ先に測る**（モデルはまだ無いので省きます）：
   ```powershell
   lao p0 run --gpu-python C:\lao\engines\comfyui\.venv\Scripts\python.exe --skip-models
   ```
2. **カタログの TODO を埋める**（`git grep -n "TODO(P0)" catalog config scripts`）。モデルの配布元・ファイル・ライセンスを確かめ、使うものを `status = "verified"` にします（[catalog/README.md](../catalog/README.md)）。
3. **モデルを取る**（C に入ります）。同意が要るモデルのために、その PowerShell の中だけトークンを入れます（ファイルには書かない）：
   ```powershell
   $env:HF_TOKEN = "<0 章で作ったトークン>"
   lao plan e1                 # 何を取るかと大きさの確認だけ
   lao install e1 --execute
   ```
   E2〜E4 も同じです。全構成で約 400GB あるので、使う環境から順に取ります。
4. **モデルを測る**：
   ```powershell
   lao p0 run --gpu-python C:\lao\engines\comfyui\.venv\Scripts\python.exe --envs e1
   ```
   結果は `p0-results\p0-<日時>.json` に残ります。
5. **設定値の提案を受けて反映する**：`lao p0 propose` → `config\machine.proposed.toml` ができるので、`config\machine.toml` と見比べて採用します。
6. `config\machine.toml` で人が決める値（一覧は [README](../README.md) の「設定するパラメータ」）：
   - `[machine] runtime`：`native` か `wsl2`
   - `[memory] budget_gb`：AI 用の予算（初期 96GB）
   - `[server] api_key`：**固定の値を入れる**（空だと起動のたびに変わり、6 章のブラウザ利用で毎回キーを調べ直すことになります）。英数字 32 文字ほどの長い値にし、他の人に見せない
   - `[policy] allow_third_party_quantized`：初期は `false` のまま
7. E2・E4（画像・動画）：ComfyUI でワークフローを作り、API 形式で書き出して `catalog/workflows/` に置き、`bindings` を埋めます（[comfyui-workflows.md](comfyui-workflows.md)）。ComfyUI の画面は 6 章のトンネルを使うとメイン PC から操作できます。
8. E3（読み上げ）：TTS の候補を選び、[tts-contract.md](tts-contract.md) の形の中継を用意して `[engines.tts]` に書きます。
9. 日本語の評価課題（[prep.md](prep.md) 4 章）を各候補で行い、30 分以上の連続生成を**蓋を閉じた状態でも**行います（2.4 節）。

## 5 Spark 上で起動の確認

```powershell
cd C:\lao\local-ai-orchestrator
lao serve --env e1
```

表示された `http://127.0.0.1:8710/?key=…` を Spark の Edge で開き、短い質問に答えが返れば成功です。止めるときは Ctrl+C。

## 6 メイン PC から使う

手順の本体は [remote.md](remote.md) です。順番と確かめる点だけ書きます。

1. **Spark：** 0 章の公開鍵（`id_ed25519.pub`）を USB メモリから `C:\lao\local-ai-orchestrator` に置く。
2. **Spark：** 管理者の PowerShell で `powershell -ExecutionPolicy Bypass -File scripts\enable-remote.ps1 -PublicKeyFile .\id_ed25519.pub -AutostartServe` を確認モードで動かし、内容を見てから `-Execute` を付けて実行する（[remote.md](remote.md)「準備（Spark 側）」）。最後に出る PC 名を控える。
3. **Spark：** 一度サインインし直すか、`Start-ScheduledTask lao-serve` で lao serve を起動する。
4. **メイン PC：** `~/.ssh/config` に `Host spark` を書く。ブラウザで画面を使うので `LocalForward 8710 127.0.0.1:8710` の 1 行も足す（[remote.md](remote.md)「準備（メイン PC 側）」「lao の画面をメイン PC で使う」）。
5. **メイン PC：** `ssh spark C:/lao/venv/Scripts/lao.exe mcp --check` → `OK：lao serve に接続できました`
6. **メイン PC：** `claude mcp add lao --scope user -- ssh spark C:/lao/venv/Scripts/lao.exe mcp` と、`~/.claude/settings.json` の `MCP_TOOL_TIMEOUT`。必要なら `~/.claude/CLAUDE.md` に使い方を書く。
7. **メイン PC：** Claude Code で「lao の status を見て」と頼み、答えが返れば完了。続けて「猫の画像を 1 枚作って」で E2 を試す。
8. **メイン PC：** `ssh -N spark` を動かしたまま、Edge で `http://127.0.0.1:8710/?key=<api_key>` を開く。「アプリとしてインストール」しておくと次から楽です。
9. **蓋を閉じて確かめる：** Spark の蓋を閉じ、5 分ほど待ってから 5 と 7 をもう一度行う。通れば 2 章の設定が効いています。

## 7 必要なときだけ

- **意味検索：** `lao install embedding --execute` → `machine.toml` の `[embedding] enabled = true`
- **コード実行：** `scripts\install-sandbox.ps1`（確認のみ）→ `-Execute` → `lao sandbox-check` がすべて合格したら `[sandbox] enabled = true`（WSL が要ります）

## 困ったとき

| 起きたこと | 見るところ |
|---|---|
| メイン PC から `ssh spark` が通らない | Spark のネットワークが「プライベート」か（1 章 7）、Spark が眠っていないか（2 章）、IP が変わっていないか（1 章 8）、`Get-Service sshd` が Running か |
| `mcp --check` が lao serve につながらないと言う | Spark で lao serve が動いているか（再起動の後はサインインが要る。2.3 節）。`Get-ScheduledTask lao-serve` |
| 蓋を閉じるとしばらくして切れる | `powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION` の AC が 0 か、スリープが「なし」か、Wi-Fi の省電力（2.2 節）。有線 LAN を試す |
| 蓋を閉じると生成が遅くなる | 熱で絞られています（2.4 節）。置き方を変えるか、蓋を開けて使う |
| `lao` が見つからない | 3 章 5 の `Set-Alias`、またはフルパス `C:\lao\venv\Scripts\lao.exe` |
| ffmpeg が見つからないと `lao doctor` が言う | 3 章 6 の `[engines.ffmpeg]` |
| llama.cpp のビルドが失敗する | 3 章の最後（WSL2 案） |
| モデルの取得で 401・403 | Hugging Face で利用条件に同意したか、`$env:HF_TOKEN` を入れたか |

## 結果の残し方

- 測定結果：`p0-results\`（lao が書きます）
- 1 章 4 の記録、各エンジンの動作可否、主経路の判断：[plan.md](plan.md) 15 章と [prep.md](prep.md) 2 章の表を更新します
- 動作を確かめたエンジンの版：`install-native.ps1` の `-LlamaCppRef`・`-ComfyUIRef` の既定値をその版に固定します
