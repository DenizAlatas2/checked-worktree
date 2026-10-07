import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import checked_worktree as cw


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "tracked.py").write_text("one")

    def tearDown(self):
        self.temp.cleanup()

    def test_detects_added_deleted_and_changed_including_untracked(self):
        (self.root / "gone.txt").write_text("gone")
        before = cw.scan(self.root, ["."])
        (self.root / "tracked.py").write_text("two")
        (self.root / "new.txt").write_text("new")
        (self.root / "gone.txt").unlink()
        changes = cw.diff_manifests(before, cw.scan(self.root, ["."]))
        self.assertEqual(changes, {"added": ["new.txt"], "deleted": ["gone.txt"], "changed": ["tracked.py"]})

    def test_symlink_and_unreadable_are_visible_exclusions(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_text("outside")
        try:
            (self.root / "escape").symlink_to(outside)
            private = self.root / "mode-zero.txt"
            private.write_text("no read bits")
            private.chmod(0)
            found = cw.scan(self.root, ["."])
            exclusions = {e["path"]: e["reason"] for e in found["uncovered"]}
            self.assertEqual(exclusions["escape"], "symlink-outside-root")
            self.assertEqual(exclusions["mode-zero.txt"], "unreadable")
            self.assertNotIn("escape", found["files"])
            self.assertNotIn("mode-zero.txt", found["files"])
        finally:
            outside.unlink(missing_ok=True)

    def test_explicit_path_cannot_escape_through_symlink_parent(self):
        outside = self.root.parent / (self.root.name + "-parent")
        outside.mkdir()
        (outside / "payload.txt").write_text("outside")
        try:
            (self.root / "linked-dir").symlink_to(outside, target_is_directory=True)
            found = cw.scan(self.root, ["linked-dir/payload.txt"])
            self.assertEqual(found["files"], {})
            self.assertEqual(found["uncovered"][0]["reason"], "symlink-outside-root")
        finally:
            import shutil
            shutil.rmtree(outside)

    def test_nested_git_directory_is_excluded(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        (nested / "secret.txt").write_text("not scanned")
        self.assertEqual(cw.scan(self.root, ["."])["uncovered"], [{"path": "nested", "reason": "nested-repository-or-submodule"}])

    def test_root_git_metadata_is_skipped_without_excluding_project(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("synthetic metadata")
        found = cw.scan(self.root, ["."])
        self.assertIn("tracked.py", found["files"])
        self.assertFalse(any(path.startswith(".git/") for path in found["files"]))
        self.assertFalse(found["uncovered"])

    def test_missing_scope_is_unknown_not_empty_current(self):
        manifest = cw.scan(self.root, ["missing.txt"])
        st = self.root.stat()
        record = {"root": str(self.root.resolve()), "root_identity": {"device": st.st_dev, "inode": st.st_ino}, "includes": ["missing.txt"], "after_manifest": manifest, "junit": {"status": "not-configured"}}
        self.assertEqual(cw.current_comparison(record)[0], "unknown")

    def test_unreadable_directory_is_excluded(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        (blocked / "entry.txt").write_text("synthetic")
        try:
            blocked.chmod(0)
            result = cw.scan(self.root, ["."])
            self.assertNotIn("blocked/entry.txt", result["files"])
            self.assertIn({"path": "blocked", "reason": "unreadable-directory"}, result["uncovered"])
        finally:
            blocked.chmod(0o700)

    def test_comparison_states_current_changed_unknown(self):
        manifest = cw.scan(self.root, ["."])
        st = self.root.stat()
        record = {"root": str(self.root.resolve()), "root_identity": {"device": st.st_dev, "inode": st.st_ino}, "includes": ["."], "after_manifest": manifest, "junit": {"status": "not-configured"}}
        self.assertEqual(cw.current_comparison(record)[0], "current")
        (self.root / "tracked.py").write_text("changed")
        self.assertEqual(cw.current_comparison(record)[0], "changed")
        record["after_manifest"]["uncovered"] = [{"path": "x", "reason": "symlink"}]
        self.assertEqual(cw.current_comparison(record)[0], "changed")
        (self.root / "tracked.py").write_text("one")
        record["after_manifest"]["files"] = cw.scan(self.root, ["."])["files"]
        self.assertEqual(cw.current_comparison(record)[0], "unknown")

    def test_root_replacement_and_readability_loss_cannot_be_current(self):
        manifest = cw.scan(self.root, ["."])
        st = self.root.stat()
        record = {"root": str(self.root.resolve()), "root_identity": {"device": st.st_dev, "inode": st.st_ino}, "includes": ["."], "after_manifest": manifest, "junit": {"status": "not-configured"}}
        file = self.root / "tracked.py"
        file.chmod(0)
        self.assertNotEqual(cw.current_comparison(record)[0], "current")
        file.chmod(0o644)
        parked = self.root.parent / "parked-root"
        self.root.rename(parked)
        try:
            self.root.mkdir()
            (self.root / "tracked.py").write_text("one")
            self.assertEqual(cw.current_comparison(record)[0], "unknown")
        finally:
            import shutil
            shutil.rmtree(self.root)
            parked.rename(self.root)

    def test_symlink_target_change_stays_unknown(self):
        first = self.root.parent / "first-target"
        second = self.root.parent / "second-target"
        first.write_text("one")
        second.write_text("two")
        link = self.root / "escape"
        try:
            link.symlink_to(first)
            manifest = cw.scan(self.root, ["."])
            st = self.root.stat()
            record = {"root": str(self.root.resolve()), "root_identity": {"device": st.st_dev, "inode": st.st_ino}, "includes": ["."], "after_manifest": manifest, "junit": {"status": "not-configured"}}
            link.unlink()
            link.symlink_to(second)
            self.assertEqual(cw.current_comparison(record)[0], "unknown")
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            link.unlink(missing_ok=True)

    def test_file_swapped_for_symlink_after_lstat_is_not_opened(self):
        outside = self.root.parent / "outside-secret.txt"
        outside.write_text("synthetic outside target")
        original_open = os.open
        swapped = False

        def swap_before_open(path, *args, **kwargs):
            nonlocal swapped
            if path == "tracked.py" and "dir_fd" in kwargs and not swapped:
                (self.root / "tracked.py").unlink()
                (self.root / "tracked.py").symlink_to(outside)
                swapped = True
            return original_open(path, *args, **kwargs)

        try:
            with mock.patch.object(cw, "_safe_fd_support", return_value=True), mock.patch.object(cw.os, "open", side_effect=swap_before_open):
                result = cw.scan(self.root, ["."])
            self.assertTrue(swapped)
            self.assertNotIn("tracked.py", result["files"])
            self.assertIn({"path": "tracked.py", "reason": "unreadable-or-raced"}, result["uncovered"])
        finally:
            outside.unlink(missing_ok=True)

    def test_directory_swapped_for_symlink_before_traversal_is_not_followed(self):
        outside = self.root.parent / "outside-tree"
        outside.mkdir()
        (outside / "outside.txt").write_text("synthetic outside target")
        inner = self.root / "inner"
        inner.mkdir()
        (inner / "inside.txt").write_text("synthetic inside target")
        original_open = os.open
        swapped = False

        def swap_before_open(path, *args, **kwargs):
            nonlocal swapped
            if path == "inner" and "dir_fd" in kwargs and not swapped:
                inner.rename(self.root / "inner-parked")
                inner.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, *args, **kwargs)

        try:
            with mock.patch.object(cw, "_safe_fd_support", return_value=True), mock.patch.object(cw.os, "open", side_effect=swap_before_open):
                result = cw.scan(self.root, ["."])
            self.assertTrue(swapped)
            self.assertNotIn("inner/outside.txt", result["files"])
            self.assertIn({"path": "inner", "reason": "unreadable-or-raced-directory"}, result["uncovered"])
        finally:
            import shutil
            shutil.rmtree(outside)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "existing.txt").write_text("base")
        self.config = self.base / "config.json"
        self.report = self.base / "junit.xml"
        self.config.write_text(json.dumps({"root": "project", "include": ["."], "junit_report": "junit.xml"}))
        self.output, self.md = self.base / "receipt.json", self.base / "receipt.md"

    def tearDown(self):
        self.temp.cleanup()

    def run_py(self, code, name="safe-check", extra_args=None):
        return cw.run_command(self.config, name, self.output, self.md, [sys.executable, "-c", code, *(extra_args or [])])

    def test_unchanged_and_valid_junit_zero_tests_is_not_unknown(self):
        code = f"from pathlib import Path; Path({str(self.report)!r}).write_text('<testsuite tests=\"0\" failures=\"0\" errors=\"0\"/>')"
        self.assertEqual(self.run_py(code, extra_args=["PRIVATE_ARGUMENT_MARKER"]), 0)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["status"], "current")
        self.assertEqual(record["junit"]["status"], "valid")
        self.assertEqual(record["junit"]["tests"], 0)
        self.assertNotIn("PRIVATE_ARGUMENT_MARKER", self.output.read_text())
        self.assertIn("Tests: 0", self.md.read_text())

    def test_added_file_is_reported_and_later_comparison_is_changed(self):
        code = f"from pathlib import Path; Path({str(self.root / 'created.txt')!r}).write_text('demo')"
        self.run_py(code)
        rec = json.loads(self.output.read_text())
        self.assertEqual(rec["diff"]["added"], ["created.txt"])
        (self.root / "later.txt").write_text("later")
        self.assertEqual(cw.current_comparison(rec)[0], "changed")

    def test_persistent_mutation_during_command_is_changed(self):
        code = f"from pathlib import Path; Path({str(self.root / 'existing.txt')!r}).write_text('modified')"
        self.run_py(code)
        self.assertEqual(json.loads(self.output.read_text())["diff"]["changed"], ["existing.txt"])

    def test_invalid_missing_and_stale_reports_are_unknown_counts(self):
        self.report.unlink(missing_ok=True)
        code = f"from pathlib import Path; Path({str(self.report)!r}).write_text('not xml')"
        self.run_py(code)
        junit = json.loads(self.output.read_text())["junit"]
        self.assertEqual(junit["status"], "invalid")
        self.assertEqual(json.loads(self.output.read_text())["status"], "unknown")
        self.assertIsNone(junit["tests"])
        self.report.unlink(missing_ok=True)
        self.run_py("pass")
        junit = json.loads(self.output.read_text())["junit"]
        self.assertEqual(junit["status"], "missing")
        self.assertIsNone(junit["tests"])
        self.report.write_text('<testsuite tests="2"/>')
        os.utime(self.report, ns=(1, 1))
        self.run_py("pass")
        self.assertEqual(json.loads(self.output.read_text())["junit"]["status"], "stale-or-unattributed")
        self.assertEqual(json.loads(self.output.read_text())["status"], "unknown")

    def test_nonzero_exit_preserved_and_args_not_exported(self):
        self.assertEqual(self.run_py("raise SystemExit(7)", name="release-check", extra_args=["PRIVATE_ARGUMENT_MARKER"]), 7)
        record = json.loads(self.output.read_text())
        self.assertEqual(record["command"]["exit_code"], 7)
        raw = self.output.read_text()
        self.assertNotIn("raise SystemExit", raw)
        self.assertNotIn("PRIVATE_ARGUMENT_MARKER", raw)


if __name__ == "__main__":
    unittest.main()
