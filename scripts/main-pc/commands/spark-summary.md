---
description: メイン PC のファイルやフォルダを、Claude が読まずに Spark（lao）で要約する
argument-hint: ファイルかフォルダのパスと、まとめ方の指定
---
次のファイル・フォルダを Spark の lao で要約してください。依頼：$ARGUMENTS

1. ファイルの中身は自分で読まない（トークンを節約するため）
2. `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 put <パス>` で送る（フォルダもそのまま送れる）
3. 表示された名前を lao の ask_local の inbox に渡し、task には出力の形と長さを書く（指定がなければ「要点・決まったこと・残った課題に分けて、全体で 20 行以内」）
4. 結果をそのまま伝える。足りないところがあるときだけ、取り込まれた ref を refs に入れて ask_local でもう一度聞く
