# 構成カタログ

各環境（E1〜E4）の構成をデータとして持つ。コードを変えずにモデルを差し替えられる。

- `status = "candidate"`：未検証の候補。値は公開情報からの見積もり。
- `status = "verified"`：RTX Spark 実機で必須能力・メモリ・速度を確認済み。
- `quantization`：`none`（配布元の元精度）／`publisher`（配布元が公式に出した量子化形式）／`third_party`（第三者の量子化。既定では使えない）。
- `mem.weights_gb` と `mem.overhead_gb` は見積もり。実測値が `config/machine.toml` の `[measured]` にあればそちらを使う。
- `source.revision` と `source.sha256` は導入時に固定する。空なら Hugging Face の公開ハッシュで検証し、取得後に記録する。
- `engine = "comfyui"` の構成は `workflow` に API 形式のワークフロー JSON を置き、`bindings` で入力の差し込み先を指定する（P0 で ComfyUI から書き出す）。

TODO（P0）：リポジトリ名・ファイル名・容量・ライセンスを実際の配布元で確認し、`status` を更新する。
