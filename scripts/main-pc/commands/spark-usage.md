---
description: Claude のトークンの記録を見る（lao に任せた分の節約と、Claude Code が実際に使った量）
argument-hint: 月（YYYY-MM、省略すると今月）
---
Claude のトークンの記録を見せてください。月：$ARGUMENTS

1. `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 usage 31` で、この PC の Claude Code の使用量を Spark へ送り直す（会話の中身は送らず、日ごとの数だけ）
2. lao の usage を呼ぶ（月の指定があれば month に入れる）
3. 節約できたトークン、Claude にできないことを任せた回数、Claude Code が使った量、節約の割合を短く伝える。数字は見積もりであることを添える。グラフが要るときは lao の画面の「ふりかえり」にあると伝える
