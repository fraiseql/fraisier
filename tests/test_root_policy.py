"""The root policy is trusted only if nobody but root could have written it (#433)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from fraisier.root_policy import (
    RootPolicy,
    RootPolicyError,
    UnitGrant,
    dump_policy,
    load_policy,
    parse_policy,
    policy_path,
)

POLICY = RootPolicy(
    project="demo",
    scaffold_dir="/var/lib/fraisier/demo/scaffold",
    users=frozenset({"deploy"}),
    groups=frozenset({"deploy"}),
    exec_prefixes=("/home/deploy/.local/bin/",),
    read_paths=frozenset({"/etc/demo/app.env"}),
    directories=frozenset({"app"}),
    units={"app.service": UnitGrant("systemd/app.service", "plain")},
    operator_only={"/etc/sudoers.d/demo": "sudoers"},
)

PATH = Path("/etc/fraisier/demo/root-policy.json")


def _st(mode: int, uid: int = 0, gid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


DIR = stat.S_IFDIR | 0o755
FILE = stat.S_IFREG | 0o644


def _fs(overrides: dict[str, os.stat_result] | None = None):
    table = {
        "/": _st(DIR),
        "/etc": _st(DIR),
        "/etc/fraisier": _st(DIR),
        "/etc/fraisier/demo": _st(DIR),
        str(PATH): _st(FILE),
        **(overrides or {}),
    }

    def lstat(path: Path) -> os.stat_result:
        try:
            return table[str(path)]
        except KeyError:
            raise FileNotFoundError(path) from None

    return lstat


def _load(overrides=None) -> RootPolicy:
    return load_policy(
        PATH, lstat=_fs(overrides), read_text=lambda _p: dump_policy(POLICY)
    )


def test_policy_path_is_under_etc_fraisier():
    assert policy_path("demo") == PATH


def test_a_root_only_chain_loads():
    assert _load() == POLICY


def test_dump_and_parse_round_trip():
    assert parse_policy(dump_policy(POLICY)) == POLICY


def test_a_missing_policy_names_the_operator_step():
    with pytest.raises(RootPolicyError, match="sudo fraisier scaffold-install"):
        load_policy(PATH, lstat=_missing, read_text=str)


def _missing(path: Path) -> os.stat_result:
    if str(path) == str(PATH):
        raise FileNotFoundError(path)
    return _st(DIR)


@pytest.mark.parametrize(
    ("component", "st"),
    [
        (str(PATH), _st(FILE, uid=1000)),
        ("/etc/fraisier/demo", _st(DIR, uid=1000)),
        ("/etc/fraisier", _st(stat.S_IFDIR | 0o777)),
        ("/etc/fraisier/demo", _st(stat.S_IFDIR | 0o775, gid=1000)),
        (str(PATH), _st(stat.S_IFREG | 0o664, gid=1000)),
        ("/etc/fraisier/demo", _st(stat.S_IFLNK | 0o777)),
        (str(PATH), _st(stat.S_IFLNK | 0o777)),
        (str(PATH), _st(DIR)),
        ("/etc/fraisier", _st(FILE)),
    ],
)
def test_anything_another_user_could_change_is_refused(component, st):
    with pytest.raises(RootPolicyError):
        _load({component: st})


@pytest.mark.parametrize("component", ["/etc/fraisier/demo", str(PATH)])
def test_a_symlink_is_refused_as_a_symlink(component):
    with pytest.raises(RootPolicyError, match="symlink"):
        _load({component: _st(stat.S_IFLNK | 0o777)})


def test_another_schema_version_is_refused_as_such():
    raw = dump_policy(POLICY).replace('"schema_version": 1', '"schema_version": 2')
    with pytest.raises(RootPolicyError, match="schema_version"):
        parse_policy(raw)


def test_a_sticky_root_directory_is_allowed():
    assert _load({"/etc": _st(stat.S_IFDIR | 0o1777)}) == POLICY


def test_a_sticky_directory_someone_else_owns_is_refused():
    with pytest.raises(RootPolicyError):
        _load({"/etc": _st(stat.S_IFDIR | 0o1777, uid=1000)})


def test_a_sticky_bit_on_the_file_itself_does_not_exempt_it():
    with pytest.raises(RootPolicyError):
        _load({str(PATH): _st(stat.S_IFREG | 0o1666)})


def test_group_write_for_root_group_is_allowed():
    assert _load({"/etc/fraisier": _st(stat.S_IFDIR | 0o775, gid=0)}) == POLICY


def test_a_relative_path_is_refused():
    with pytest.raises(RootPolicyError, match="not absolute"):
        load_policy(Path("root-policy.json"), lstat=_fs(), read_text=str)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"schema_version": 2}',
        dump_policy(POLICY).replace('"plain"', '"run_as_root"'),
        dump_policy(POLICY).replace(
            '"users": [\n    "deploy"\n  ]', '"users": "deploy"'
        ),
        dump_policy(POLICY).replace('"project": "demo"', '"project": ""'),
    ],
)
def test_a_malformed_policy_is_refused(raw):
    with pytest.raises(RootPolicyError):
        parse_policy(raw)
