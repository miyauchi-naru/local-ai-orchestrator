"""コマンド：lao doctor | plan | install | serve | p0 run | p0 propose"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
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
        print("ダミーエンジンで起動します（本物のモデルは使いません）")
    else:
        machine = load_machine(config)
        service = Service(machine, catalog_dir)
    key = machine["server"].get("api_key") or secrets.token_urlsafe(18)
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


def cmd_p0(args) -> int:
    from .config import load_machine
    from .p0.probe import propose, run

    config, catalog_dir = _paths(args)
    machine = load_machine(config)
    results = Path(args.results or REPO / "p0-results")
    if args.p0 == "run":
        envs = args.envs.split(",") if args.envs else ["e1", "e2", "e3", "e4"]
        run(machine, catalog_dir, results, gpu_python=args.gpu_python, envs=envs, skip_models=args.skip_models)
        return 0
    out = config.with_name("machine.proposed.toml")
    r = propose(machine, results, out)
    print(f"測定結果：{r['source']}")
    for n in r["notes"]:
        print(f"  - {n}")
    print(f"\n提案：{out}\n内容を確認し、問題なければ {config.name} に上書きしてください")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lao", description="RTX Spark 向けローカルAI環境ツール")
    p.add_argument("--config", help="machine.toml のパス")
    p.add_argument("--catalog", help="カタログのフォルダ")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="設定・カタログ・機器・エンジンを点検する")
    for name, helptext in (("plan", "取得計画を表示する"), ("install", "モデルを取得する（既定は確認のみ）")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("env", choices=["e1", "e2", "e3", "e4"])
        sp.add_argument("--configuration")
        if name == "install":
            sp.add_argument("--execute", action="store_true", help="実際に取得する")
    sp = sub.add_parser("serve", help="サービスを起動する")
    sp.add_argument("--env", choices=["e1", "e2", "e3", "e4"])
    sp.add_argument("--configuration")
    sp.add_argument("--port", type=int)
    sp.add_argument("--fake", action="store_true", help="ダミーエンジンで画面と流れだけ確認する")
    sp = sub.add_parser("p0", help="実機検証")
    sp.add_argument("p0", choices=["run", "propose"])
    sp.add_argument("--results", help="結果フォルダ")
    sp.add_argument("--gpu-python", help="PyTorch(CUDA) が入った Python。GPU の確保上限を測る")
    sp.add_argument("--envs", help="測る環境（例：e1,e2）")
    sp.add_argument("--skip-models", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    if args.cmd == "install":
        return cmd_plan(args, execute=args.execute)
    if args.cmd == "serve":
        return cmd_serve(args)
    return cmd_p0(args)


if __name__ == "__main__":
    sys.exit(main())
