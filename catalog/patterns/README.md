生成モデルごとのパターン（会話モデルが名前で選んで組み合わせる ComfyUI のワークフロー）。
フォルダはカタログの生成部品の `patterns` で決まる。作り方は ../../docs/patterns.md。

同じ働きのパターンは、モデルが違っても同じ id にそろえる（lao p0 pairs の課題がこの id で正解を判定する）：

| id | 働き |
|---|---|
| `upscale-2x` | 2 倍に拡大して細部を足す |
| `fix-face-hands` | 顔と手を見つけて描き直す |
| `pose-from-reference` | 参考画像のポーズ・構図で新しく描く |
| `img2img` | 画像を元に、変化の強さを指定して描き直す |
| `inpaint-box` | 指定した範囲だけ描き直す |
| `outpaint` | 画像の外側を描き足す |
| `character-sheet` | 同じキャラクターの三面図 |
