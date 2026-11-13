#!/usr/bin/env python3
"""Build a deterministic, checksummed archive from one versioned source tree."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


def version(source):
    tree = ast.parse((source/"version.py").read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            value = ast.literal_eval(node.value)
            if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
                return value
    raise ValueError("Invalid package version")


def files(source):
    manifest = json.loads((source/"package_manifest.json").read_text("utf-8"))
    names = manifest["runtime"]+manifest["support"]
    names += [str(p.relative_to(source)) for p in (source/"tests").rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json")]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate package member")
    for name in names:
        path = source/name
        if Path(name).is_absolute() or ".." in Path(name).parts or not path.is_file() or path.is_symlink():
            raise ValueError("Missing/unsafe package member: "+name)
    return sorted(names)


def build(source, output, commit=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Refusing to overwrite an existing release archive")
    if commit is None:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("A full source commit SHA is required")
    names = files(source)
    payload = {name: (source/name).read_bytes() for name in names}
    metadata = {"version": version(source), "commit": commit,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
    payload["BUILD.json"] = (json.dumps(metadata, sort_keys=True, indent=2)+"\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo("tinvest_ubuntu_collector/"+name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+".sha256").write_text(digest+"  "+output.name+"\n", encoding="ascii")
    return metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--commit")
    args = p.parse_args()
    print(json.dumps(build(args.source, args.output, args.commit), sort_keys=True))


if __name__ == "__main__":
    main()
