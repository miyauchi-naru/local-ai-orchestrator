---
description: Spark（lao）で作ったものをメイン PC に取ってくる
argument-hint: 取ってくる成果物（ref か「さっきの画像」など）と保存先
---
Spark の成果物をメイン PC に取ってきてください。依頼：$ARGUMENTS

1. どの成果物かが ref で分からなければ lao の list_artifacts で探す。保存場所（Spark のパス）は生成の結果か list_artifacts に出ている
2. `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 get "<保存場所>" [保存先]` で取ってくる（保存先の指定がなければ ~/Downloads/lao）
3. 保存したパスを伝える
