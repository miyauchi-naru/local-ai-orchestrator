---
description: Spark（lao）で画像を作る。案を何枚か作って並べて見比べる
argument-hint: 作りたい画像の説明（スタイル名があれば「スタイル：名前」）
---
Spark の lao で画像を作ってください。依頼：$ARGUMENTS

1. 依頼を画像用の英語プロンプトにする（短い依頼なら自分で、長い説明や資料があれば lao の ask_local に任せる）
2. 縦横比やスタイルの指定があれば aspect・style に入れる（保存済みのスタイルは lao の styles で確かめる）
3. 枚数の指定がなければ generate_image を count=4 で呼ぶ
4. view の refs で並べた 1 枚を見て、依頼に一番合うものを選び、ref と保存場所を伝える。合わなければプロンプトを直してもう一度
