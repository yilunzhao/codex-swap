"""Classifying running Codex processes from a process listing."""

from __future__ import annotations

import os
import subprocess

import pytest

from codex_swap import process_detection
from codex_swap.process_detection import CodexProcess, ProcessScan, _classify, scan

# A real `ps -axo pid=,ppid=,command=` sample, trimmed to the shapes that a
# naive matcher gets wrong on a live machine:
#   735  a Node broker that is not Codex at all
#   741  the npm shim -- argv[0] is `node`, the real binary is argv[1]
#   742  the native binary the shim spawned; same session, must not double-count
#  1405  a sandbox helper: real, but holds no credentials
#  6119  `-c` takes a value, which must not be mistaken for a sub-command
#  9000  an unrelated app whose name merely starts with "codex"
PS_SAMPLE = """\
  735     1 /usr/local/bin/node /path/to/app-server-broker.mjs serve --endpoint unix:/tmp/x
  741   735 node /Users/me/.nvm/versions/node/v22.12.0/bin/codex app-server
  742   741 /Users/me/.nvm/.../codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex app-server
 1405   742 /Users/me/.nvm/.../vendor/aarch64-apple-darwin/bin/codex-code-mode-host
 6117     1 /usr/local/bin/codex
 6118     1 /usr/local/bin/codex exec --full-auto "do the thing"
 6119     1 /usr/local/bin/codex -c model=o3
 7792     1 /Users/me/.vscode/extensions/openai.chatgpt/bin/codex-linux-sandbox
 8000     1 /usr/bin/python3 -m pytest
 9000     1 /Applications/Codexterous.app/Contents/MacOS/Codexterous
"""


@pytest.fixture
def fake_ps(monkeypatch):
    def _install(stdout: str, *, fail: bool = False, timeout: bool = False):
        def fake_run(command, **kwargs):
            if fail:
                raise OSError("ps unavailable")
            if timeout:
                raise subprocess.TimeoutExpired(command, 5)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

    return _install


class TestDetection:
    def test_finds_the_core_binary_behind_a_node_shim(self, fake_ps):
        """The npm distribution shows up as `node /path/bin/codex ...`."""
        fake_ps("  741   735 node /Users/me/.nvm/versions/node/v22/bin/codex app-server\n")
        result = scan(exclude_self=False)
        assert [p.pid for p in result.holders] == [741]
        assert result.holders[0].role == "app-server"

    def test_one_session_behind_a_shim_counts_once(self, fake_ps):
        """The shim spawns the native binary; both are the same session."""
        fake_ps(PS_SAMPLE)
        holders = scan(exclude_self=False).holders
        assert 742 not in {p.pid for p in holders}, "native child double-counted"
        assert 741 in {p.pid for p in holders}

    def test_counts_only_credential_holding_processes(self, fake_ps):
        fake_ps(PS_SAMPLE)
        result = scan(exclude_self=False)
        assert {p.pid for p in result.holders} == {741, 6117, 6118, 6119}

    def test_helpers_are_counted_separately(self, fake_ps):
        fake_ps(PS_SAMPLE)
        result = scan(exclude_self=False)
        assert {p.pid for p in result.helpers} == {1405, 7792}
        assert all(p.name in process_detection.HELPER_NAMES for p in result.helpers)

    @pytest.mark.parametrize(
        ("pid", "why"),
        [
            (735, "a node broker that is not codex"),
            (8000, "pytest under a python launcher"),
            (9000, "an unrelated app whose name starts with codex"),
        ],
    )
    def test_unrelated_processes_are_ignored(self, fake_ps, pid, why):
        fake_ps(PS_SAMPLE)
        result = scan(exclude_self=False)
        seen = {p.pid for p in result.holders} | {p.pid for p in result.helpers}
        assert pid not in seen, why

    def test_codex_swap_itself_is_not_counted(self, fake_ps):
        """Otherwise every run would warn about itself."""
        fake_ps("  100     1 /usr/local/bin/codex-swap switch\n")
        result = scan(exclude_self=False)
        assert result.holders == [] and result.helpers == []


class TestRoles:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["/usr/local/bin/codex"], "tui"),
            (["/usr/local/bin/codex", "app-server"], "app-server"),
            (["/usr/local/bin/codex", "exec", "--full-auto"], "exec"),
            (["/usr/local/bin/codex", "resume", "--last"], "resume"),
            # `-c` takes a value that does not start with a dash.
            (["/usr/local/bin/codex", "-c", "model=o3"], "tui"),
            # A bare prompt is an interactive session, not a sub-command.
            (["/usr/local/bin/codex", "fix", "the", "bug"], "tui"),
            (["/usr/local/bin/codex", "--enable", "foo", "exec"], "exec"),
        ],
    )
    def test_role_extraction(self, argv, expected):
        probe = ProcessScan()
        _classify(1, argv, probe)
        assert probe.holders[0].role == expected

    def test_summary_groups_by_label(self, fake_ps):
        fake_ps(PS_SAMPLE)
        summary = scan(exclude_self=False).summary()
        assert "1 x codex app-server" in summary
        assert "codex tui" in summary
        assert "codex exec" in summary

    def test_label_combines_name_and_role(self):
        assert CodexProcess(1, "codex", "app-server").label == "codex app-server"
        assert CodexProcess(1, "codex-code-mode-host").label == "codex-code-mode-host"


class TestRobustness:
    def test_scan_excludes_the_current_process(self, fake_ps):
        fake_ps(f"{os.getpid()} 1 /usr/local/bin/codex exec\n  999 1 /usr/local/bin/codex\n")
        assert {p.pid for p in scan(exclude_self=True).holders} == {999}

    def test_unavailable_listing_is_reported_not_raised(self, fake_ps):
        fake_ps("", fail=True)
        result = scan(exclude_self=False)
        assert result.unavailable is True
        assert result.holders == []
        assert bool(result) is False

    def test_timeout_is_treated_as_unavailable(self, fake_ps):
        fake_ps("", timeout=True)
        assert scan(exclude_self=False).unavailable is True

    def test_empty_listing(self, fake_ps):
        fake_ps("")
        result = scan(exclude_self=False)
        assert result.holders == [] and result.helpers == []
        assert result.unavailable is False

    def test_malformed_lines_are_skipped(self, fake_ps):
        fake_ps("\n\ngarbage\n  abc 1 /usr/local/bin/codex\n  12 \n  13 x /usr/bin/codex\n")
        result = scan(exclude_self=False)
        # `13 x ...` has a non-numeric ppid but a valid pid: still a real process.
        assert {p.pid for p in result.holders} == {13}

    def test_scan_is_truthy_only_when_holders_exist(self, fake_ps):
        fake_ps("  12 1 /usr/local/bin/codex\n")
        assert bool(scan(exclude_self=False)) is True
        fake_ps("  12 1 /usr/local/bin/codex-code-mode-host\n")
        assert bool(scan(exclude_self=False)) is False

    def test_windows_exe_suffix_is_stripped(self):
        probe = ProcessScan()
        _classify(42, ["codex.exe", "app-server"], probe)
        assert probe.holders[0].role == "app-server"

    def test_windows_path_separators_are_handled(self):
        probe = ProcessScan()
        _classify(42, ["C:\\Program Files\\codex\\codex.exe", "app-server"], probe)
        assert probe.holders and probe.holders[0].name == "codex"

    def test_empty_argv_is_ignored(self):
        probe = ProcessScan()
        _classify(1, [], probe)
        assert probe.holders == [] and probe.helpers == []

    def test_deep_argv_matches_are_not_trusted(self):
        """A path passed as an argument must not look like the binary."""
        probe = ProcessScan()
        _classify(1, ["/usr/bin/vim", "--", "/some/dir/codex"], probe)
        assert probe.holders == []
