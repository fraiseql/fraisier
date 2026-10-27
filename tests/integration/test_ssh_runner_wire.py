"""``SSHRunner`` against a real sshd: the remote command runs and does its job.

These assert what happened on the host (output, cwd, uid, files written),
never what the command string contains. Substring asserts on that string are
how every ``SSHRunner`` command exiting 127 survived from v0.8.0 to v0.94.0
(#494).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from fraisier import ssh
from fraisier.runners import SSHRunner

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import SshdBox

pytestmark = pytest.mark.integration

#: Every wire test bounds its call, so a remote side that waits forever fails.
WIRE_TIMEOUT = 10


def _runner(
    sshd: SshdBox,
    user: str,
    *,
    use_sudo: bool = False,
    sudo_password: str | None = None,
) -> SSHRunner:
    target = sshd.target(user)
    return SSHRunner(
        host=target.host,
        user=user,
        port=target.port,
        key_path=target.key_path,
        strict_host_key=False,
        use_sudo=use_sudo,
        sudo_password=sudo_password,
        connect_timeout=target.connect_timeout,
    )


def _remote_dir() -> str:
    return f"/tmp/fraisier-wire-{uuid.uuid4().hex[:12]}"


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("alpha\n")
    (tree / "sub" / "b.txt").write_text("beta\n")
    return tree


def _read_back(sshd: SshdBox, remote_dir: str) -> str:
    """Both files' contents, read as root so any owner is readable."""
    result = ssh.short_cmd(
        sshd.target("nopw"),
        ["sudo", "-n", "cat", f"{remote_dir}/a.txt", f"{remote_dir}/sub/b.txt"],
    )
    return result.stdout


def test_run_runs_the_command(sshd: SshdBox) -> None:
    result = _runner(sshd, "nopw").run(["echo", "hi"], timeout=WIRE_TIMEOUT)

    assert result.stdout == "hi\n"


def test_run_preserves_argument_boundaries(sshd: SshdBox) -> None:
    words = ["a b", "c'd", '"e"', "$HOME", "f;g"]

    result = _runner(sshd, "nopw").run(["printf", "%s|", *words], timeout=WIRE_TIMEOUT)

    assert result.stdout == "".join(f"{w}|" for w in words)


def test_run_in_cwd(sshd: SshdBox) -> None:
    cwd = f"{_remote_dir()} with space"
    ssh.short_cmd(sshd.target("nopw"), ["mkdir", "-p", cwd])

    result = _runner(sshd, "nopw").run(["pwd"], cwd=cwd, timeout=WIRE_TIMEOUT)

    assert result.stdout == f"{cwd}\n"


def test_run_passes_env_to_the_command(sshd: SshdBox) -> None:
    result = _runner(sshd, "nopw").run(
        ["printenv", "FRAISIER_WIRE"],
        env={"FRAISIER_WIRE": "v a'l"},
        timeout=WIRE_TIMEOUT,
    )

    assert result.stdout == "v a'l\n"


def test_run_under_sudo_runs_as_root(sshd: SshdBox) -> None:
    result = _runner(sshd, "nopw", use_sudo=True).run(
        ["id", "-u"], timeout=WIRE_TIMEOUT
    )

    assert result.stdout == "0\n"


def test_run_under_sudo_with_password_runs_as_root(sshd: SshdBox) -> None:
    runner = _runner(sshd, "pw", use_sudo=True, sudo_password=sshd.password)

    result = runner.run(["id", "-u"], timeout=WIRE_TIMEOUT)

    assert result.stdout == "0\n"


@pytest.mark.parametrize(
    ("user", "use_sudo", "with_password"),
    [("nopw", False, False), ("nopw", True, False), ("pw", True, True)],
    ids=["plain", "sudo", "sudo-password"],
)
def test_upload_tree_writes_every_file(
    sshd: SshdBox,
    tmp_path: Path,
    user: str,
    use_sudo: bool,
    with_password: bool,
) -> None:
    remote_dir = _remote_dir()
    runner = _runner(
        sshd,
        user,
        use_sudo=use_sudo,
        sudo_password=sshd.password if with_password else None,
    )

    runner.upload_tree(_tree(tmp_path), remote_dir)

    assert _read_back(sshd, remote_dir) == "alpha\nbeta\n"


@pytest.mark.parametrize(
    ("user", "with_password"),
    [("nopw", False), ("pw", True)],
    ids=["sudo", "sudo-password"],
)
def test_upload_tree_under_sudo_is_owned_by_root(
    sshd: SshdBox, tmp_path: Path, user: str, with_password: bool
) -> None:
    """Root owns the whole tree, however the upload got root.

    Bootstrap runs the uploaded ``install.sh`` as root. Root's ``tar`` keeps
    the archive's numeric owners (the operator's local uid), and ``cp -a``
    keeps the SSH user's: either way a non-root account could rewrite a file
    root then executes.
    """
    remote_dir = _remote_dir()
    runner = _runner(
        sshd,
        user,
        use_sudo=True,
        sudo_password=sshd.password if with_password else None,
    )

    runner.upload_tree(_tree(tmp_path), remote_dir)

    not_root = ssh.short_cmd(
        sshd.target("nopw"),
        ["sudo", "-n", "find", remote_dir, "!", "-uid", "0", "-printf", "%u %p\\n"],
    )
    assert _read_back(sshd, remote_dir) == "alpha\nbeta\n"
    assert not_root.stdout == ""


#: Prints the secret as the command's env holds it, then how many argv on the
#: host contain it. The bracket keeps this pattern from matching itself.
_SECRET = "wire-pgpass-value"
_SEES_SECRET = [
    "sh",
    "-c",
    'printenv PGPASSWORD; ps -eo args | grep -c "wire-pgpass-valu[e]" || true',
]


@pytest.mark.parametrize(
    ("user", "use_sudo", "with_password"),
    [("nopw", False, False), ("nopw", True, False), ("pw", True, True)],
    ids=["plain", "sudo", "sudo-password"],
)
def test_env_value_reaches_the_command_and_no_remote_argv(
    sshd: SshdBox, user: str, use_sudo: bool, with_password: bool
) -> None:
    runner = _runner(
        sshd,
        user,
        use_sudo=use_sudo,
        sudo_password=sshd.password if with_password else None,
    )

    result = runner.run(_SEES_SECRET, env={"PGPASSWORD": _SECRET}, timeout=WIRE_TIMEOUT)

    assert result.stdout == f"{_SECRET}\n0\n"


#: Prints a marker only if the command ran, and the env it got.
_RAN = ["sh", "-c", "echo ran; printenv PGPASSWORD"]


def test_wrong_sudo_password_costs_one_attempt_and_runs_nothing(
    sshd: SshdBox,
) -> None:
    """The password is checked on its own one-line pipe first (D7).

    A one-stage ``sudo -S`` retried with the sentinel and the ``export``
    lines as further password attempts: three tries, one of them a secret.
    """
    runner = _runner(sshd, "pw", use_sudo=True, sudo_password="wrong")

    result = runner.run(
        _RAN, env={"PGPASSWORD": _SECRET}, timeout=WIRE_TIMEOUT, check=False
    )

    assert result.returncode == 124
    assert result.stdout == ""
    assert "1 incorrect password attempt" in result.stderr
    assert result.stderr.count("Sorry, try again") == 1


@pytest.mark.parametrize("user", ["pw", "zshu"])
def test_right_sudo_password_never_reaches_output(sshd: SshdBox, user: str) -> None:
    runner = _runner(sshd, user, use_sudo=True, sudo_password=sshd.password)

    result = runner.run(_RAN, env={"PGPASSWORD": _SECRET}, timeout=WIRE_TIMEOUT)

    assert result.stdout == f"ran\n{_SECRET}\n"
    assert sshd.password not in result.stdout + result.stderr


def test_unneeded_sudo_password_is_never_executed(sshd: SshdBox) -> None:
    """NOPASSWD sudo leaves the password line unread; it must not run."""
    runner = _runner(sshd, "nopw", use_sudo=True, sudo_password=sshd.password)

    result = runner.run(_RAN, env={"PGPASSWORD": _SECRET}, timeout=WIRE_TIMEOUT)

    assert result.stdout == f"ran\n{_SECRET}\n"
    assert result.stderr == ""


def test_no_sudo_ticket_fails_loudly_and_leaks_nothing(sshd: SshdBox) -> None:
    """``timestamp_timeout=0`` keeps no ticket for the inner ``sudo -n``."""
    runner = _runner(sshd, "nocache", use_sudo=True, sudo_password=sshd.password)

    result = runner.run(
        _RAN, env={"PGPASSWORD": _SECRET}, timeout=WIRE_TIMEOUT, check=False
    )

    assert result.returncode != 0
    assert "a password is required" in result.stderr
    assert result.stdout == ""
    assert _SECRET not in result.stderr
    assert sshd.password not in result.stderr


def _remote_env(
    runner: SSHRunner,
    *,
    scrub_pg: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    out = runner.run(["env"], timeout=WIRE_TIMEOUT, scrub_pg=scrub_pg, env=env).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_the_remote_login_has_ambient_pg_variables(sshd: SshdBox) -> None:
    """The control: without ``scrub_pg`` the host's own ``PG*`` reach the
    command (the box sets them in ``/etc/environment``)."""
    assert _remote_env(_runner(sshd, "nopw"))["PGSERVICE"] == "acme"


@pytest.mark.parametrize("user", ["nopw", "zshu"])
def test_scrub_pg_drops_the_hosts_pg_but_the_operator_defaults(
    sshd: SshdBox, user: str
) -> None:
    env = _remote_env(_runner(sshd, user), scrub_pg=True, env={"PGPASSWORD": "pw"})

    assert "PGSERVICE" not in env
    assert env["PGSSLROOTCERT"] == "/etc/acme/root.crt"
    assert env["PGPASSWORD"] == "pw"


def test_scrub_pg_under_sudo_with_password(sshd: SshdBox) -> None:
    runner = _runner(sshd, "pw", use_sudo=True, sudo_password=sshd.password)

    env = _remote_env(runner, scrub_pg=True, env={"PGPASSWORD": "pw"})

    assert "PGSERVICE" not in env
    assert env["PGPASSWORD"] == "pw"
