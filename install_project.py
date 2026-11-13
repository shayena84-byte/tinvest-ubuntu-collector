#!/usr/bin/env python3
"""One offline, staged installer for all modules. Trading is never enabled."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from runtime_lock import engine_lock
from version import VERSION

TRADING = ("tinvest-sandbox-autotrader", "tinvest-sandbox-ai", "tinvest-sandbox-local-ai", "tinvest-sandbox-openai")


def verify_source(source):
    source = Path(source)
    manifest = json.loads((source/"package_manifest.json").read_text("utf-8"))
    for name in manifest["runtime"]:
        path = source/name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or not path.is_file():
            raise ValueError("Missing/unsafe runtime file: "+name)
    build_file = source/"BUILD.json"
    if build_file.is_file():
        build = json.loads(build_file.read_text("utf-8"))
        if build["version"] != VERSION or not set(manifest["runtime"]).issubset(build["files"]):
            raise ValueError("Package build/version mismatch")
        for name, digest in build["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts or (source/name).is_symlink():
                raise ValueError("Unsafe build manifest")
            if hashlib.sha256((source/name).read_bytes()).hexdigest() != digest:
                raise ValueError("Package checksum mismatch: "+name)
    return manifest


def verify_installed(destination, names):
    modules = [Path(n).stem for n in names if n.endswith(".py")]
    script = "import sys,importlib,json;sys.path.insert(0,sys.argv[1]);" + \
             "[importlib.import_module(n) for n in json.loads(sys.argv[2])];" + \
             "from version import VERSION;print(VERSION)"
    result = subprocess.run([sys.executable, "-I", "-c", script, str(destination), json.dumps(modules)],
                            cwd=destination, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != VERSION:
        raise RuntimeError("Installed module import check failed: "+result.stderr[-500:])


def backup_database(path, target):
    if not path or not Path(path).is_file():
        return
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True) as src:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def install_tree(source, destination, backup_root=None, databases=()):
    """Pure filesystem operation, testable without changing system services."""
    source, destination = Path(source).resolve(), Path(destination).absolute()
    manifest = verify_source(source)
    if destination.is_symlink() or destination == source:
        raise ValueError("Destination must be a separate regular directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = Path(backup_root or destination.parent/(destination.name+"-backups"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    backup = root/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    for index, path in enumerate(databases):
        backup_database(path, backup/(str(index)+"-"+Path(path).name))
    stage = Path(tempfile.mkdtemp(prefix=".tinvest-stage-", dir=destination.parent))
    replaced = False
    try:
        if destination.exists():
            shutil.copytree(destination, stage, dirs_exist_ok=True, symlinks=True)
        for name in manifest["runtime"]:
            target = stage/name
            if target.is_symlink(): target.unlink()
            shutil.copy2(source/name, target)
            target.chmod(0o700 if name.endswith(".py") else 0o600)
            if name.endswith(".py"): py_compile.compile(str(target), doraise=True)
        if (stage/"BUILD.json").exists(): (stage/"BUILD.json").unlink()
        if (source/"BUILD.json").is_file(): shutil.copy2(source/"BUILD.json", stage/"BUILD.json")
        verify_installed(stage, manifest["runtime"])
        (stage/"installed-version.json").write_text(json.dumps({"version":VERSION,"installed_utc":datetime.now(timezone.utc).isoformat()})+"\n")
        if destination.exists():
            destination.rename(backup/"code")
            replaced = True
        stage.rename(destination)
        try:
            verify_installed(destination, manifest["runtime"])
        except Exception:
            shutil.rmtree(destination)
            if replaced: (backup/"code").rename(destination)
            raise
    finally:
        if stage.exists(): shutil.rmtree(stage)
        if replaced and not destination.exists() and (backup/"code").exists():
            (backup/"code").rename(destination)
    return {"version":VERSION,"destination":str(destination),"backup":str(backup)}


def systemctl(*args, check=False):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=check)


def configure_services(destination, config_dir, units_dir):
    """Preserve all existing service overrides and proxy files."""
    units_dir.mkdir(parents=True, exist_ok=True)
    proxy = (config_dir/"proxychains.conf").is_file()
    command = "/usr/bin/python3 " + str(destination/"sandbox_supervisor.py") + " run"
    if proxy:
        if not shutil.which("proxychains4"):
            raise RuntimeError("Existing SOCKS5 configuration requires proxychains4")
        command = "/usr/bin/proxychains4 -q -f " + str(config_dir/"proxychains.conf") + " " + command
    unit = "[Unit]\nDescription=Sandbox portfolio and order observer, independent of OpenAI\nAfter=network-online.target\n[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
    unit += "Environment=SSL_CERT_FILE="+str(config_dir/"ca-bundle.pem")+"\nExecStart="+command+"\nTimeoutStartSec=245\n"
    (units_dir/"tinvest-sandbox-supervisor.service").write_text(unit)
    (units_dir/"tinvest-sandbox-supervisor.timer").write_text(
        "[Unit]\nDescription=Observe Sandbox portfolio every minute\n[Timer]\nOnCalendar=*-*-* *:*:30\nPersistent=false\nAccuracySec=5s\n[Install]\nWantedBy=timers.target\n")
    # Base units already installed by previous releases keep their proxy overrides.
    for name, script in (("tinvest-sandbox-openai", "openai_quality_trader.py"),
                         ("tinvest-sandbox-market", "market_recorder.py"),
                         ("tinvest-sandbox-dashboard", "market_dashboard.py")):
        path = units_dir/(name+".service")
        if not path.exists():
            action = "run" if name.endswith("openai") else "collect" if name.endswith("market") else "--port 8787"
            cmd = "/usr/bin/python3 "+str(destination/script)+" "+action
            if proxy and not name.endswith("dashboard"):
                cmd = "/usr/bin/proxychains4 -q -f "+str(config_dir/"proxychains.conf")+" "+cmd
            path.write_text("[Unit]\nDescription="+name+"\n[Service]\nType="+("simple" if name.endswith("dashboard") else "oneshot")+
                            "\nUMask=0077\nEnvironment=SSL_CERT_FILE="+str(config_dir/"ca-bundle.pem")+"\nExecStart="+cmd+"\n[Install]\nWantedBy=default.target\n")
        if not name.endswith("dashboard") and not (units_dir/(name+".timer")).exists():
            (units_dir/(name+".timer")).write_text("[Unit]\nDescription="+name+"\n[Timer]\nOnCalendar=*-*-* *:"+("01/5:00" if name.endswith("openai") else "00/5:00")+"\nPersistent=false\n[Install]\nWantedBy=timers.target\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dest", type=Path, default=Path.home()/"tinvest-collector")
    p.add_argument("--no-services", action="store_true", help="File-only install, e.g. isolated verification")
    p.add_argument("account_ref", nargs="?", help="Optional legacy tb_ alias for a first install")
    args = p.parse_args()
    if os.getuid() == 0:
        raise SystemExit("Run as your normal Ubuntu user, without sudo")
    source = Path(__file__).resolve().parent
    verify_source(source)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=source, check=True)
    conf = Path.home()/".config/tinvest-readonly"
    units = Path.home()/".config/systemd/user"
    stopped = []
    if not args.no_services:
        # Abort rather than assume trading is stopped when user systemd is unavailable.
        systemctl("show-environment", check=True)
        for name in TRADING:
            systemctl("disable", "--now", name+".timer")
        for name in ("tinvest-sandbox-supervisor.timer", "tinvest-sandbox-market.timer", "tinvest-sandbox-dashboard.service"):
            if systemctl("is-active", "--quiet", name).returncode == 0:
                systemctl("stop", name, check=True)
                stopped.append(name)
        # Market reads can stop safely; a running trader is guarded by locks below.
        systemctl("stop", "tinvest-sandbox-market.service")
    try:
        with ExitStack() as locks:
            locks.enter_context(engine_lock())
            locks.enter_context(engine_lock(Path.home()/".local/state/tinvest-sandbox-trader/run.lock"))
            result = install_tree(source, args.dest, databases=(
                Path.home()/".local/share/tinvest-market/market.sqlite3",
                Path.home()/".local/share/tinvest-market/trading.sqlite3"))
            conf.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not (conf/"market-recorder.json").exists():
                from market_recorder import DEFAULT_CONFIG
                from collector import private_write
                private_write(conf/"market-recorder.json", (json.dumps(DEFAULT_CONFIG)+"\n").encode())
            if args.account_ref and not (conf/"autotrader.json").exists():
                import re
                if not re.fullmatch(r"tb_[a-f0-9]{12}", args.account_ref):
                    raise ValueError("Invalid Sandbox account alias")
                from collector import private_write
                private_write(conf/"autotrader.json", (json.dumps({"account_ref":args.account_ref,"enabled":True})+"\n").encode())
            if not args.no_services:
                configure_services(args.dest, conf, units)
                systemctl("daemon-reload", check=True)
    finally:
        if not args.no_services:
            for name in stopped:
                # Do not re-enable a potentially protective supervisor without an operator action.
                if name != "tinvest-sandbox-supervisor.timer": systemctl("start", name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("v"+VERSION+" installed; "+("service state was not inspected or changed." if args.no_services else "all trading timers remain disabled."))
    print("Observer (default broker-read-only mode): systemctl --user enable --now tinvest-sandbox-supervisor.timer")
    print("Status: python3 ~/tinvest-collector/system_status.py")


if __name__ == "__main__":
    main()
