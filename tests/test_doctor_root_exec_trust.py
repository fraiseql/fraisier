"""Doctor reports a root command whose code the deploy user can change (#433).

A unit that runs as root (no ``User=``, ``User=root`` or ``User=0``), or any
``+``/``!`` command line in a unit that sets ``User=``, executes its binary,
the binary's shebang interpreter, the venv that interpreter belongs to, and the
base Python named by that venv's ``pyvenv.cfg``. If any of those, or any
directory above them, is owned by someone other than root or writable by
someone other than root, that someone can run code as root.

What counts is the **effective** unit, drop-ins included. On a live host it is
read from ``systemctl show``. The file reader is the fallback and the test
seam. The suite does not run as root, so ownership is faked through
``doctor._path_stat``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fraisier import doctor

if TYPE_CHECKING:
    from fraisier.doctor import CheckResult

CHECK = "root_unit_exec_trust"
DEPLOY_UID = 4242


class FakeOwners:
    """Every path is root-owned and 0755-ish unless a test says otherwise."""

    def __init__(self) -> None:
        self.owned: set[Path] = set()
        self.modes: dict[Path, int] = {}
        self.gids: dict[Path, int] = {}

    def deploy_owns(self, *paths: Path) -> None:
        self.owned.update(paths)

    def stat(self, path: str) -> tuple[int, int, int]:
        real = os.lstat(path)
        p = Path(path)
        # A symlink keeps its real 0777: only its directory decides who can
        # re-point it, and the check must know that.
        default = real.st_mode if stat.S_ISLNK(real.st_mode) else real.st_mode & ~0o022
        mode = self.modes.get(p, default)
        uid = DEPLOY_UID if p in self.owned else 0
        return uid, self.gids.get(p, DEPLOY_UID), mode


@pytest.fixture
def owners(monkeypatch: pytest.MonkeyPatch) -> FakeOwners:
    fake = FakeOwners()
    monkeypatch.setattr(doctor, "_path_stat", fake.stat)
    return fake


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", units)
    monkeypatch.setattr(doctor, "SYSTEMD_DROPIN_ROOTS", (units,))
    monkeypatch.setattr(doctor, "_systemctl_show", lambda _names: {})
    return units


def _venv(root: Path, *, home: Path | None = None) -> Path:
    """A tool venv at *root*; returns its ``bin/fraisier`` console script."""
    (root / "bin").mkdir(parents=True)
    site = root / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / "fraisier.pth").write_text("")
    base = home or root.parent / "base-python" / "bin"
    base.mkdir(parents=True, exist_ok=True)
    (root / "pyvenv.cfg").write_text(f"home = {base}\nversion_info = 3.14.5\n")
    python = root / "bin" / "python"
    python.write_text("")
    python.chmod(0o755)
    script = root / "bin" / "fraisier"
    script.write_text(f"#!{python}\nimport fraisier\n")
    script.chmod(0o755)
    return script


def _unit(unit_dir: Path, name: str, *lines: str) -> Path:
    path = unit_dir / name
    path.write_text(
        "\n".join(["[Unit]", f"Description={name}", "", "[Service]", *lines]) + "\n"
    )
    return path


def _dropin(root: Path, dirname: str, conf: str, *lines: str) -> Path:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    path = d / conf
    path.write_text("\n".join(["[Service]", *lines]) + "\n")
    return path


def _run() -> CheckResult:
    return doctor.DOCTOR_CHECKS[CHECK].fn(None)


def test_registered() -> None:
    assert CHECK in doctor.DOCTOR_CHECKS


def test_since_release_d_it_fails() -> None:
    """#433's fix has shipped: root running changeable code is a failure now."""
    assert doctor.ROOT_EXEC_TRUST_STATUS == "fail"


def test_a_venv_interpreter_run_with_dash_m_has_its_venv_judged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """The root helpers run ``<venv>/bin/python -I -m fraisier.x`` (#433).

    ``bin/python`` is a link to the base interpreter, so resolving it lands
    outside the venv. The venv holding the imported code must still be walked.
    """
    venv = tmp_path / "root-venv"
    _venv(venv)
    base = tmp_path / "base-python" / "bin" / "python3.14"
    base.write_text("")
    (venv / "bin" / "python").unlink()
    (venv / "bin" / "python").symlink_to(base)
    pth = venv / "lib" / "python3.14" / "site-packages" / "fraisier.pth"
    owners.deploy_owns(pth)
    _unit(unit_dir, "h.service", f"ExecStart={venv}/bin/python -I -m fraisier.helper")
    result = _run()
    assert result.status == "fail"
    assert str(pth) in result.detail


def test_the_fix_hint_names_the_operator_steps(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.deploy_owns(venv)
    hint = _run().fix_hint or ""
    assert "fraisier-root-upgrade" in hint
    assert "sudo fraisier scaffold-install" in hint


# --- what is judged ---------------------------------------------------------


def test_a_root_unit_on_a_root_owned_chain_passes(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    _unit(unit_dir, "h.service", f"ExecStart={_venv(tmp_path / 'root-venv')} helper")
    result = _run()
    assert result.status == "pass", result.detail


def test_a_deploy_owned_venv_is_flagged_by_unit_path_and_owner(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.deploy_owns(venv)
    result = _run()
    assert result.status == doctor.ROOT_EXEC_TRUST_STATUS
    assert "h.service" in result.detail
    assert str(venv) in result.detail
    assert str(DEPLOY_UID) in result.detail
    assert result.fix_hint is not None
    assert "#433" in result.fix_hint


def test_a_deploy_owned_module_inside_a_root_owned_venv_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "root-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    pth = venv / "lib" / "python3.14" / "site-packages" / "fraisier.pth"
    owners.deploy_owns(pth)
    result = _run()
    assert result.status == doctor.ROOT_EXEC_TRUST_STATUS
    assert str(pth) in result.detail


def test_a_root_owned_venv_on_a_deploy_owned_base_python_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """uv's managed Python lives under ``~/.local/share/uv/python`` by default."""
    home = tmp_path / "uv-python" / "cpython-3.14.5" / "bin"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(tmp_path / 'v', home=home)} x")
    owners.deploy_owns(tmp_path / "uv-python")
    result = _run()
    assert result.status == "fail"
    assert str(tmp_path / "uv-python") in result.detail


def test_a_root_owned_script_on_a_deploy_owned_interpreter_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    interp_dir = tmp_path / "deploy-bin"
    interp_dir.mkdir()
    interp = interp_dir / "python3"
    interp.write_text("")
    script = tmp_path / "root-bin" / "tool"
    script.parent.mkdir()
    script.write_text(f"#!{interp} -I\nprint()\n")
    _unit(unit_dir, "h.service", f"ExecStart={script}")
    owners.deploy_owns(interp_dir)
    result = _run()
    assert result.status == "fail"
    assert str(interp_dir) in result.detail


def test_a_group_writable_parent_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "root-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.modes[tmp_path] = stat.S_IFDIR | 0o775
    result = _run()
    assert result.status == "fail"
    assert f"{tmp_path} (group-writable" in result.detail


def test_a_world_writable_parent_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "root-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.modes[venv / "bin"] = stat.S_IFDIR | 0o757
    result = _run()
    assert result.status == "fail"
    assert f"{venv / 'bin'} (world-writable" in result.detail


def test_a_sticky_root_owned_world_writable_dir_is_not_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """Like ``/tmp``: nobody can replace an entry they do not own."""
    venv = tmp_path / "root-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.modes[tmp_path] = stat.S_IFDIR | 0o1777
    assert _run().status == "pass"


def test_a_group_writable_dir_whose_group_is_root_is_not_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """Only root's group can write it, and membership of group 0 is root already."""
    venv = tmp_path / "root-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)} helper")
    owners.modes[tmp_path] = stat.S_IFDIR | 0o775
    owners.gids[tmp_path] = 0
    assert _run().status == "pass"


def test_a_root_owned_link_to_a_deploy_owned_target_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    links = tmp_path / "usr-local-bin"
    links.mkdir()
    (links / "fraisier").symlink_to(script)
    _unit(unit_dir, "h.service", f"ExecStart={links / 'fraisier'} helper")
    owners.deploy_owns(venv)
    result = _run()
    assert result.status == "fail"
    assert str(venv) in result.detail


def test_a_link_in_a_deploy_owned_dir_is_flagged_even_to_a_root_target(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """``~/.local/bin`` can be re-pointed whatever the link currently names."""
    script = _venv(tmp_path / "root-venv")
    local_bin = tmp_path / "home" / "deploy" / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "fraisier").symlink_to(script)
    _unit(unit_dir, "h.service", f"ExecStart={local_bin / 'fraisier'} helper")
    owners.deploy_owns(tmp_path / "home" / "deploy")
    result = _run()
    assert result.status == "fail"
    assert str(tmp_path / "home" / "deploy") in result.detail


def test_backup_alert_root_on_bin_sh_passes(unit_dir: Path, owners: FakeOwners) -> None:
    """``backup-alert@`` has no ``User=`` and runs only ``/bin/sh``."""
    _unit(
        unit_dir,
        "fraisier-p-backup-alert@.service",
        "Type=oneshot",
        "ExecStart=/bin/sh -c \"echo 'fraisier-p backup failed: %i' | "
        'systemd-cat -t fraisier-backup-alert -p err"',
    )
    result = _run()
    assert result.status == "pass", result.detail


def test_the_scaffold_install_helper_script_argument_is_judged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """The helper runs ``bash <script>``: the script is code root executes."""
    state = tmp_path / "var-lib-fraisier" / "p" / "scaffold"
    state.mkdir(parents=True)
    (state / "install.sh").write_text("#!/bin/bash\n")
    helper = _venv(tmp_path / "root-venv").with_name("fraisier-scaffold-install-helper")
    helper.write_text(f"#!{helper.parent / 'python'}\n")
    _unit(
        unit_dir,
        "fraisier-p-scaffold-install-helper.service",
        f"ExecStart={helper} --deploy-user deploy {state / 'install.sh'}",
    )
    owners.deploy_owns(state)
    result = _run()
    assert result.status == "fail"
    assert str(state) in result.detail


def test_a_deploy_unit_is_not_judged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "w.service", "User=deploy", f"ExecStart={_venv(venv)} webhook")
    owners.deploy_owns(venv)
    assert _run().status == "skip"


@pytest.mark.parametrize("user", ["root", "0"])
def test_user_root_is_root(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners, user: str
) -> None:
    """Path 6 (``retain.user: root``) and path 9 (``service.user: root``)."""
    venv = tmp_path / "deploy-venv"
    _unit(
        unit_dir, "r.service", f"User={user}", f"ExecStart={_venv(venv)} backup prune"
    )
    owners.deploy_owns(venv)
    assert _run().status == "fail"


def test_dynamic_user_is_not_root(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "d.service", "DynamicUser=yes", f"ExecStart={_venv(venv)} x")
    owners.deploy_owns(venv)
    assert _run().status == "skip"


@pytest.mark.parametrize("prefix", ["+", "!", "-+", "@+"])
def test_a_privileged_line_in_a_deploy_unit_is_judged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners, prefix: str
) -> None:
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    argv = f"{script} argzero" if "@" in prefix else str(script)
    _unit(
        unit_dir,
        "w.service",
        "User=deploy",
        f"ExecStartPre={prefix}{argv}",
        "ExecStart=/bin/true",
    )
    owners.deploy_owns(venv)
    result = _run()
    assert result.status == "fail"
    assert "w.service" in result.detail
    assert "ExecStartPre" in result.detail


def test_a_double_bang_line_is_not_privileged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """``!!`` is ignored wherever ambient capabilities exist (systemd 260 warns
    "no longer supported and is now ignored"), so the line runs as ``User=``."""
    venv = tmp_path / "deploy-venv"
    _unit(
        unit_dir,
        "w.service",
        "User=deploy",
        f"ExecStartPre=!!{_venv(venv)}",
        "ExecStart=/bin/true",
    )
    owners.deploy_owns(venv)
    assert _run().status == "skip"


def test_permissions_start_only_makes_the_other_lines_privileged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(
        unit_dir,
        "w.service",
        "User=deploy",
        "PermissionsStartOnly=yes",
        f"ExecStartPre={_venv(venv)}",
        "ExecStart=/bin/true",
    )
    owners.deploy_owns(venv)
    result = _run()
    assert result.status == "fail"
    assert "ExecStartPre" in result.detail


# --- drop-ins (file reader) -------------------------------------------------


def test_a_privileged_line_from_a_dropin_is_judged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "w.service", "User=deploy", "ExecStart=/bin/true")
    _dropin(unit_dir, "w.service.d", "x.conf", f"ExecStartPre=+{_venv(venv)}")
    owners.deploy_owns(venv)
    assert _run().status == "fail"


@pytest.mark.parametrize("dirname", ["w.service.d", "service.d", "w-.service.d"])
def test_a_dropin_under_run_that_clears_user_makes_the_unit_root(
    tmp_path: Path,
    unit_dir: Path,
    owners: FakeOwners,
    monkeypatch: pytest.MonkeyPatch,
    dirname: str,
) -> None:
    """``/run`` and ``/usr/lib`` drop-ins, and type- and prefix-level ones, count."""
    run = tmp_path / "run-systemd"
    monkeypatch.setattr(doctor, "SYSTEMD_DROPIN_ROOTS", (run, unit_dir))
    venv = tmp_path / "deploy-venv"
    _unit(
        unit_dir,
        "w-x.service" if "-" in dirname else "w.service",
        "User=deploy",
        f"ExecStart={_venv(venv)}",
    )
    _dropin(run, dirname, "50-root.conf", "User=")
    owners.deploy_owns(venv)
    assert _run().status == "fail"


def test_a_dropin_that_sets_user_makes_the_unit_unprivileged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "w.service", f"ExecStart={_venv(venv)}")
    _dropin(unit_dir, "w.service.d", "user.conf", "User=deploy")
    owners.deploy_owns(venv)
    assert _run().status == "skip"


def test_an_empty_exec_assignment_in_a_dropin_clears_the_list(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "h.service", f"ExecStart={_venv(venv)}")
    _dropin(unit_dir, "h.service.d", "x.conf", "ExecStart=", "ExecStart=/bin/true")
    owners.deploy_owns(venv)
    assert _run().status == "pass"


def test_etc_masks_a_same_named_dropin_under_run(
    tmp_path: Path,
    unit_dir: Path,
    owners: FakeOwners,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "run-systemd"
    monkeypatch.setattr(doctor, "SYSTEMD_DROPIN_ROOTS", (run, unit_dir))
    venv = tmp_path / "deploy-venv"
    _unit(unit_dir, "w.service", "User=deploy", f"ExecStart={_venv(venv)}")
    _dropin(run, "w.service.d", "o.conf", "User=")
    _dropin(unit_dir, "w.service.d", "o.conf", "Nice=5")
    owners.deploy_owns(venv)
    assert _run().status == "skip"


# --- systemctl show (live reader) -------------------------------------------

#: Captured from systemd 260 (``systemctl --user show`` of a transient unit with
#: ``ExecStartPre=!/bin/true a``, ``ExecStartPost=!!/bin/true b``,
#: ``ExecStopPost=+/bin/true c``, ``ExecCondition=-@/bin/true argzero d``).
SHOW_FLAGS = """\
LoadState=loaded
NeedDaemonReload=no
ExecConditionEx={ path=/bin/true ; argv[]=argzero d ; flags=ignore-failure ; start_time=[Sat 2026-10-10 18:44:45 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798735 ; code=exited ; status=0 }
ExecStartPre={ path=/bin/true ; argv[]=/bin/true a ; ignore_errors=no ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798737 ; code=exited ; status=0 }
ExecStartPreEx={ path=/bin/true ; argv[]=/bin/true a ; flags=no-setuid ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798737 ; code=exited ; status=0 }
ExecStart={ path=/bin/true ; argv[]=/bin/true main ; ignore_errors=no ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798746 ; code=exited ; status=0 }
ExecStartEx={ path=/bin/true ; argv[]=/bin/true main ; flags= ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798746 ; code=exited ; status=0 }
ExecStartPost={ path=/bin/true ; argv[]=/bin/true b ; ignore_errors=no ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798747 ; code=exited ; status=0 }
ExecStartPostEx={ path=/bin/true ; argv[]=/bin/true b ; flags= ; start_time=[Sat 2026-10-10 18:44:46 CEST] ; stop_time=[Sat 2026-10-10 18:44:46 CEST] ; pid=2798747 ; code=exited ; status=0 }
ExecStopPost={ path=/bin/true ; argv[]=/bin/true c ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }
ExecStopPostEx={ path=/bin/true ; argv[]=/bin/true c ; flags=privileged ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }
User=deploy
DynamicUser=no
"""

#: Captured from systemd 260 for a unit with ``ExecStartPre=-+/bin/true x``.
SHOW_TWO_FLAGS = """\
LoadState=loaded
ExecStartPreEx={ path=/bin/true ; argv[]=/bin/true x ; flags=ignore-failure privileged ; start_time=[Sat 2026-10-10 18:44:51 CEST] ; stop_time=[Sat 2026-10-10 18:44:51 CEST] ; pid=2799813 ; code=exited ; status=0 }
User=
DynamicUser=no
"""


def test_show_reads_user_and_privilege_flags() -> None:
    service = doctor._parse_systemctl_show(SHOW_FLAGS)
    assert service is not None
    assert service.user == "deploy"
    by_key = {c.key: c for c in service.commands}
    assert set(by_key) == {
        "ExecCondition",
        "ExecStartPre",
        "ExecStart",
        "ExecStartPost",
        "ExecStopPost",
    }
    assert by_key["ExecStartPre"].privileged  # `!`
    assert by_key["ExecStopPost"].privileged  # `+`
    assert not by_key["ExecStartPost"].privileged  # `!!`, ignored
    assert not by_key["ExecStart"].privileged
    # `@` puts argzero in argv[]; the executable is path=.
    assert by_key["ExecCondition"].argv == ("/bin/true", "d")
    assert by_key["ExecStart"].argv == ("/bin/true", "main")


def test_show_reads_several_flags() -> None:
    service = doctor._parse_systemctl_show(SHOW_TWO_FLAGS)
    assert service is not None
    assert service.user == ""
    assert service.commands[0].privileged


@pytest.mark.parametrize(
    "text",
    [
        "LoadState=not-found\nUser=\nDynamicUser=no\n",
        "LoadState=masked\nUser=\nDynamicUser=no\n",
        "LoadState=loaded\nNeedDaemonReload=yes\nUser=\n",
        # systemd < 243 has no *Ex properties, so a `+` cannot be seen.
        "LoadState=loaded\nExecStart={ path=/bin/true ; argv[]=/bin/true ;"
        " ignore_errors=no ; start_time=[n/a] }\nUser=\n",
    ],
)
def test_show_defers_to_the_files_when_it_cannot_answer(text: str) -> None:
    assert doctor._parse_systemctl_show(text) is None


def test_the_live_reader_wins_over_the_files(
    tmp_path: Path,
    unit_dir: Path,
    owners: FakeOwners,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drop-in the file reader cannot see (say, a vendor dir) cleared ``User=``."""
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    _unit(unit_dir, "w.service", "User=deploy", f"ExecStart={script}")
    show = (
        "LoadState=loaded\nNeedDaemonReload=no\n"
        f"ExecStartEx={{ path={script} ; argv[]={script} ; flags= ;"
        " start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
        "User=\nDynamicUser=no\n"
    )
    asked: list[list[str]] = []

    def fake_show(names: list[str]) -> dict[str, str]:
        asked.append(names)
        return {"w.service": show}

    monkeypatch.setattr(doctor, "_systemctl_show", fake_show)
    owners.deploy_owns(venv)
    assert _run().status == "fail"
    assert asked == [["w.service"]]


def test_a_template_unit_is_read_from_its_files(
    unit_dir: Path, owners: FakeOwners, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``systemctl show foo@.service`` refuses a template name outright."""
    asked: list[list[str]] = []
    monkeypatch.setattr(
        doctor, "_systemctl_show", lambda names: asked.append(names) or {}
    )
    _unit(unit_dir, "a@.service", "ExecStart=/bin/true %i")
    assert _run().status == "pass"
    assert asked in ([], [[]])


def test_show_output_is_split_per_unit() -> None:
    blocks = doctor._split_show_output(
        "Id=a.service\nUser=\n\nId=b.service\nUser=x\n", ["a.service", "b.service"]
    )
    assert blocks == {
        "a.service": "Id=a.service\nUser=\n",
        "b.service": "Id=b.service\nUser=x\n",
    }


# --- reporting --------------------------------------------------------------


def test_no_unit_dir_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", tmp_path / "missing")
    assert _run().status == "skip"


def test_every_offending_unit_is_named(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    _unit(unit_dir, "a.service", f"ExecStart={script}")
    _unit(unit_dir, "b.service", f"ExecStart={script}")
    owners.deploy_owns(venv)
    result = _run()
    assert "a.service" in result.detail
    assert "b.service" in result.detail
    assert result.detail.startswith("2 ")


def test_one_unit_is_reported_once_however_many_lines_are_flawed(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    _unit(unit_dir, "a.service", f"ExecStartPre={script}", f"ExecStart={script}")
    owners.deploy_owns(venv)
    assert _run().detail.startswith("1 unit(s)")


def test_an_env_shebang_is_followed_to_the_interpreter(
    tmp_path: Path,
    unit_dir: Path,
    owners: FakeOwners,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy_bin = tmp_path / "deploy-bin"
    deploy_bin.mkdir()
    (deploy_bin / "python3").write_text("")
    (deploy_bin / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(deploy_bin))
    script = tmp_path / "root-bin" / "tool"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env -S python3 -I\n")
    _unit(unit_dir, "h.service", f"ExecStart={script}")
    owners.deploy_owns(deploy_bin)
    result = _run()
    assert result.status == "fail"
    assert str(deploy_bin) in result.detail


def test_a_bare_executable_is_found_on_systemds_path(
    tmp_path: Path,
    unit_dir: Path,
    owners: FakeOwners,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv = tmp_path / "deploy-venv"
    script = _venv(venv)
    monkeypatch.setattr(doctor, "_SYSTEMD_EXEC_PATH", str(script.parent))
    _unit(unit_dir, "h.service", "ExecStart=fraisier helper")
    owners.deploy_owns(venv)
    assert _run().status == "fail"


def test_dropin_dirs_cover_type_prefix_template_and_unit() -> None:
    assert doctor._dropin_dir_names("fraisier-p-x@a.service") == [
        "service.d",
        "fraisier-.service.d",
        "fraisier-p-.service.d",
        "fraisier-p-x@.service.d",
        "fraisier-p-x@a.service.d",
    ]


def test_show_output_that_does_not_line_up_is_not_trusted() -> None:
    assert doctor._split_show_output("User=\n", ["a.service", "b.service"]) == {}


@pytest.mark.parametrize("outcome", ["missing", "refused"])
def test_a_systemctl_that_cannot_answer_reads_as_no_answer(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import subprocess

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        if outcome == "missing":
            raise FileNotFoundError("systemctl")
        return subprocess.CompletedProcess([], 1, "User=\n", "Failed")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor._systemctl_show(["a.service"]) == {}


def test_a_root_owned_link_to_a_root_owned_target_passes(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """A symlink is 0777 on Linux; that is not a world-writable file."""
    links = tmp_path / "usr-local-bin"
    links.mkdir()
    (links / "fraisier").symlink_to(_venv(tmp_path / "root-venv"))
    _unit(unit_dir, "h.service", f"ExecStart={links / 'fraisier'} helper")
    assert _run().status == "pass"


def test_a_link_to_a_deploy_owned_binary_is_flagged(
    tmp_path: Path, unit_dir: Path, owners: FakeOwners
) -> None:
    """No shebang and no venv: only following the link finds the target."""
    deploy_bin = tmp_path / "deploy-bin"
    deploy_bin.mkdir()
    binary = deploy_bin / "helper"
    binary.write_bytes(b"\x7fELF")
    links = tmp_path / "usr-local-bin"
    links.mkdir()
    (links / "helper").symlink_to(binary)
    _unit(unit_dir, "h.service", f"ExecStart={links / 'helper'}")
    owners.deploy_owns(deploy_bin)
    result = _run()
    assert result.status == "fail"
    assert str(deploy_bin) in result.detail


def test_an_at_prefix_drops_the_argv0_override_from_the_arguments() -> None:
    command = doctor._parse_exec_value("ExecStart", "@/bin/x zero /s.sh", False)
    assert command is not None
    assert command.argv == ("/bin/x", "/s.sh")
