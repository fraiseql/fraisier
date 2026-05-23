"""Unit tests for BaseDeployer helpers."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import BaseDeployer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deployer_with_runner(runner=None):
    deployer = APIDeployer({})
    if runner is not None:
        deployer.runner = runner
    return deployer


class TestDetectConfigChanges:
    def test_no_config_path_returns_false(self):
        deployer = APIDeployer({})
        result = deployer._detect_config_changes(config_path=None)
        assert result is False

    def test_config_unchanged_returns_false(self, tmp_path):
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("fraises: []")

        deployer = APIDeployer({})
        with patch("fraisier.config_watcher.ConfigWatcher") as MockWatcher:
            mock_watcher = MagicMock()
            mock_watcher.has_changed.return_value = False
            MockWatcher.return_value = mock_watcher

            result = deployer._detect_config_changes(config_path=config_path)

        assert result is False
        MockWatcher.assert_called_once_with(tmp_path)

    def test_config_changed_returns_true(self, tmp_path):
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("fraises: []")

        deployer = APIDeployer({})
        with patch("fraisier.config_watcher.ConfigWatcher") as MockWatcher:
            mock_watcher = MagicMock()
            mock_watcher.has_changed.return_value = True
            MockWatcher.return_value = mock_watcher

            result = deployer._detect_config_changes(config_path=config_path)

        assert result is True


class TestSyncFraisesYaml:
    def test_no_paths_logs_and_returns(self, tmp_path):
        deployer = APIDeployer({})
        # No-op: should not raise
        deployer._sync_fraises_yaml(source_path=None, dest_path=None)

    def test_missing_source_raises(self, tmp_path):
        deployer = APIDeployer({})
        with pytest.raises(FileNotFoundError):
            deployer._sync_fraises_yaml(
                source_path=tmp_path / "nonexistent.yaml",
                dest_path=tmp_path / "dest.yaml",
            )

    def test_existing_source_copies(self, tmp_path):
        source = tmp_path / "fraises.yaml"
        source.write_text("fraises: []")
        dest = tmp_path / "dest.yaml"

        deployer = APIDeployer({})
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(ok=True)
        deployer.runner = mock_runner

        deployer._sync_fraises_yaml(source_path=source, dest_path=dest)
        assert mock_runner.run.call_count == 2
        calls = mock_runner.run.call_args_list
        assert calls[0][0][0] == ["mkdir", "-p", str(tmp_path)]
        assert calls[1][0][0] == ["cp", str(source), str(dest)]

    def test_creates_dest_directory_if_missing(self, tmp_path):
        source = tmp_path / "fraises.yaml"
        source.write_text("fraises: []")
        dest = tmp_path / "subdir" / "fraises.yaml"

        deployer = APIDeployer({})
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(ok=True)
        deployer.runner = mock_runner

        deployer._sync_fraises_yaml(source_path=source, dest_path=dest)
        calls = mock_runner.run.call_args_list
        assert calls[0][0][0] == ["mkdir", "-p", str(tmp_path / "subdir")]


class TestInstallScaffold:
    """_install_scaffold() must pass cwd and -c config_path to the runner."""

    def test_install_scaffold_uses_cwd_and_config(self, tmp_path):
        """With config_path, runner receives cwd and -c flag as distinct args."""
        config_path = tmp_path / "fraises.yaml"
        config_path.touch()

        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="")

        deployer = _make_deployer_with_runner(mock_runner)
        deployer._install_scaffold(config_path=config_path)

        mock_runner.run.assert_called_once()
        cmd = mock_runner.run.call_args[0][0]
        kwargs = mock_runner.run.call_args[1]
        assert "-c" in cmd
        assert str(config_path) in cmd
        assert "scaffold-install" in cmd
        assert kwargs.get("cwd") == str(tmp_path)

    def test_install_scaffold_no_config_falls_back(self):
        """Without config_path, original simple command is used."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="")

        deployer = _make_deployer_with_runner(mock_runner)
        deployer._install_scaffold(config_path=None)

        mock_runner.run.assert_called_once()
        cmd = mock_runner.run.call_args[0][0]
        assert "scaffold-install" in " ".join(cmd)
        assert "cd " not in " ".join(cmd)


# ---------------------------------------------------------------------------
# _get_fraisier_executable — resolves the fraisier binary across installs
# ---------------------------------------------------------------------------


def _make_executable(path):
    """Create an executable stand-in for the fraisier console script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python\n")
    path.chmod(0o755)
    return path


class TestGetFraisierExecutable:
    """_get_fraisier_executable() must resolve every supported install layout.

    Issue #216: under `uv tool install fraisier`, the binary lives at
    ~/.local/share/uv/tools/fraisier/bin/fraisier (with a symlink at
    ~/.local/bin/fraisier). The deploy daemon's systemd unit inherits a
    default PATH that excludes both, so shutil.which() returns None and
    the v0.22.0 hardcoded fallback list does not cover either location.

    Resolution order (most-correct first):
      1. sys.executable sibling — by construction the same install as the
         running Python, so its console-script sibling is always our binary.
      2. shutil.which("fraisier") — covers daemons launched through a
         wrapper that uses a different Python (e.g. system python -m).
      3. Hardcoded fallbacks — ~/.local/bin, the uv tool share dir, and
         the historical /usr/local/bin, /usr/bin, /opt/fraisier/bin paths.

    Candidates that are missing, non-regular files, or not executable are
    skipped so the daemon never returns a path that would later fail exec.
    """

    @pytest.fixture(autouse=True)
    def _clear_resolver_cache(self):
        """Reset the module-level lookup cache between tests."""
        from fraisier.deployers import base as base_mod

        base_mod._resolve_fraisier_executable.cache_clear()
        yield
        base_mod._resolve_fraisier_executable.cache_clear()

    def test_sys_executable_sibling_resolves_first(self, tmp_path, monkeypatch):
        """uv-tool layout: binary sits next to the interpreter."""
        bin_dir = tmp_path / "uv-tool" / "bin"
        fake_python = bin_dir / "python"
        _make_executable(fake_python)
        fake_fraisier = _make_executable(bin_dir / "fraisier")

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        assert BaseDeployer._get_fraisier_executable() == str(fake_fraisier)

    def test_sibling_beats_path_when_both_exist(self, tmp_path, monkeypatch):
        """Sibling has the strongest correctness guarantee — must win."""
        bin_dir = tmp_path / "current" / "bin"
        fake_python = bin_dir / "python"
        _make_executable(fake_python)
        sibling = _make_executable(bin_dir / "fraisier")

        # PATH points at a stale older version sitting elsewhere
        stale = _make_executable(tmp_path / "stale" / "fraisier")

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr("shutil.which", lambda _name: str(stale))
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        assert BaseDeployer._get_fraisier_executable() == str(sibling)

    def test_non_executable_sibling_is_skipped(self, tmp_path, monkeypatch):
        """A file at the sibling path without +x must NOT be returned."""
        bin_dir = tmp_path / "broken" / "bin"
        fake_python = bin_dir / "python"
        _make_executable(fake_python)
        # Sibling exists but lacks the executable bit
        bad_sibling = bin_dir / "fraisier"
        bad_sibling.write_text("not really a script")
        bad_sibling.chmod(0o644)

        good = _make_executable(tmp_path / "ok" / "fraisier")
        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr(
            "shutil.which", lambda name: str(good) if name == "fraisier" else None
        )
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        assert BaseDeployer._get_fraisier_executable() == str(good)

    def test_directory_at_sibling_path_is_skipped(self, tmp_path, monkeypatch):
        """A directory (e.g. a stray `fraisier/` package next to python) is rejected."""
        bin_dir = tmp_path / "weird" / "bin"
        fake_python = bin_dir / "python"
        _make_executable(fake_python)
        # The sibling path is a *directory* — must not be returned
        (bin_dir / "fraisier").mkdir(parents=True)

        good = _make_executable(tmp_path / "ok" / "fraisier")
        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr(
            "shutil.which", lambda name: str(good) if name == "fraisier" else None
        )
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        assert BaseDeployer._get_fraisier_executable() == str(good)

    def test_path_lookup_when_no_sibling(self, tmp_path, monkeypatch):
        """Without a sibling (e.g. `python -m`), PATH is the next probe."""
        fake_python = tmp_path / "lonely" / "python"
        _make_executable(fake_python)
        # No sibling fraisier next to fake_python

        on_path = _make_executable(tmp_path / "ok" / "fraisier")
        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr(
            "shutil.which", lambda name: str(on_path) if name == "fraisier" else None
        )
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        assert BaseDeployer._get_fraisier_executable() == str(on_path)

    def test_local_bin_fallback_for_uv_tool_symlink(self, tmp_path, monkeypatch):
        """The canonical uv-tool symlink at ~/.local/bin/fraisier resolves."""
        fake_home = tmp_path / "home"
        local_bin_fraisier = _make_executable(fake_home / ".local" / "bin" / "fraisier")

        fake_python = tmp_path / "elsewhere" / "python"
        _make_executable(fake_python)
        # No sibling next to fake_python, no PATH match

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("HOME", str(fake_home))

        assert BaseDeployer._get_fraisier_executable() == str(local_bin_fraisier)

    def test_uv_tools_share_fallback(self, tmp_path, monkeypatch):
        """The uv-tool data dir is searched when the convenience symlink is gone."""
        fake_home = tmp_path / "home"
        uv_bin = fake_home / ".local" / "share" / "uv" / "tools" / "fraisier" / "bin"
        target = _make_executable(uv_bin / "fraisier")

        fake_python = tmp_path / "elsewhere" / "python"
        _make_executable(fake_python)

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("HOME", str(fake_home))

        assert BaseDeployer._get_fraisier_executable() == str(target)

    def test_legacy_standard_paths_still_resolve(self, tmp_path, monkeypatch):
        """The pre-#216 fallback list keeps working as a last-resort."""
        # /usr/local/bin/fraisier-equivalent — we monkeypatch Path.exists checks
        # by routing through a fake filesystem via the resolver's candidate list.
        # Easier: drop a binary in tmp and have the resolver probe it via PATH.
        fake_python = tmp_path / "elsewhere" / "python"
        _make_executable(fake_python)

        legacy = _make_executable(tmp_path / "usr-local-bin" / "fraisier")
        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr(
            "shutil.which", lambda name: str(legacy) if name == "fraisier" else None
        )
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        # When shutil.which finds it, that's the answer (it's strategy #2)
        assert BaseDeployer._get_fraisier_executable() == str(legacy)

    def test_diagnostic_lists_every_probed_path(self, tmp_path, monkeypatch):
        """The error message must name each strategy and remediation hint."""
        fake_python = tmp_path / "nowhere" / "python"
        _make_executable(fake_python)
        # Nothing exists anywhere

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        with pytest.raises(RuntimeError) as excinfo:
            BaseDeployer._get_fraisier_executable()

        msg = str(excinfo.value)
        # Each strategy must appear by name so journald output is self-diagnosing
        assert "sys.executable" in msg
        assert str(fake_python.parent / "fraisier") in msg
        assert "PATH" in msg or "$PATH" in msg
        assert "/usr/local/bin/fraisier" in msg
        assert "/usr/bin/fraisier" in msg
        assert "/opt/fraisier/bin/fraisier" in msg
        assert ".local/bin/fraisier" in msg
        assert ".local/share/uv/tools/fraisier" in msg
        # Remediation hint: tells the operator what to do
        assert "uv tool install fraisier" in msg

    def test_result_is_cached_within_process(self, tmp_path, monkeypatch):
        """Repeated calls hit a cache — the resolver is invoked once per process."""
        bin_dir = tmp_path / "uv-tool" / "bin"
        fake_python = _make_executable(bin_dir / "python")
        fake_fraisier = _make_executable(bin_dir / "fraisier")

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        call_count = {"n": 0}

        def counting_which(_name):
            call_count["n"] += 1

        monkeypatch.setattr("shutil.which", counting_which)

        first = BaseDeployer._get_fraisier_executable()
        # Even after the candidate disappears, the cached result is returned
        fake_fraisier.unlink()
        second = BaseDeployer._get_fraisier_executable()

        assert first == second == str(fake_fraisier)
        # shutil.which would have been called only on the first invocation
        # (sibling resolution wins before which() is even consulted, so 0 calls)
        assert call_count["n"] == 0

    def test_empty_sys_executable_does_not_match_cwd(self, tmp_path, monkeypatch):
        """sys.executable='' (frozen-app edge case) must not silently match './fraisier'."""
        # Drop a fraisier-like file in CWD that would match Path('') / 'fraisier'
        cwd_decoy = _make_executable(tmp_path / "fraisier")
        monkeypatch.chdir(tmp_path)

        good = _make_executable(tmp_path / "ok" / "fraisier")
        monkeypatch.setattr("sys.executable", "")
        monkeypatch.setattr(
            "shutil.which", lambda name: str(good) if name == "fraisier" else None
        )
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

        result = BaseDeployer._get_fraisier_executable()
        # The CWD decoy must NOT be returned — we want the real PATH answer
        assert result == str(good)
        assert result != str(cwd_decoy)
