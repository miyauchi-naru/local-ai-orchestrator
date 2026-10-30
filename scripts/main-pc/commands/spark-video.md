---
description: Spark（lao）で画像から動画を作る。読み上げを付けることもできる
argument-hint: 元の画像（ファイルのパスか ref）と、動きの説明・秒数・読み上げの文
---
Spark の lao で動画を作ってください。依頼：$ARGUMENTS

1. 元の画像がメイン PC のファイルなら `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 put <パス>` で送り、lao の import_file で ref にする。画像がなければ先に generate_image で作る
2. 動きの説明を英語のプロンプトにする。秒数の指定がなければ 5 秒
3. 読み上げの文があれば make_clip、なければ generate_video を呼ぶ。job が返ったら job で待つ
4. describe_media で依頼どおりかを確かめ、ref と保存場所を伝える
