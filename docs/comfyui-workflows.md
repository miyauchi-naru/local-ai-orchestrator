# ComfyUI ワークフローの登録（P0）

画像生成（E2）と動画生成（E4）は ComfyUI で動かします。本ツールは ComfyUI のワークフローを「API 形式の JSON」として持ち、プロンプトや画像などの値をカタログの `bindings` で指定した場所に差し込んで実行します。

## 手順

1. ComfyUI を起動し、実機で狙いどおりに動くワークフローを作る（モデルは `catalog/*.toml` の構成に合わせる）。
2. メニューの「Export (API)」で JSON を書き出し、カタログの `workflow` に書いた名前で `catalog/workflows/` に置く。
   - E2：`e2-generate.json`（文章→画像）、`e2-edit.json`（画像＋指示→画像）
   - E4：`e4-ltx-i2v.json`、`e4-wan-i2v.json`（画像＋文章→動画）
3. JSON を開き、値を差し込むノードの番号と入力名を `bindings` に書く。形式は `"ノード番号.inputs.入力名"`。

```toml
bindings = { prompt = "6.inputs.text", negative = "7.inputs.text", seed = "3.inputs.seed",
             width = "5.inputs.width", height = "5.inputs.height", image = "52.inputs.image" }
```

| キー | 中身 |
|---|---|
| `prompt` / `negative` | 文章 |
| `seed` | 乱数シード |
| `width` / `height` | 画像サイズ |
| `image` | 入力画像（本ツールが ComfyUI にアップロードしたファイル名） |
| `seconds` / `fps` / `frames` | 動画の長さ。`frames` は LTX 系の「8n+1」に合わせて計算済み |

4. 出力は「保存」系のノード（SaveImage、SaveVideo 等）で `output` に出すこと。本ツールは履歴から出力ファイルを取得します。
5. `lao serve` で実際に生成し、成果物の長さ・音声の有無が正しく記録されるか確かめる。

## メモリについて

ComfyUI はモデルを内部にキャッシュします。本ツールは部品を下ろすときに `/free` を呼び、ComfyUI 側に隠れたメモリ使用を残さないようにしています。P0 では生成中のピークを `lao p0` と同じ方法で測り、`[measured]` に入れてください。
