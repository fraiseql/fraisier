"""A refusal located in the gate's built schema is restated in the tree's terms.

confiture 1.49 names the file and line of a ``DIFFER_403``; for the drift gate
that file is the schema it built into a temporary directory. ``confiture
build`` marks each file it concatenates, so the line maps back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.dbops.drift import _in_the_tree

_BUILT = Path("/tmp/fraisier-drift-x/expected_schema.sql")
_RULE = "=" * 42

_SEPARATORS = {
    "block_comment": ["", f"/* {_RULE}", " * File: {path}", f" * {_RULE} */", ""],
    "line_comment": ["", f"-- {_RULE}", "-- File: {path}", f"-- {_RULE}", ""],
    "mysql": ["", f"# {_RULE}", "# File: {path}", f"# {_RULE}", ""],
}


def _build(style: str, files: dict[str, list[str]]) -> str:
    lines = ["-- header", "-- generated"]
    for path, body in files.items():
        lines += [line.format(path=path) for line in _SEPARATORS[style]]
        lines += body
    return "\n".join(lines) + "\n"


def _line_of(text: str, needle: str) -> int:
    return text.splitlines().index(needle) + 1


def _said(line: int) -> str:
    return f'names column app.t."Bad" at {_BUILT}:{line}, which needs quotes'


@pytest.mark.parametrize("style", sorted(_SEPARATORS))
def test_a_line_maps_to_its_file_and_line(style: str) -> None:
    text = _build(
        style,
        {
            "010_a.sql": ["CREATE TABLE a (id int);"],
            "sub/020_b.sql": ["CREATE TABLE b (", "  id int,", '  "Bad" int', ");"],
        },
    )

    out = _in_the_tree(_said(_line_of(text, '  "Bad" int')), _BUILT, text)

    assert out == 'names column app.t."Bad" at sub/020_b.sql:3, which needs quotes'


def test_the_first_line_of_a_file_is_line_one() -> None:
    text = _build("block_comment", {"010_a.sql": ['CREATE TABLE "Bad" (id int);']})

    out = _in_the_tree(
        _said(_line_of(text, 'CREATE TABLE "Bad" (id int);')), _BUILT, text
    )

    assert " at 010_a.sql:1," in out


def test_a_custom_separator_falls_back_to_the_built_line() -> None:
    text = "-- header\n\n-- >> 010_a.sql\n" + 'CREATE TABLE "Bad" (id int);\n'

    out = _in_the_tree(_said(4), _BUILT, text)

    assert " at line 4 of the built schema," in out
    assert str(_BUILT) not in out


def test_a_marker_without_its_closing_rule_is_not_trusted() -> None:
    text = "-- header\n-- File: 010_a.sql\nCREATE TABLE x (id int);\n"

    out = _in_the_tree(_said(3), _BUILT, text)

    assert " at line 3 of the built schema," in out


def test_a_line_before_any_file_falls_back() -> None:
    text = _build("block_comment", {"010_a.sql": ["CREATE TABLE a (id int);"]})

    out = _in_the_tree(_said(1), _BUILT, text)

    assert " at line 1 of the built schema," in out


def test_a_line_on_a_separator_falls_back() -> None:
    text = _build("block_comment", {"010_a.sql": ["CREATE TABLE a (id int);"]})

    out = _in_the_tree(_said(_line_of(text, " * File: 010_a.sql") + 1), _BUILT, text)

    assert " at line " in out
    assert "of the built schema" in out


def test_a_message_naming_another_file_is_left_alone() -> None:
    message = 'names column app.t."Bad" at db/schema/x.sql:7, which needs quotes'

    assert _in_the_tree(message, _BUILT, "") == message


def test_a_line_in_an_earlier_file_maps_to_that_file() -> None:
    text = _build(
        "line_comment",
        {
            "010_a.sql": ['CREATE TABLE "Bad" (id int);'],
            "020_b.sql": ["CREATE TABLE b (id int);"],
        },
    )

    out = _in_the_tree(
        _said(_line_of(text, 'CREATE TABLE "Bad" (id int);')), _BUILT, text
    )

    assert " at 010_a.sql:1," in out


@pytest.mark.parametrize(
    ("after_marker", "then"),
    [
        ("-- not a rule", ""),
        (f"-- {_RULE}", "CREATE TABLE x (id int);"),
    ],
    ids=["no-closing-rule", "no-blank-line"],
)
def test_a_marker_not_shaped_like_a_build_separator_is_not_trusted(
    after_marker: str, then: str
) -> None:
    text = "\n".join(["-- header", "-- File: 010_a.sql", after_marker, then, "x"])

    out = _in_the_tree(_said(5), _BUILT, text)

    assert " at line 5 of the built schema," in out


def test_a_marker_at_the_end_of_the_build_is_not_trusted() -> None:
    text = f"-- header\n-- File: 010_a.sql\n-- {_RULE}\n"

    out = _in_the_tree(_said(3), _BUILT, text)

    assert " at line 3 of the built schema," in out
