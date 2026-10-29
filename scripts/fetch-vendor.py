"""成果物の表示に使う部品（React・グラフ・図など）を npm から取り、src/lao/web/vendor に置く。

部品はリポジトリに入れてあるので、ふだんは実行しなくてよい（実機ではネットを使わずに表示できる）。
版を上げるときだけ、この表を直して開発用の PC で実行し、できたファイルを確かめてから登録する。
manifest.json に SHA256 を書き、サーバーはこの表にある名前だけを返す。

  python scripts/fetch-vendor.py
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "lao" / "web" / "vendor"

# (出力名, npm パッケージ, 版, 包みの中のファイル, 画面での使い道)
PARTS = [
    ("react.js", "react", "18.3.1", "umd/react.production.min.js", "React（window.React）"),
    ("react-dom.js", "react-dom", "18.3.1", "umd/react-dom.production.min.js", "ReactDOM（window.ReactDOM）"),
    ("prop-types.js", "prop-types", "15.8.1", "prop-types.min.js", "Recharts が使う"),
    ("babel.js", "@babel/standalone", "7.26.4", "babel.min.js", "JSX を画面の中で変換する"),
    ("recharts.js", "recharts", "2.15.0", "umd/Recharts.js", "グラフ（window.Recharts）"),
    ("lucide-react.js", "lucide-react", "0.460.0", "dist/umd/lucide-react.min.js", "アイコン（window.LucideReact）"),
    ("chart.js", "chart.js", "4.4.7", "dist/chart.umd.js", "グラフ（window.Chart）"),
    ("d3.js", "d3", "7.9.0", "dist/d3.min.js", "図・グラフ（window.d3）"),
    ("mermaid.js", "mermaid", "11.4.1", "dist/mermaid.min.js", "流れ図など（window.mermaid）"),
    ("tailwind.js", "@tailwindcss/browser", "4.1.11", "dist/index.global.js", "Tailwind の class を画面の中で CSS にする"),
    ("three.js", "three", "0.159.0", "build/three.min.js", "3D（window.THREE）"),
    ("papaparse.js", "papaparse", "5.4.1", "papaparse.min.js", "CSV（window.Papa）"),
    ("lodash.js", "lodash", "4.17.21", "lodash.min.js", "便利関数（window._）"),
]


def tarball(pkg: str, ver: str) -> bytes:
    base = pkg.split("/")[-1]
    with urllib.request.urlopen(f"https://registry.npmjs.org/{pkg}/-/{base}-{ver}.tgz", timeout=120) as r:
        return r.read()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "licenses").mkdir(exist_ok=True)
    manifest = {}
    for name, pkg, ver, inner, use in PARTS:
        with tarfile.open(fileobj=io.BytesIO(tarball(pkg, ver)), mode="r:gz") as tf:
            data = tf.extractfile(f"package/{inner}").read()
            meta = json.loads(tf.extractfile("package/package.json").read())
            lic = next((m for m in tf.getmembers() if m.name.lower() in ("package/license", "package/license.md",
                                                                          "package/license.txt")), None)
            if lic:
                (OUT / "licenses" / f"{name}.txt").write_bytes(tf.extractfile(lic).read())
        (OUT / name).write_bytes(data)
        manifest[name] = {"package": pkg, "version": ver, "file": inner, "license": meta.get("license", ""),
                          "use": use, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        print(f"{name:18} {pkg}@{ver}  {len(data) // 1024} KB")
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
