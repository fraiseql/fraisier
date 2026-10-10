"""Doctor's view of the root-owned fraisier install (#433, D5).

Two copies of fraisier now live on a host: the deploy user's, which the
webhook upgrades itself, and the root copy the root helpers run, which only an
operator upgrades. Their versions drifting apart is a normal, reported state.
And ``sudo fraisier`` must run the root copy: an operator's scaffold-install
under sudo is how root content gets written.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fraisier import doctor
from tests.test_doctor_root_exec_trust import FakeOwners


def _install(venv: Path, version: str) -> Path:
    info = (
        venv / "lib" / "python3.14" / "site-packages" / f"fraisier-{version}.dist-info"
    )
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: fraisier\nVersion: {version}\n"
    )
    return venv


@pytest.fixture
def venvs(tmp_path, monkeypatch):
    root, deploy = tmp_path / "root" / "fraisier", tmp_path / "deploy" / "fraisier"
    monkeypatch.setattr(doctor, "ROOT_TOOL_VENV", root)
    monkeypatch.setattr(doctor, "_deploy_tool_venv", lambda _config: deploy)
    return root, deploy


def _skew():
    check = doctor.DOCTOR_CHECKS["root_helper_version_skew"]
    return check.fn(SimpleNamespace())  # ty: ignore[invalid-argument-type]


class TestVersionSkew:
    def test_same_version_passes(self, venvs):
        _install(venvs[0], "0.90.0")
        _install(venvs[1], "0.90.0")
        result = _skew()
        assert result.status == "pass"
        assert "0.90.0" in result.detail

    def test_different_versions_warn_and_name_the_upgrade(self, venvs):
        _install(venvs[0], "0.89.0")
        _install(venvs[1], "0.90.0")
        result = _skew()
        assert result.status == "warn"
        assert "root copy 0.89.0" in result.detail
        assert "deploy copy 0.90.0" in result.detail
        assert "sudo fraisier-root-upgrade 0.90.0" in (result.fix_hint or "")

    def test_no_root_install_skips(self, venvs):
        _install(venvs[1], "0.90.0")
        assert _skew().status == "skip"

    def test_no_deploy_install_skips(self, venvs):
        _install(venvs[0], "0.90.0")
        assert _skew().status == "skip"

    def test_the_deploy_venv_is_the_deploy_users_uv_tool_dir(self):
        config = SimpleNamespace(scaffold=SimpleNamespace(deploy_user="deployer"))
        venv = doctor._deploy_tool_venv(config)  # ty: ignore[invalid-argument-type]
        assert venv == Path("/home/deployer/.local/share/uv/tools/fraisier")


@pytest.fixture
def command(tmp_path, monkeypatch):
    """A root install with its link, and a sudoers that names a secure_path."""
    root = tmp_path / "fraisier-root"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    target = bin_dir / "fraisier"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link_dir = tmp_path / "usr-local-bin"
    link_dir.mkdir()
    link = link_dir / "fraisier"
    link.symlink_to(target)
    sudoers = tmp_path / "sudoers"
    sudoers.write_text(f'Defaults\tsecure_path="{link_dir}:/usr/bin:/bin"\n')
    monkeypatch.setattr(doctor, "ROOT_DIR", str(root))
    monkeypatch.setattr(doctor, "ROOT_LINK", str(link))
    monkeypatch.setattr(doctor, "_sudoers_files", lambda: [sudoers])
    owners = FakeOwners()
    monkeypatch.setattr(doctor, "_path_stat", owners.stat)
    return SimpleNamespace(
        root=root,
        link=link,
        link_dir=link_dir,
        sudoers=sudoers,
        owners=owners,
        tmp=tmp_path,
    )


def _command():
    return doctor.DOCTOR_CHECKS["root_fraisier_command"].fn(None)


class TestSudoFraisierRunsTheRootCopy:
    def test_a_root_owned_link_first_on_secure_path_passes(self, command):
        result = _command()
        assert result.status == "pass", result.detail

    def test_no_link_warns(self, command):
        command.link.unlink()
        result = _command()
        assert result.status == "warn"
        assert str(command.link) in result.detail

    def test_a_link_to_another_copy_warns(self, command):
        other = command.tmp / "deploy" / "fraisier"
        other.parent.mkdir()
        other.write_text("")
        command.link.unlink()
        command.link.symlink_to(other)
        result = _command()
        assert result.status == "warn"
        assert str(other) in result.detail

    def test_a_link_someone_else_can_change_warns(self, command):
        command.owners.deploy_owns(command.link_dir)
        result = _command()
        assert result.status == "warn"
        assert str(command.link_dir) in result.detail

    def test_a_deploy_owned_dir_earlier_on_secure_path_warns(self, command):
        early = command.tmp / "home-deploy-bin"
        early.mkdir()
        command.owners.deploy_owns(early)
        command.sudoers.write_text(
            f"Defaults secure_path={early}:{command.link_dir}:/usr/bin\n"
        )
        result = _command()
        assert result.status == "warn"
        assert f"{early} comes before {command.link_dir}" in result.detail

    def test_a_secure_path_without_the_link_dir_warns(self, command):
        command.sudoers.write_text("Defaults secure_path=/usr/bin:/bin\n")
        result = _command()
        assert result.status == "warn"
        assert "does not include" in result.detail

    def test_the_last_secure_path_wins(self, command):
        command.sudoers.write_text(
            "Defaults secure_path=/usr/bin:/bin\n"
            f'Defaults secure_path="{command.link_dir}:/usr/bin"\n'
        )
        assert _command().status == "pass"

    def test_an_unreadable_sudoers_still_judges_the_link(self, command, monkeypatch):
        monkeypatch.setattr(doctor, "_sudoers_files", lambda: [command.tmp / "nope"])
        result = _command()
        assert result.status == "pass"
        assert "secure_path" in result.detail

    def test_no_root_install_skips(self, command):
        import shutil

        shutil.rmtree(command.root)
        command.link.unlink()
        assert _command().status == "skip"

    def test_a_regular_file_in_place_of_the_link_warns_as_such(self, command):
        command.link.unlink()
        command.link.write_text("#!/bin/sh\n")
        result = _command()
        assert result.status == "warn"
        assert result.detail == f"{command.link} is not a link to the root copy"
