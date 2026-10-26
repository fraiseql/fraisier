"""The ``sshd`` fixture is what its users say it is.

Every SSH runner test leans on these facts: a test that passes because a
password user turned out to be NOPASSWD, or because the developer's own
``~/.ssh/config`` answered, proves nothing about the code under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fraisier import ssh

if TYPE_CHECKING:
    from tests.integration.conftest import SshdBox

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("user", ["nopw", "pw", "nocache", "zshu"])
def test_each_user_logs_in_with_the_session_key(sshd: SshdBox, user: str) -> None:
    result = ssh.short_cmd(sshd.target(user), ["id", "-un"], check=False)

    assert (result.returncode, result.stdout.strip()) == (0, user)


def test_ssh_goes_through_the_isolating_wrapper(sshd: SshdBox) -> None:
    ssh.short_cmd(sshd.target("nopw"), ["true"])

    known_hosts = Path(sshd.wrapper_dir).parent / "known_hosts"
    assert f"[{sshd.host}]:{sshd.port}" in known_hosts.read_text()


def test_nopw_runs_sudo_without_a_password(sshd: SshdBox) -> None:
    result = ssh.short_cmd(sshd.target("nopw"), ["sudo", "-n", "id", "-u"])

    assert result.stdout.strip() == "0"


@pytest.mark.parametrize("user", ["pw", "nocache", "zshu"])
def test_password_users_need_the_password(sshd: SshdBox, user: str) -> None:
    result = ssh.short_cmd(sshd.target(user), ["sudo", "-n", "true"], check=False)

    assert result.returncode == 1
    assert "a password is required" in result.stderr


def test_the_password_unlocks_sudo(sshd: SshdBox) -> None:
    result = ssh.cmd_with_input(
        sshd.target("pw"),
        ["sudo", "-S", "-p", "", "id", "-u"],
        input=sshd.password + "\n",
    )

    assert result.stdout.strip() == "0"


def test_zshu_logs_in_to_zsh(sshd: SshdBox) -> None:
    result = ssh.short_cmd(sshd.target("zshu"), ["getent", "passwd", "zshu"])

    assert result.stdout.strip().rsplit(":", 1)[1] == "/usr/bin/zsh"


def test_nocache_keeps_no_sudo_ticket(sshd: SshdBox) -> None:
    result = ssh.short_cmd(
        sshd.target("nopw"), ["sudo", "-n", "cat", "/etc/sudoers.d/nocache"]
    )

    assert "Defaults:nocache timestamp_timeout=0" in result.stdout
