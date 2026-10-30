"""コマンド：lao doctor | plan | install | serve | p0 run | p0 propose | p0 pairs | pattern | configure | sandbox-check | mcp"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _paths(args) -> tuple[Path, Path]:
    config = Path(args.config or os.environ.get("LAO_CONFIG") or REPO / "config" / "machine.toml")
    catalog = Path(args.catalog or REPO / "catalog")
    return config, catalog


def cmd_doctor(args) -> int:
    from .config import ConfigError, check_policy, load_catalog, load_machine, missing_capabilities
    from .p0.probe import checks, engine_versions, system_info

    config, catalog_dir = _paths(args)
    ok = True
    try:
        machine = load_machine(config)
        print(f"[OK] 設定 {config}")
    except ConfigError as e:
        print(f"[NG] {e}")
        return 1
    try:
        catalog = load_catalog(catalog_dir)
        print(f"[OK] カタログ {', '.join(catalog)}")
    except ConfigError as e:
        print(f"[NG] カタログ：{e}")
        return 1
    for env in catalog.values():
        for c in env.configurations.values():
            problems = missing_capabilities(env, c) + check_policy(machine, c)
            mark = "OK" if not problems else "NG"
            ok &= not problems
            print(f"[{mark}] {env.id}/{c.id}（{c.status}）{'：' + '、'.join(problems) if problems else ''}")
            resident = sum(x.resident_gb for x in c.components.values()) * machine.safety_factor
            if resident > machine.budget_gb:
                print(f"      注意：全部品の常駐 {resident:.0f}GB が予算 {machine.budget_gb:.0f}GB を超えるため、"
                      "対話と生成の間でロードし直しが起きます")
    info = system_info(machine)
    for good, msg in checks(info, machine):
        ok &= good
        print(f"[{'OK' if good else 'NG'}] {msg}")
    eng = engine_versions(machine)
    print(f"[{'OK' if eng['llama_server'] else 'NG'}] llama-server：{eng['llama_server'] or '見つからない'}")
    print(f"[{'OK' if eng['ffmpeg'] else 'NG'}] ffmpeg：{eng['ffmpeg'] or '見つからない'}")
    print(f"[{'NG' if eng['ollama_found'] else 'OK'}] Ollama を使っていない")
    return 0 if ok else 1


def cmd_plan(args, execute: bool = False) -> int:
    import httpx

    from .config import load_catalog, load_machine
    from .installer import build_plan, disk_free, download, write_lock

    config, catalog_dir = _paths(args)
    machine = load_machine(config)
    if args.env == "embedding":  # 意味検索のモデル（環境とは別）
        from .config import load_embedding
        confs = [load_embedding(catalog_dir)]
    else:
        env = load_catalog(catalog_dir)[args.env]
        confs = [env.configurations[args.configuration]] if args.configuration else list(env.configurations.values())
    client = httpx.Client(timeout=60, follow_redirects=True,
                          headers={"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {})
    rc = 0
    for conf in confs:
        plan = build_plan(machine, conf, client)
        print(plan.summary(disk_free(machine.path_of("models"))))
        if not execute:
            continue
        if plan.blocked:
            print("ルールに反する項目があるため取得しません")
            rc = 1
            continue
        if plan.todo:
            print("未設定の項目があります。分かっている分だけ取得します")
        for i, t in enumerate(plan.tasks, 1):
            print(f"  ({i}/{len(plan.tasks)}) {t.path} → {t.dest}")
            last = [0]

            def prog(have, total, last=last):
                pct = int(have * 100 / total) if total else 0
                if pct >= last[0] + 5:
                    last[0] = pct
                    print(f"      {pct}%", flush=True)

            download(t, client, prog)
        write_lock(plan, machine.path_of("models") / conf.id / "lock.json")
        print("  取得と照合が終わりました")
    if not execute:
        print("\n（確認のみ。取得するには lao install に --execute を付けて実行します）")
    return rc


def cmd_serve(args) -> int:
    import uvicorn

    from .config import load_machine
    from .server import create_app
    from .service import Service

    config, catalog_dir = _paths(args)
    if args.fake:
        from .backends.fake import fake_engine_factory
        from .config import MachineConfig
        from .demo import demo_machine, make_demo_catalog

        root = Path(tempfile.gettempdir()) / "lao-demo"
        machine = MachineConfig(demo_machine(root))
        catalog_dir = make_demo_catalog(catalog_dir, root / "catalog")
        # ダミーでは実機の空きメモリを見ない（128GB 機のふりをする）
        service = Service(machine, catalog_dir, fake_engine_factory(0.3), memory_probe=lambda: (128.0, 120.0))
        if shutil.which("bwrap"):  # Linux で試すときはコード実行も動かす
            service.sandbox.conf.enabled = True
        print("ダミーエンジンで起動します（本物のモデルは使いません）")
    else:
        machine = load_machine(config)
        service = Service(machine, catalog_dir)
    key = machine["server"].get("api_key") or ""
    if not key:
        # 起動のたびに作るキーは、同じ PC の lao mcp が読めるよう root/server.key に残す
        key = secrets.token_urlsafe(18)
        key_file = machine.path_of("root") / "server.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
        key_file.chmod(0o600)
    if args.env:
        asyncio.run(service.select(args.env, args.configuration))
    app = create_app(service, key)
    host, port = machine["server"]["host"], int(args.port or machine["server"]["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("server.host はループバック（127.0.0.1）にしてください")
        return 1
    print(f"画面：http://{host}:{port}/?key={key}")
    print(f"OpenAI 互換：http://{host}:{port}/v1  API キー：{key}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_configure(args) -> int:
    import tomllib

    from .setup import apply, auto_values, parse_value, proposed_values

    config, _ = _paths(args)
    if not config.exists():
        example = REPO / "config" / "machine.example.toml"
        print(f"{config} がありません。{example} をコピーしてから実行してください")
        return 1
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    changes = []
    if args.auto:
        changes += auto_values(raw)
    if args.apply_proposed:
        proposed = config.with_name("machine.proposed.toml")
        if not proposed.exists():
            print(f"{proposed} がありません。先に lao p0 propose を実行してください")
            return 1
        changes += proposed_values(tomllib.loads(proposed.read_text(encoding="utf-8")))
    for item in args.set or []:
        name, sep, value = item.partition("=")
        section, dot, key = name.strip().rpartition(".")
        if not sep or not dot:
            print(f"--set は 表.鍵=値 の形で指定してください：{item}")
            return 1
        changes.append((section, key, parse_value(value.strip()), "指定された値"))
    if not changes:
        print("変える値はありません")
        return 0
    try:
        apply(config, changes, dry_run=args.dry_run)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        print(f"書き換えられませんでした（ファイルは元のままです）：{e}")
        return 1
    for section, key, value, why in changes:
        shown = "（表示しません）" if key == "api_key" else value
        print(f"  [{section}] {key} = {shown}　… {why}")
    print("（確認のみ。書き込んでいません）" if args.dry_run else f"{config} を書き換えました")
    return 0


def cmd_p0(args) -> int:
    from .config import load_machine
    from .p0.probe import propose, run

    config, catalog_dir = _paths(args)
    machine = load_machine(config)
    results = Path(args.results or REPO / "p0-results")
    if args.p0 == "pairs":
        return cmd_pairs(args, machine, catalog_dir, results)
    if args.p0 == "run":
        envs = args.envs.split(",") if args.envs else ["e1", "e2", "e3", "e4"]
        run(machine, catalog_dir, results, gpu_python=args.gpu_python, envs=envs, skip_models=args.skip_models)
        return 0
    out = config.with_name("machine.proposed.toml")
    r = propose(machine, results, out)
    from .p0.pairs import latest
    best = latest(results)
    if best and best["ranking"]["recommended"]:
        r["notes"].append(f"組み合わせ評価（{best['timestamp']}）の推奨：{best['ranking']['recommended']}"
                          "（catalog の構成は手で選んでください）")
    print(f"測定結果：{r['source']}")
    for n in r["notes"]:
        print(f"  - {n}")
    print(f"\n提案：{out}\n内容を確認し、問題なければ {config.name} に上書きしてください")
    return 0


def cmd_pairs(args, machine, catalog_dir: Path, results: Path) -> int:
    from .p0.pairs import PairsError, finish, normalize_answers, run

    try:
        if args.score:
            out = Path(args.score)
            answers = json.loads((out / "answers.json").read_text(encoding="utf-8"))
            finish(out, normalize_answers(answers, args.judge or ""))
            print(f"採点しました：{out / 'report.md'}")
            return 0
        suite = Path(args.suite or catalog_dir / "shared" / "pairs.toml")
        run(machine, catalog_dir, suite, results, only=args.only or "", judge=args.judge, no_judge=args.no_judge)
    except (PairsError, FileNotFoundError) as e:
        print(e)
        return 1
    return 0


def cmd_pattern(args) -> int:
    """パターンの一覧・下書き・検証・承認（会話モデルが組み合わせて使う ComfyUI のワークフロー）。"""
    from .config import load_catalog
    from .p0.pairs import PairsError, resolve
    from .patterns import PatternError, approve, check, load_allowlist, load_pattern, patterns_for, scaffold

    _, catalog_dir = _paths(args)
    catalog = load_catalog(catalog_dir)
    try:
        if args.action == "list":
            for env in catalog.values():
                for conf in env.configurations.values():
                    for p in patterns_for(catalog_dir, conf):
                        mark = "使える" if p.usable else ("問題あり" if p.problems else
                                                         ("承認後に変更あり" if p.approved else "未承認"))
                        print(f"{env.id}/{conf.id}.{p.component}  {p.id}  [{mark}]  {p.does or '、'.join(p.problems)}")
            return 0
        if args.action == "new":
            if not (args.component and args.id):
                print("--component（環境.構成.部品）と --id が要ります")
                return 1
            _, _, comp = resolve(catalog, args.component)
            if not comp.raw.get("patterns"):
                print(f"{args.component} には patterns のフォルダがありません（catalog に patterns = \"…\" を書く）")
                return 1
            out = scaffold(Path(args.target), catalog_dir / comp.raw["patterns"], args.id)
            print(f"下書きを作りました：{out}\n説明（does・needs・good_for・avoid）と work_gb を書き、"
                  f"lao pattern approve {out} で承認します")
            return 0
        p = load_pattern(Path(args.target))
        allowed = load_allowlist(catalog_dir)
        problems = check(p, allowed) if args.action == "check" else approve(p, allowed)
    except (PatternError, PairsError, FileNotFoundError, ValueError) as e:
        print(e)
        return 1
    for msg in problems:
        print(f"[NG] {msg}")
    if problems:
        return 1
    print("[OK] 問題ありません" if args.action == "check" else f"[OK] 承認しました：{p.id}")
    return 0


def cmd_sandbox_check(args) -> int:
    from .config import load_machine
    from .sandbox import SandboxConfig, check

    config, _ = _paths(args)
    machine = load_machine(config)
    conf = SandboxConfig.from_machine(machine.raw, Path(machine.raw["paths"]["root"]) / "sandbox")
    print(f"コード実行の箱を確かめます（mode = {conf.mode}、作業フォルダ {conf.work_host}）")
    rows = asyncio.run(check(conf))
    for name, ok, detail in rows:
        print(f"[{'OK' if ok else 'NG'}] {name}" + (f"：{detail}" if detail else ""))
    if all(ok for _, ok, _ in rows):
        print("\nすべて合格です。machine.toml の [sandbox] で enabled = true にするとコード実行が使えます")
        return 0
    print("\n不合格があります。コード実行は有効にしないでください")
    return 1


def cmd_mcp(args) -> int:
    from .config import load_machine
    from .mcp import run

    config, _ = _paths(args)
    if args.fake:
        from .config import MachineConfig
        from .demo import demo_machine

        machine = MachineConfig(demo_machine(Path(tempfile.gettempdir()) / "lao-demo"))
    else:
        machine = load_machine(config)
    return run(machine, url=args.url, check=args.check)


def cmd_update(args) -> int:
    from .config import load_machine
    from .updater import UpdateError, update

    config, _ = _paths(args)
    try:
        return update(load_machine(config), execute=args.execute, restart=not args.no_restart)
    except UpdateError as e:
        print(f"更新できませんでした：{e}")
        return 1


def cmd_backup(args) -> int:
    from .config import load_machine
    from .maintenance import backup

    config, _ = _paths(args)
    out = backup(Path(load_machine(config).path_of("root")), keep=args.keep)
    print(out)
    return 0


def cmd_usage(args) -> int:
    from .config import load_machine
    from .usage import UsageLog, summary_text

    config, _ = _paths(args)
    log = UsageLog(Path(load_machine(config).path_of("root")) / "usage")
    try:
        r = log.report(args.month or "")
    except ValueError as e:
        print(e)
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=1) if args.json else summary_text(r))
    return 0


def cmd_key(args) -> int:
    """lao serve の API キーを出す（メイン PC の準備スクリプトが画面のショートカットを作るときに使う）。"""
    from .config import load_machine
    from .mcp import LaoError, server_key

    config, _ = _paths(args)
    try:
        print(server_key(load_machine(config)))
    except LaoError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lao", description="RTX Spark 向けローカルAI環境ツール")
    p.add_argument("--config", help="machine.toml のパス")
    p.add_argument("--catalog", help="カタログのフォルダ")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="設定・カタログ・機器・エンジンを点検する")
    for name, helptext in (("plan", "取得計画を表示する"), ("install", "モデルを取得する（既定は確認のみ）")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("env", choices=["e1", "e2", "e3", "e4", "embedding"])
        sp.add_argument("--configuration")
        if name == "install":
            sp.add_argument("--execute", action="store_true", help="実際に取得する")
    sp = sub.add_parser("serve", help="サービスを起動する")
    sp.add_argument("--env", choices=["e1", "e2", "e3", "e4"])
    sp.add_argument("--configuration")
    sp.add_argument("--port", type=int)
    sp.add_argument("--fake", action="store_true", help="ダミーエンジンで画面と流れだけ確認する")
    sp = sub.add_parser("p0", help="実機検証")
    sp.add_argument("p0", choices=["run", "propose", "pairs"],
                    help="run＝機器とモデルの測定、propose＝設定の提案、pairs＝会話モデル＋生成モデルの組み合わせ評価")
    sp.add_argument("--results", help="結果フォルダ")
    sp.add_argument("--gpu-python", help="PyTorch(CUDA) が入った Python。GPU の確保上限を測る")
    sp.add_argument("--envs", help="測る環境（例：e1,e2）")
    sp.add_argument("--skip-models", action="store_true")
    sp.add_argument("--suite", help="pairs：課題と候補のファイル（既定 catalog/shared/pairs.toml）")
    sp.add_argument("--only", help="pairs：名前にこの文字を含む組み合わせだけ測る")
    sp.add_argument("--judge", help="pairs：判定役（all＝画像を見られる全モデル、または 環境.構成.部品 をカンマ区切り）。"
                    "--score では answers.json の判定役の名前")
    sp.add_argument("--no-judge", action="store_true", help="pairs：生成だけ行い、判定は後で（answers.json）")
    sp.add_argument("--score", metavar="DIR", help="pairs：DIR の answers.json で採点し直す")
    sp = sub.add_parser("configure", help="machine.toml をコメントを残したまま書き換える")
    sp.add_argument("--auto", action="store_true", help="ffmpeg・llama-server の場所と固定の API キーを自動で埋める")
    sp.add_argument("--apply-proposed", action="store_true", help="lao p0 propose の測定値を採用する")
    sp.add_argument("--set", action="append", metavar="表.鍵=値", help="値を一つ書く（何度でも）")
    sp.add_argument("--dry-run", action="store_true", help="書き込まずに変更点だけ表示する")
    sp = sub.add_parser("pattern", help="会話モデルが組み合わせて使う ComfyUI のパターン（一覧・下書き・検証・承認）")
    sp.add_argument("action", choices=["list", "new", "check", "approve"])
    sp.add_argument("target", nargs="?", default="", help="new：書き出したワークフロー JSON、check・approve：パターンの TOML")
    sp.add_argument("--component", help="new：パターンを置く生成部品（環境.構成.部品）")
    sp.add_argument("--id", help="new：パターンの id（catalog/patterns/README.md の標準の名前にそろえる）")
    sub.add_parser("sandbox-check", help="コード実行の箱が通信できず、PC のファイルも見えないことを確かめる")
    sp = sub.add_parser("mcp", help="別の PC の Claude Code から使う MCP サーバー（SSH 越しに起動する）")
    sp.add_argument("--check", action="store_true", help="lao serve につながるかだけ確かめる")
    sp.add_argument("--url", help="lao serve の URL（既定は machine.toml の port）")
    sp.add_argument("--fake", action="store_true", help="lao serve --fake につなぐ")
    sp = sub.add_parser("update", help="lao を最新にして lao serve を再起動する（動かなければ元に戻す）")
    sp.add_argument("--execute", action="store_true", help="実際に更新する（無ければ手順の表示だけ）")
    sp.add_argument("--no-restart", action="store_true", help="lao serve を再起動しない")
    sp = sub.add_parser("backup", help="会話・記憶・設定などを zip にまとめ、場所を表示する")
    sp.add_argument("--keep", type=int, default=14, help="残すバックアップの数")
    sub.add_parser("key", help="lao serve の API キーを表示する")
    sp = sub.add_parser("usage", help="Claude のトークンの記録（lao に任せた分の節約と、Claude Code の実際の使用量）")
    sp.add_argument("--month", help="YYYY-MM（省略すると今月）")
    sp.add_argument("--json", action="store_true", help="JSON で出す（動画やグラフ用）")
    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    if args.cmd == "install":
        return cmd_plan(args, execute=args.execute)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "configure":
        return cmd_configure(args)
    if args.cmd == "pattern":
        if args.action != "list" and not args.target:
            print("ファイルを指定してください")
            return 1
        return cmd_pattern(args)
    if args.cmd == "sandbox-check":
        return cmd_sandbox_check(args)
    if args.cmd == "mcp":
        return cmd_mcp(args)
    if args.cmd in ("update", "backup", "key", "usage"):
        return {"update": cmd_update, "backup": cmd_backup, "key": cmd_key, "usage": cmd_usage}[args.cmd](args)
    return cmd_p0(args)


if __name__ == "__main__":
    sys.exit(main())
