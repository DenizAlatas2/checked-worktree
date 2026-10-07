# checked-worktree

`checked-worktree` records SHA-256 manifests for an explicitly selected file scope before and after one command, then lets you compare the saved after-state with the files later. It includes untracked files because it scans the filesystem directly; Git status and index state are not consulted or changed.

## Quick start

Requires Python 3.9+ and no third-party packages.

```sh
python3 -B checked_worktree.py run \
  --config checked-worktree.example.json \
  --name "unit tests" \
  --json /tmp/unit-tests.json \
  --markdown /tmp/unit-tests.md \
  -- python3 -B -m unittest discover -s tests -v

python3 -B checked_worktree.py compare /tmp/unit-tests.json
python3 -B demo.py
```

The command after `--` is an argument list passed directly to `subprocess.run` with shell interpretation disabled. The explicit `-s tests` is needed because the test directory is not a Python package; plain `unittest discover` from the repository root can report success after running zero tests. Confirm the test output says `Ran N tests` with `N > 0`; `-B` prevents bytecode cache files from changing the selected scope. This quick start deliberately has no JUnit report, so its receipt says `JUnit: not-configured` and records unknown test counts. The separate synthetic demo shows a valid JUnit report and a later changed comparison. Select the exact scope in the JSON configuration, and keep receipt files outside it. When `junit_report` is configured, the report is counted only when it exists and changes after this invocation starts; an unchanged old report is marked `stale-or-unattributed`. Invalid, missing, stale, or unconfigured reports have unknown counts, distinct from a valid report with zero tests.

```json
{
  "root": ".",
  "include": ["src", "tests", "pyproject.toml"],
  "junit_report": "/tmp/unit-tests.xml"
}
```

Paths in `include` are relative to the config file's directory (as is a relative report path). Directory selection includes regular files recursively. Symlinks, nested repositories/submodules, unreadable files/directories, special files, and missing selections are listed under `uncovered`, not silently hashed. `.git` metadata is skipped. `current` requires the same root directory identity, no uncovered entries, and—when configured—a valid report attributed to this invocation; missing, stale, invalid, or unreadable JUnit data yields `unknown`. `compare` is read-only with respect to the project and reports `current`, `changed`, or `unknown`; any detected file changes take precedence over `unknown`.

## Evidence and limits

JSON receipts contain file paths and hashes, the user-chosen check label, timestamps, the command exit code, manifest differences, coverage exclusions, and JUnit counts/status. They do **not** contain command arguments, environment variables, file contents, or raw output. Choose a non-sensitive check label. The command itself can still print output to its own terminal. Receipt paths and scope names may reveal project structure, so review them before sharing.

On systems with descriptor-relative `O_NOFOLLOW` support, files and directories are opened relative to pinned directory descriptors and checked against the observed device/inode before hashing. Platforms without those primitives report the scope as unknown. This reduces path-swap races but is not a race-free monitor: equal before/after hashes cannot rule out a transient change during the command or a file added and removed between directory observations. A command may modify files outside the configured scope. The record is not signed or tamper-proof, and a JUnit file changed during execution cannot be conclusively attributed to the command if another process can write it. It adds no service, hook, agent, model, key, or Git operation.

## Why this scope

The working name was `DoneReceipt`; the descriptive repository name is `checked-worktree`. General-purpose test receipts are not new. This project focuses on whether a configured on-disk file state remained the same during an observed run and whether that observed state is still current later. It is intentionally narrower than pytest's JUnit output, agent-session/checkpoint tools such as [Entire CLI](https://github.com/entireio/cli), or signed command evidence such as [Treeship](https://github.com/zerkerlabs/treeship). It does not claim absolute novelty, name clearance, or security attestation.

## Development

```sh
python3 -B -m unittest discover -s tests -v
```

MIT licensed; see [LICENSE](LICENSE). All examples and demo files are synthetic.
