"""Collect redistributable notices and prepare a reviewed local public-beta bundle.

Run `notices` in the locked build environment before building wheels/EXEs, then
`bundle --exe ... --gitleaks ... --pilot ...`. Publication is a separate action.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "src/panini_cli/licenses"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def license_files(folder):
    return sorted(p for p in folder.rglob("*") if p.is_file() and
        any(p.name.upper().startswith(word) for word in ("LICENSE", "LICENCE", "COPYING", "NOTICE")) and
        p.suffix.lower() in {"", ".txt", ".md", ".rst"})


def collect_notices():
    inventory, sections = [], []

    def add(name, version, license_name, texts, ecosystem):
        if not texts:
            raise ValueError(f"No redistribution license text found for {name}")
        combined = "\n\n".join(dict.fromkeys(texts))
        sections.append(f"{'=' * 72}\n{name} {version} ({ecosystem})\nLicense: {license_name}\n{'=' * 72}\n{combined.strip()}\n")
        inventory.append({"name": name, "version": version, "license": license_name,
                          "ecosystem": ecosystem, "notice_sha256": hashlib.sha256(combined.encode()).hexdigest()})

    from panini_cli.agent import VERSION
    add("PANINI CLI", VERSION, "MIT", [(ROOT / "LICENSE").read_text(encoding="utf-8")], "project")
    native = ROOT / "src/panini_cli/panini-sandbox"
    add("Microsoft MXC", "1.0.0", "MIT", [(native / "LICENSE.md").read_text(encoding="utf-8")], "native")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    add("CPython", sys.version.split()[0], "PSF-2.0 and bundled notices", [python_license.read_text(encoding="utf-8")], "runtime")
    names = re.findall(r"^([\w-]+)==([^\s]+)$", (ROOT / "requirements-runtime.lock").read_text(), re.MULTILINE)
    names.append(("pyinstaller", importlib.metadata.version("pyinstaller")))
    for name, expected in names:
        package = importlib.metadata.distribution(name)
        if package.version != expected:
            raise ValueError(f"Locked {name} {expected} differs from installed {package.version}")
        files = [package.locate_file(p) for p in package.files or [] if
                 any(Path(p).name.upper().startswith(word) for word in ("LICENSE", "LICENCE", "COPYING", "NOTICE"))]
        texts = [p.read_text(encoding="utf-8", errors="replace") for p in files if p.is_file() and p.stat().st_size < 2 * 1024**2]
        upstream_notice = NOTICES / f"{name}-{package.version}.txt"
        if not texts and upstream_notice.is_file():
            texts = [upstream_notice.read_text(encoding="utf-8")]
        add(name, package.version, package.metadata.get("License-Expression") or package.metadata.get("License") or "See notice text",
            texts, "python")
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo.exe")
    completed = subprocess.run([cargo, "metadata", "--locked", "--offline", "--format-version", "1",
        "--filter-platform", "x86_64-pc-windows-msvc", "--manifest-path", str(native / "src/Cargo.toml")],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    metadata = json.loads(completed.stdout)
    for package in sorted(metadata["packages"], key=lambda p: (p["name"], p["version"])):
        if not package.get("source"):
            continue  # Microsoft's workspace code is covered by its MIT notice.
        manifest = Path(package["manifest_path"])
        paths = license_files(manifest.parent)
        if package.get("license_file"):
            paths.append(manifest.parent / package["license_file"])
        texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths if p.exists()]
        upstream_notice = NOTICES / f"{package['name']}-{package['version']}.txt"
        if not texts and upstream_notice.is_file():
            texts = [upstream_notice.read_text(encoding="utf-8")]
        if not texts and package["name"] in {"tracelogging", "tracelogging_macros"}:
            texts = [(native / "LICENSE.md").read_text(encoding="utf-8")]
        add(package["name"], package["version"], package.get("license") or "See notice text", texts, "rust")
    NOTICES.mkdir(parents=True, exist_ok=True)
    (NOTICES / "THIRD_PARTY_NOTICES.txt").write_text("PANINI CLI redistribution notices\n\n"
        "This inventory covers the locked Python runtime, PyInstaller bootloader, and native MXC Cargo dependencies.\n"
        "It may include platform-specific Cargo dependencies not linked into this target.\n\n" + "\n".join(sections), encoding="utf-8")
    (NOTICES / "manifest.json").write_text(json.dumps({"format": 1, "components": inventory}, indent=2), encoding="utf-8")
    print(f"Collected {len(inventory)} component notices: {NOTICES}")


def source_files():
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
        check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    roots = {"src", "tests", "scripts", "assets", ".github"}
    top = {".gitignore", ".gitattributes", ".coveragerc", ".env.example", "README.md", "SECURITY.md", "LICENSE",
           "CONTRIBUTING.md", "pyproject.toml", "packaging_entry.py", "run.py", "Start-Panini.cmd",
           "requirements-runtime.lock", "requirements-build.lock", "JEV-RETRIEVAL.md"}
    for name in sorted(set(files)):
        path = ROOT / name
        parts = Path(name).parts
        selected = name in top or parts and parts[0] in roots or name in {"docs/SANDBOX.md", "docs/ROUTER.md"}
        selected = selected or name in {"evals/run.py", "evals/tasks.json", "evals/harbor_adapter.py", "evals/pilot.py"}
        selected = selected or name.startswith("evals/pilots/") and path.suffix in {".json", ".html", ".md"}
        if selected and path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(ROOT) and not any(p in {"__pycache__", ".pytest_cache"} for p in parts):
            yield path, name.replace("\\", "/")


def scan(gitleaks, mode, source, report):
    completed = subprocess.run([str(gitleaks), mode, str(source), "--redact", "--no-banner",
        "--report-format", "json", "--report-path", str(report)], capture_output=True, text=True, encoding="utf-8", timeout=240)
    if completed.returncode not in {0, 1}:
        raise ValueError(f"Secret scanner failed ({completed.returncode}); private diagnostics were retained")
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return {"passed": completed.returncode == 0, "findings": len(findings)}


def bundle(args):
    from panini_cli.agent import VERSION
    folder = ROOT / "build/public-beta" / VERSION
    folder.mkdir(parents=True, exist_ok=True)
    private = ROOT / "build/beta/secret-scans" / VERSION
    private.mkdir(parents=True, exist_ok=True)
    # Export a fresh allowlisted source snapshot, excluding history and private evaluation logs.
    snapshot = ROOT / "build" / ("public-source-" + VERSION)
    if snapshot.exists():
        raise ValueError("Source snapshot already exists. Use a fresh version or move the previous snapshot before preparing again.")
    snapshot.mkdir()
    for path, name in source_files():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    source_scan = scan(args.gitleaks, "dir", snapshot, private / "source.json")
    history_scan = scan(args.gitleaks, "git", ROOT, private / "history.json")
    if not source_scan["passed"]:
        raise ValueError("Source secret scan found items. Review private reports before publication; no source archive was prepared.")
    archive = folder / f"panini-cli-{VERSION}-source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(snapshot.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(snapshot).as_posix())
    binary = folder / "panini-cli.exe"
    shutil.copyfile(args.exe, binary)
    subprocess.run([str(binary), "--version"], check=True, timeout=30)
    output = subprocess.run([str(binary), "licenses"], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30).stdout
    if output != (NOTICES / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8"):
        raise ValueError("Executable notices differ from the source release")
    for name, source in (("LICENSE.txt", ROOT / "LICENSE"), ("THIRD_PARTY_NOTICES.txt", NOTICES / "THIRD_PARTY_NOTICES.txt"),
                         ("components.json", NOTICES / "manifest.json")):
        shutil.copyfile(source, folder / name)
    if args.pilot:
        pilot_scan = scan(args.gitleaks, "dir", args.pilot, private / "pilot.json")
        if not pilot_scan["passed"]:
            raise ValueError("Pilot report secret scan failed; do not publish its traces")
        with zipfile.ZipFile(folder / "pilot-results.zip", "w", zipfile.ZIP_DEFLATED) as output:
            for path in sorted(args.pilot.rglob("*")):
                if path.is_file() and path.suffix in {".json", ".html", ".md"}:
                    output.write(path, path.relative_to(args.pilot).as_posix())
    else:
        pilot_scan = None
    files = {p.name: {"sha256": sha256(p), "bytes": p.stat().st_size} for p in sorted(folder.iterdir()) if
             p.is_file() and not p.name.endswith(".private.json") and p.name not in {"release.json", "SHA256SUMS.txt"}}
    (folder / "SHA256SUMS.txt").write_text("".join(f"{facts['sha256']}  {name}\n" for name, facts in files.items()), encoding="utf-8")
    manifest = {"version": VERSION, "channel": "public-beta", "files": files,
        "secret_scans": {"source_snapshot": source_scan, "git_history": history_scan, "pilot": pilot_scan},
        "known_limits": ["Executable is unsigned", "Clean Windows machine UAT has not been completed",
                         "No independent security certification", "Harbor pilot does not test Windows MXC"],
        "publication": {"repository": None, "history_safe_to_publish": history_scan["passed"],
                        "source_snapshot_safe_to_publish": source_scan["passed"]}}
    (folder / "release.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Prepared local beta: {folder}; history scan passed: {history_scan['passed']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("notices")
    package = commands.add_parser("bundle")
    package.add_argument("--exe", type=Path, required=True)
    package.add_argument("--gitleaks", type=Path, required=True)
    package.add_argument("--pilot", type=Path)
    args = parser.parse_args()
    if args.command == "notices":
        collect_notices()
    else:
        bundle(args)


if __name__ == "__main__":
    main()
