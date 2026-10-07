#!/usr/bin/env python3
"""Record and later compare explicitly scoped working-tree file states."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

VERSION = "0.1.0"


def _safe_fd_support() -> bool:
    return (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.scandir in os.supports_fd
    )


def _open_path_nofollow(path: Path, *, directory: bool = False) -> int:
    """Open through no-follow directory descriptors; fail closed if unavailable."""
    if not _safe_fd_support():
        raise OSError("descriptor-based no-follow access is unavailable")
    absolute = path.absolute()
    parts = absolute.parts[1:]
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not parts:
            raise OSError("expected a file path")
        return fd
    except Exception:
        os.close(fd)
        raise


def _hash_fd(fd: int) -> str:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("opened path is not a regular file")
    h = hashlib.sha256()
    with os.fdopen(fd, "rb", closefd=True) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_file_nofollow(path: Path) -> tuple[os.stat_result, bytes]:
    fd = _open_path_nofollow(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("opened path is not a regular file")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            data = stream.read()
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise OSError("file changed while being read")
        return after, data
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def scan(root: Path, includes: list[str]) -> dict[str, Any]:
    """Hash regular files under explicit paths without following symlinks."""
    root = root.absolute()
    files: dict[str, str] = {}
    uncovered: list[dict[str, str]] = []
    root_identity: dict[str, int] | None = None

    def skip(path: Path, reason: str) -> None:
        try:
            rel = path.absolute().relative_to(root).as_posix() if path != root else "."
        except ValueError:
            rel = path.name or "<outside-root>"
        item = {"path": rel, "reason": reason}
        if item not in uncovered:
            uncovered.append(item)

    if not _safe_fd_support():
        for include in includes:
            skip(root / include, "descriptor-no-follow-unavailable")
        return {"files": {}, "uncovered": uncovered, "root_identity": None}

    def readlink_reason(parent_fd: int, name: str, rel: str) -> str:
        try:
            target = os.readlink(name, dir_fd=parent_fd)
            target_path = Path(target)
            candidate = target_path if target_path.is_absolute() else root / Path(rel).parent / target_path
            return "symlink-outside-root" if not candidate.resolve(strict=False).is_relative_to(root) else "symlink"
        except (OSError, RuntimeError, ValueError):
            return "symlink"

    def nested_repo(directory_fd: int) -> bool:
        try:
            os.stat(".git", dir_fd=directory_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def walk_dir(directory_fd: int, rel_dir: str) -> None:
        try:
            dir_info = os.fstat(directory_fd)
            if not (dir_info.st_mode & 0o555):
                skip(root / rel_dir, "unreadable-directory")
                return
            with os.scandir(directory_fd) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError:
            skip(root / rel_dir, "unreadable-directory")
            return
        for name in names:
            if name == ".git":
                continue
            rel = f"{rel_dir}/{name}".lstrip("/") if rel_dir else name
            path = root / rel
            try:
                before_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                skip(path, "unreadable-or-raced")
                continue
            if stat.S_ISLNK(before_info.st_mode):
                skip(path, readlink_reason(directory_fd, name, rel))
                continue
            if stat.S_ISDIR(before_info.st_mode):
                child_fd = None
                try:
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (before_info.st_dev, before_info.st_ino):
                        skip(path, "directory-changed-during-scan")
                        continue
                    if nested_repo(child_fd):
                        skip(path, "nested-repository-or-submodule")
                        continue
                    walk_dir(child_fd, rel)
                except PermissionError:
                    skip(path, "unreadable-directory")
                except OSError:
                    skip(path, "unreadable-or-raced-directory")
                finally:
                    if child_fd is not None:
                        try:
                            os.close(child_fd)
                        except OSError:
                            pass
                continue
            if not stat.S_ISREG(before_info.st_mode):
                skip(path, "not-regular-file")
                continue
            if not (before_info.st_mode & 0o444):
                skip(path, "unreadable")
                continue
            try:
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                opened = os.fstat(file_fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before_info.st_dev, before_info.st_ino):
                    os.close(file_fd)
                    skip(path, "file-changed-during-scan")
                    continue
                files[rel] = _hash_fd(file_fd)
            except OSError:
                skip(path, "unreadable-or-raced")

    try:
        root_fd = _open_path_nofollow(root, directory=True)
    except OSError:
        for include in includes:
            skip(root / include, "root-unavailable-or-raced")
        return {"files": {}, "uncovered": uncovered, "root_identity": None}
    try:
        root_info = os.fstat(root_fd)
        root_identity = {"device": root_info.st_dev, "inode": root_info.st_ino}
        for include in includes:
            relative = Path(include)
            if relative.is_absolute() or ".." in relative.parts:
                skip(root / include, "outside-root")
                continue
            parts = [part for part in relative.parts if part not in (".", "")]
            if ".git" in parts:
                skip(root / include, "git-metadata")
                continue
            current_fd = os.dup(root_fd)
            rel_parts: list[str] = []
            if not parts:
                walk_dir(current_fd, "")
                os.close(current_fd)
                continue
            for index, part in enumerate(parts):
                rel_parts.append(part)
                rel = "/".join(rel_parts)
                try:
                    before_info = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                except OSError:
                    skip(root / rel, "missing-or-unreadable")
                    break
                if stat.S_ISLNK(before_info.st_mode):
                    skip(root / rel, readlink_reason(current_fd, part, rel))
                    break
                is_final = index == len(parts) - 1
                if stat.S_ISDIR(before_info.st_mode):
                    if is_final:
                        try:
                            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                            opened = os.fstat(child_fd)
                            if (opened.st_dev, opened.st_ino) != (before_info.st_dev, before_info.st_ino):
                                skip(root / rel, "directory-changed-during-scan")
                            elif nested_repo(child_fd):
                                skip(root / rel, "nested-repository-or-submodule")
                            else:
                                walk_dir(child_fd, rel)
                            os.close(child_fd)
                        except OSError:
                            skip(root / rel, "unreadable-or-raced-directory")
                        break
                    try:
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
                        opened = os.fstat(next_fd)
                        if (opened.st_dev, opened.st_ino) != (before_info.st_dev, before_info.st_ino):
                            os.close(next_fd)
                            skip(root / rel, "directory-changed-during-scan")
                            break
                        if nested_repo(next_fd):
                            os.close(next_fd)
                            skip(root / rel, "nested-repository-or-submodule")
                            break
                        os.close(current_fd)
                        current_fd = next_fd
                    except OSError:
                        skip(root / rel, "unreadable-or-raced-directory")
                        break
                    continue
                if not is_final:
                    skip(root / rel, "not-a-directory")
                    break
                if not stat.S_ISREG(before_info.st_mode):
                    skip(root / rel, "not-regular-file")
                    break
                if not (before_info.st_mode & 0o444):
                    skip(root / rel, "unreadable")
                    break
                try:
                    file_fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
                    opened = os.fstat(file_fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before_info.st_dev, before_info.st_ino):
                        os.close(file_fd)
                        skip(root / rel, "file-changed-during-scan")
                    else:
                        files[rel] = _hash_fd(file_fd)
                except OSError:
                    skip(root / rel, "unreadable-or-raced")
                break
            os.close(current_fd)
    finally:
        os.close(root_fd)
    return {"files": dict(sorted(files.items())), "uncovered": sorted(uncovered, key=lambda x: (x["path"], x["reason"])), "root_identity": root_identity}


def diff_manifests(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    a, b = before.get("files", {}), after.get("files", {})
    return {
        "added": sorted(b.keys() - a.keys()),
        "deleted": sorted(a.keys() - b.keys()),
        "changed": sorted(key for key in a.keys() & b.keys() if a[key] != b[key]),
    }


def current_comparison(record: dict[str, Any]) -> tuple[str, dict[str, list[str]], list[dict[str, str]]]:
    root = Path(record["root"])
    includes = record["includes"]
    baseline = record["after_manifest"]
    try:
        resolved_root = root.resolve(strict=True)
        root_stat = root.stat()
        saved_identity = record["root_identity"]
        if str(resolved_root) != record["root"] or {
            "device": root_stat.st_dev, "inode": root_stat.st_ino
        } != saved_identity:
            return "unknown", {"added": [], "deleted": [], "changed": []}, [{"path": ".", "reason": "root-changed"}]
    except (OSError, KeyError, TypeError):
        return "unknown", {"added": [], "deleted": [], "changed": []}, [{"path": ".", "reason": "root-unavailable"}]
    now = scan(root, includes)
    if now.get("root_identity") != saved_identity:
        return "unknown", {"added": [], "deleted": [], "changed": []}, [{"path": ".", "reason": "root-changed-during-scan"}]
    changes = diff_manifests(baseline, now)
    unknown = bool(baseline.get("uncovered") or now.get("uncovered"))
    junit_state = record.get("junit", {}).get("status", "unknown")
    unknown = unknown or junit_state not in ("valid", "not-configured")
    status = "changed" if any(changes.values()) else ("unknown" if unknown else "current")
    return status, changes, now["uncovered"]


def _junit_summary(report: Path, started_ns: int, before: dict[str, Any] | None) -> dict[str, Any]:
    try:
        info, data = read_file_nofollow(report)
    except FileNotFoundError:
        return {"status": "missing", "tests": None, "failures": None, "errors": None, "skipped": None}
    except OSError:
        return {"status": "unknown", "tests": None, "failures": None, "errors": None, "skipped": None}
    current_hash = hashlib.sha256(data).hexdigest()
    # A prior report is accepted only if its content or metadata changed and its
    # timestamp is after this invocation began. Reused/stale reports are unknown.
    changed = before is None or before.get("sha256") != current_hash or before.get("mtime_ns") != info.st_mtime_ns
    if info.st_mtime_ns < started_ns or not changed:
        return {"status": "stale-or-unattributed", "tests": None, "failures": None, "errors": None, "skipped": None}
    try:
        root_element = ET.fromstring(data)
        suites = [root_element] if root_element.tag == "testsuite" else list(root_element.iter("testsuite"))
        if not suites:
            raise ValueError("no testsuite")
        totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for suite in suites:
            for key in totals:
                value = suite.get(key)
                if value is None:
                    if key == "tests":
                        totals[key] += sum(1 for _ in suite.iter("testcase"))
                else:
                    n = int(value)
                    if n < 0:
                        raise ValueError("negative count")
                    totals[key] += n
        return {"status": "valid", **totals}
    except (ET.ParseError, OSError, ValueError, TypeError):
        return {"status": "invalid", "tests": None, "failures": None, "errors": None, "skipped": None}


def _report_markdown(record: dict[str, Any]) -> str:
    comparison, changes = record["status"], record["diff"]
    junit = record["junit"]
    cmd_exit = record["command"]["exit_code"]
    tests = f"{junit['tests']}" if junit["status"] == "valid" else "unbekannt"
    lines = [
        f"# Prüfbeleg: {record['check_name']}", "",
        f"- Arbeitsstand nach dem Lauf: **{comparison}**",
        f"- Prüfkommando-Exitcode: `{cmd_exit}`",
        f"- JUnit-Bericht: `{junit['status']}`; Tests: {tests}",
        f"- Manifeständerungen während des Laufs: {sum(map(len, changes.values()))}",
        f"- Nicht abgedeckte Einträge: {len(record['after_manifest']['uncovered'])}", "",
        "Dateinamenänderungen während des Laufs:", "",
    ]
    for key in ("added", "deleted", "changed"):
        lines.append(f"- {key}: {len(changes[key])}")
    lines.extend(["", "Grenze: Gleiche Vorher-/Nachher-Hashes schließen zwischenzeitliche Änderungen nicht aus. Dieser Beleg garantiert weder Fehlerfreiheit noch Manipulationsschutz.", ""])
    return "\n".join(lines)


def run_command(config_path: Path, name: str, output: Path, markdown: Path, argv: list[str]) -> int:
    config_path = config_path.resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = (config_path.parent / config["root"]).resolve()
    includes = config["include"]
    if not isinstance(includes, list) or not includes or not all(isinstance(p, str) for p in includes):
        raise ValueError("config.include must be a non-empty list of paths")
    if not name.strip() or any(c in name for c in "\r\n\0"):
        raise ValueError("check name must be a short, safe label")
    if not argv:
        raise ValueError("provide a command after --")
    before = scan(root, includes)
    junit_path = config.get("junit_report")
    if junit_path:
        configured_report = Path(junit_path)
        report_spelling = (configured_report if configured_report.is_absolute() else config_path.parent / configured_report).absolute()
        report = report_spelling.parent.resolve(strict=True) / report_spelling.name
    else:
        report = None
    prior = None
    if report:
        try:
            st, data = read_file_nofollow(report)
            prior = {"sha256": hashlib.sha256(data).hexdigest(), "mtime_ns": st.st_mtime_ns}
        except FileNotFoundError:
            prior = None
        except OSError:
            prior = {"unreadable": True}
    start = time.time_ns()
    completed = subprocess.run(argv, cwd=root, check=False, shell=False)
    after = scan(root, includes)
    changes = diff_manifests(before, after)
    junit = _junit_summary(report, start, prior) if report else {"status": "not-configured", "tests": None, "failures": None, "errors": None, "skipped": None}
    record: dict[str, Any] = {
        "schema_version": 1, "tool_version": VERSION, "check_name": name,
        "root": str(root), "includes": includes,
        "root_identity": before["root_identity"],
        "started_at_unix_ns": start, "finished_at_unix_ns": time.time_ns(),
        "before_manifest": before, "after_manifest": after,
        "diff": changes,
        "command": {"exit_code": completed.returncode}, "junit": junit,
    }
    status, _, _ = current_comparison(record)
    record["status"] = status
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(_report_markdown(record), encoding="utf-8")
    print(f"Beleg gespeichert: {output}")
    print(f"Status nach Lauf: {status}; Exitcode: {completed.returncode}; JUnit: {junit['status']}")
    return completed.returncode


def compare(record_path: Path) -> int:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    status, changes, uncovered = current_comparison(record)
    print(status)
    if any(changes.values()):
        print(json.dumps(changes, ensure_ascii=False, indent=2))
    if uncovered:
        print(f"Nicht abgedeckt: {len(uncovered)} Einträge")
    return 0 if status == "current" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checked-worktree", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p_run = sub.add_parser("run", help="record a scoped before/after state while running an explicit command")
    p_run.add_argument("--config", type=Path, required=True)
    p_run.add_argument("--name", required=True, help="safe, user-chosen label; command arguments are never exported")
    p_run.add_argument("--json", dest="output", type=Path, required=True)
    p_run.add_argument("--markdown", type=Path, required=True)
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments after --")
    p_compare = sub.add_parser("compare", help="compare the saved after-manifest against the current files")
    p_compare.add_argument("record", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            command = args.command[1:] if args.command and args.command[0] == "--" else args.command
            return run_command(args.config, args.name, args.output, args.markdown, command)
        return compare(args.record)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"checked-worktree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
