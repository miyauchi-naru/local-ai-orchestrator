---
description: Spark（lao）の状態・健康・つながりを確かめる
---
Spark の状態を確かめてください。

1. lao の status と health を呼ぶ
2. lao の道具がつながらなければ `powershell -NoProfile -ExecutionPolicy Bypass -File ~/.lao/spark.ps1 check` を流し、眠っていそうなら `spark.ps1 wake`
3. 環境・実行中のジョブ・C と D の空き・GPU の温度・注意を短く伝える。容量の注意があれば cleanup（一覧だけ）の結果も添える
