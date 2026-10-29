# local-ai-orchestrator（lao）

RTX Spark（128GB 統合メモリ、Windows 11 on Arm）1台で、四つの目的別ローカルAI環境を構築し、一つの対話画面から使うためのツールです。

| 環境 | できること |
|---|---|
| E1 推論 | 画像・音声・動画（音声つき）を読み、相談・推論する |
| E2 画像生成 | 参考画像を見ながら画像を作り、修正する |
| E3 音声生成 | 参考音声の話し方で日本語の文章を読み上げる |
| E4 動画生成 | 画像を元に短い動画を作る（音声つき構成あり） |

企画と前提は [docs/plan.md](docs/plan.md)、実機入手前の準備は [docs/prep.md](docs/prep.md) にあります。

## 方針

- 対話モデルは llama.cpp（llama-server）を直接使います。**Ollama は一切使いません。**
- ロード・アンロード・推論ロック・メモリ予算は本ツールが管理します。推論は補助モデルを含めて一度に一つです。
- 予算を超えそうな依頼は**実行前に**止めます。無断で量子化したり、品質を下げたりはしません。
- 環境はユーザーが選びます。勝手に別の環境やクラウドへ切り替えません。
- 素材や会話を外部へ送りません。API はループバック（127.0.0.1）だけで待ち受けます。

## 実機が届いたらやること

コードは揃っています。実機では「検証して、設定値を埋める」だけです。**ユーザーの「開始」指示があるまで、以下は実行しません。**

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

## Open WebUI などから使う

`lao serve` は OpenAI 互換の `/v1/chat/completions` を出します。既存の UI から「OpenAI 互換の接続先」として `http://127.0.0.1:8710/v1` と API キーを登録できます（Ollama 連携は無効にしてください）。環境の切り替えは本ツールの画面か `/api/environment` で行います。

## 構成

```
catalog/        構成カタログ（E1〜E4。モデル・配布元・ライセンス・メモリ見積もり）
config/         実機の設定（machine.example.toml をコピーして使う）
scripts/        導入スクリプト（既定は確認のみ）
src/lao/
  config.py       設定とカタログの読み込み、必須能力・量子化ルールの検査
  memory.py       統合メモリの予算と受付判定
  lock.py         共通の推論ロック
  runtime.py      部品のロード・アンロード（予算内で、構成の中だけ）
  orchestrator.py 会話→ツール呼出し→ジョブ→説明の流れ
  tools.py        ツールの定義と引数検証
  jobs.py         ジョブ（状態の保存、取消、再起動時の中断扱い）
  library.py      四環境共通の成果物フォルダ（版と由来）
  media.py        ffmpeg による検査・分割
  installer.py    モデル取得（再開・ハッシュ照合）
  server.py       API と画面
  backends/       llama-server、ComfyUI、TTS、ダミー
  p0/probe.py     実機の測定と設定値の提案
```
