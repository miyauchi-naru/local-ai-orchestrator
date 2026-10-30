# パターン（会話モデルが ComfyUI を組み合わせて使う）

会話モデルは ComfyUI のグラフを書きません。生成モデルごとに用意して承認した「パターン」（ワークフロー）を、
`run_recipe` で最大 6 手まで並べて使います。前の手の出力が次の手の画像入力になり、途中の画像も成果物に残ります。
1 回で済む依頼は、これまでどおり `generate_image`・`edit_image` です（`steps`・`cfg`・`lora` も指定できます）。

## 用意のしかた（実機で、P0 以降）

1. ComfyUI でワークフローを作り、「Export (API)」で書き出す。
2. 下書きを作る：
   ```
   lao pattern new 書き出した.json --component e2.e2-standard.imagegen --id upscale-2x
   ```
   `catalog/patterns/<生成モデル>/upscale-2x.json` と `.toml` ができます。プロンプト・画像・seed・steps・cfg・denoise の
   差し込み先と、使っているモデルのファイル名は、見つかった分だけ埋まっています。
3. TOML の説明を書く：`does`（何をするか）、`needs`（要るもの）、`good_for`（向いている依頼の例を日本語で 2〜3 個）、
   `avoid`（使ってはいけない時）。会話モデルに見せる候補は、依頼の文とこの説明の重なりで機械的に絞るので、
   `good_for` は利用者が実際に言いそうな言い方で書きます。
4. `work_gb`（このパターンの作業メモリ）を書く。一度動かしてピークを測り、少し多めに書きます。
5. 承認する：
   ```
   lao pattern approve catalog/patterns/e2-qwen-image/upscale-2x.toml
   ```
   許可ノードの一覧（`catalog/shared/comfy-nodes.toml`）、URL、パスを含む値、モデル名（`models` の一覧にあるか）、
   画像の読み込みが lao の差し込みになっているか、保存ノードがあるかを確かめ、通ればワークフローのハッシュを
   `approved` に書きます。ワークフローを後で変えたら、承認し直すまで使えません。
6. `lao pattern list` で「使える」になっていることを確かめる。

同じ働きのパターンは、生成モデルが違っても同じ id にします（標準の名前は `catalog/patterns/README.md`）。
`lao p0 pairs` は、この id で「正しいパターンを選べたか」を採点します。

## 会話モデルへの見せ方

- 画像が要るパターンは、添付か会話の中に画像が無ければ候補から外します。
- 残りを、依頼の文とパターンの説明の文字の重なりで並べ、上から 3 個だけを `run_recipe` の候補として見せます。
- 2 手目以降は、候補に出ていない承認済みのパターンも使えます（例：三面図を描いてから拡大）。

## メイン PC の Claude から

`lao mcp` の `patterns`（一覧）と `run_recipe`（実行）で同じパターンを使えます。
