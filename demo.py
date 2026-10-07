#!/usr/bin/env python3
"""Run a synthetic checked-worktree example in a temporary directory."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

tool = Path(__file__).with_name("checked_worktree.py")
with tempfile.TemporaryDirectory(prefix="checked-worktree-demo-") as tmp:
    base = Path(tmp)
    project = base / "sample-project"
    project.mkdir()
    (project / "hello.txt").write_text("synthetic starting file\n", encoding="utf-8")
    config = base / "config.json"
    junit = base / "junit.xml"
    config.write_text(json.dumps({"root": "sample-project", "include": ["."], "junit_report": str(junit)}), encoding="utf-8")
    receipt, markdown = base / "receipt.json", base / "receipt.md"
    code = (
        "from pathlib import Path; "
        f"Path({str(project / 'demo-output.txt')!r}).write_text('created by synthetic demo\\n'); "
        f"Path({str(junit)!r}).write_text('<testsuite tests=\"1\" failures=\"0\" errors=\"0\"/>')"
    )
    result = subprocess.run([
        sys.executable, str(tool), "run", "--config", str(config), "--name", "synthetic-demo",
        "--json", str(receipt), "--markdown", str(markdown), "--", sys.executable, "-c", code,
    ], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    record = json.loads(receipt.read_text(encoding="utf-8"))
    print(f"Demo: status={record['status']}, added={record['diff']['added']}, junit={record['junit']}")
    (project / "hello.txt").write_text("edited after the check\n", encoding="utf-8")
    comparison = subprocess.run([sys.executable, str(tool), "compare", str(receipt)], check=False)
    print(f"Later comparison exit code: {comparison.returncode} (1 means changed)")
