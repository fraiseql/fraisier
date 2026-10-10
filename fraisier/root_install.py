"""The root-owned copy of fraisier that the root helpers run (#433, D5).

The deploy user's copy lives in its own uv tool dir and is upgraded by the
webhook's self-upgrade. Root must never run it: whoever owns a venv owns
whatever root imports from it. So the root helpers run a second, root-owned
install under ``/usr/local/lib/fraisier-root`` — never under ``/opt/fraisier``,
which the deploy user owns — upgraded only by an operator with
``sudo fraisier-root-upgrade VERSION``.

Everything uv touches for that install is pinned under the root dir: the tool
venvs, their bin links, the managed Python and the cache. Left to its
defaults, uv under ``sudo`` can resolve them through a ``HOME`` the deploy user
owns and fetch the interpreter there.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT_DIR = "/usr/local/lib/fraisier-root"
ROOT_TOOL_DIR = f"{ROOT_DIR}/tools"
ROOT_BIN_DIR = f"{ROOT_DIR}/bin"
ROOT_PYTHON_INSTALL_DIR = f"{ROOT_DIR}/python"
ROOT_CACHE_DIR = f"{ROOT_DIR}/cache"
ROOT_UV_DIR = f"{ROOT_DIR}/uv"
ROOT_UV = f"{ROOT_UV_DIR}/uv"

ROOT_PYTHON = f"{ROOT_TOOL_DIR}/fraisier/bin/python"
"""The root helpers' interpreter. Run with ``-I -m <module>``: a console
script's shebang cannot carry ``-I``."""

ROOT_LINK = "/usr/local/bin/fraisier"
"""Root-owned, and first on sudo's ``secure_path``, so ``sudo fraisier`` runs
the root copy rather than whichever ``fraisier`` the caller's PATH finds."""

ROOT_UV_VERSION = "0.9.18"
ROOT_PYTHON_VERSION = "3.14"
ROOT_LINK_DIR = "/usr/local/bin"
ROOT_COMMANDS = ("fraisier", "fraisier-root-upgrade")

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ROOT_UV_URL = f"https://astral.sh/uv/{ROOT_UV_VERSION}/install.sh"
_VERSION_RE = re.compile(
    r"^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$"
)


def root_install_env() -> dict[str, str]:
    """The whole environment uv runs with: nothing inherited from the caller."""
    return {
        "HOME": "/root",
        "PATH": SAFE_PATH,
        "UV_TOOL_DIR": ROOT_TOOL_DIR,
        "UV_TOOL_BIN_DIR": ROOT_BIN_DIR,
        "UV_PYTHON_INSTALL_DIR": ROOT_PYTHON_INSTALL_DIR,
        "UV_CACHE_DIR": ROOT_CACHE_DIR,
        # Never an interpreter found on a PATH or in someone else's uv dir.
        "UV_PYTHON_PREFERENCE": "only-managed",
        # Never a uv.toml from the working directory or a user's config.
        "UV_NO_CONFIG": "1",
    }


def root_install_argv(version: str) -> list[str]:
    return [
        ROOT_UV,
        "tool",
        "install",
        "--force",
        "--python",
        ROOT_PYTHON_VERSION,
        f"fraisier=={version}",
    ]


def root_uv_install_argv() -> list[str]:
    """Install a pinned uv into the root dir, with the same empty environment.

    Then make it root's: the installer unpacks with the tarball's owner (uid
    1001, gid 117, measured on 0.9.18), and whoever holds that uid on the host
    could replace the uv root runs.
    """
    return [
        "sh",
        "-c",
        f"curl -LsSf {ROOT_UV_URL}"
        f" | env -i HOME=/root PATH={SAFE_PATH}"
        f" UV_INSTALL_DIR={ROOT_UV_DIR} UV_NO_MODIFY_PATH=1 sh"
        f" && chown -R root:root {ROOT_UV_DIR} && chmod -R go-w {ROOT_UV_DIR}",
    ]


def _run(
    argv: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, check=False, cwd="/")


def _uv_present() -> bool:
    return Path(ROOT_UV).is_file()


def _prepare_root_dir() -> None:
    root = Path(ROOT_DIR)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chown(root, 0, 0)
    root.chmod(0o755)


def _link(name: str) -> None:
    """Point ``ROOT_LINK_DIR/name`` at the root copy, replacing it atomically."""
    link = Path(ROOT_LINK_DIR) / name
    tmp = link.with_name(f".{name}.fraisier-new")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(f"{ROOT_BIN_DIR}/{name}")
    tmp.replace(link)


def main(argv: list[str] | None = None) -> int:
    """``sudo fraisier-root-upgrade VERSION``: install VERSION as the root copy.

    The only way the root copy changes. The version comes from the operator's
    command line, never from a deploy. The root helpers notice the new
    version and exit after their current request; socket activation restarts
    them on it.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: sudo fraisier-root-upgrade VERSION", file=sys.stderr)
        return 2
    version = args[0]
    if not _VERSION_RE.match(version):
        print(f"not one exact release version: {version!r}", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("fraisier-root-upgrade must run as root (sudo)", file=sys.stderr)
        return 1
    _prepare_root_dir()
    if not _uv_present() and _run(root_uv_install_argv()).returncode != 0:
        print(
            f"could not install uv {ROOT_UV_VERSION} into {ROOT_UV_DIR}",
            file=sys.stderr,
        )
        return 1
    if _run(root_install_argv(version), env=root_install_env()).returncode != 0:
        print(f"could not install fraisier {version} as the root copy", file=sys.stderr)
        return 1
    for name in ROOT_COMMANDS:
        _link(name)
    print(f"root copy is fraisier {version}; `sudo fraisier` runs it")
    return 0
