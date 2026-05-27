"""Unit tests for the sudoers diff helpers (#224).

Pure-function tests over `fraisier.scaffold.sudoers_diff`. Strings in, dataclass
out — no I/O, no monkeypatching. The wrapper-side integration is covered by
`tests/test_scaffold.py::TestScaffoldCLI`.
"""

from __future__ import annotations


class TestParseRules:
    """`_parse_rules` extracts non-comment, non-blank, whitespace-normalized lines."""

    def test_strips_comments_and_blanks(self):
        from fraisier.scaffold.sudoers_diff import _parse_rules

        content = """\
# header comment
# do not edit

user1 ALL=(root) NOPASSWD: /usr/bin/foo
# inline-ish comment
user2 ALL=(root) NOPASSWD: /usr/bin/bar
"""
        assert _parse_rules(content) == [
            "user1 ALL=(root) NOPASSWD: /usr/bin/foo",
            "user2 ALL=(root) NOPASSWD: /usr/bin/bar",
        ]

    def test_collapses_interior_whitespace(self):
        from fraisier.scaffold.sudoers_diff import _parse_rules

        content = "user1  ALL=(root)   NOPASSWD: /usr/bin/foo\n"
        assert _parse_rules(content) == [
            "user1 ALL=(root) NOPASSWD: /usr/bin/foo",
        ]

    def test_empty_input_yields_empty_list(self):
        from fraisier.scaffold.sudoers_diff import _parse_rules

        assert _parse_rules("") == []
        assert _parse_rules("\n\n# only comments\n\n") == []


class TestDiffSudoers:
    """`diff_sudoers` returns added / removed sets (order-preserving)."""

    def test_detects_removed_rules(self):
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = (
            "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
            "admin ALL=(root) NOPASSWD: /usr/bin/baz\n"
        )
        new = "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        diff = diff_sudoers(current, new)
        assert diff.removed == ["admin ALL=(root) NOPASSWD: /usr/bin/baz"]
        assert diff.added == []
        assert diff.has_changes is True

    def test_detects_added_rules(self):
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        new = (
            "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
            "newcomer ALL=(root) NOPASSWD: /usr/bin/qux\n"
        )
        diff = diff_sudoers(current, new)
        assert diff.added == ["newcomer ALL=(root) NOPASSWD: /usr/bin/qux"]
        assert diff.removed == []
        assert diff.has_changes is True

    def test_mixed_add_and_remove(self):
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = (
            "a ALL=(root) NOPASSWD: /usr/bin/a\nb ALL=(root) NOPASSWD: /usr/bin/b\n"
        )
        new = "b ALL=(root) NOPASSWD: /usr/bin/b\nc ALL=(root) NOPASSWD: /usr/bin/c\n"
        diff = diff_sudoers(current, new)
        assert diff.removed == ["a ALL=(root) NOPASSWD: /usr/bin/a"]
        assert diff.added == ["c ALL=(root) NOPASSWD: /usr/bin/c"]
        assert diff.has_changes is True

    def test_identical_files_have_no_changes(self):
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        content = "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        diff = diff_sudoers(content, content)
        assert diff.added == []
        assert diff.removed == []
        assert diff.has_changes is False

    def test_whitespace_only_differences_are_unchanged(self):
        """Rules differing only in interior whitespace are treated as equal."""
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = "user1  ALL=(root)   NOPASSWD: /usr/bin/foo\n"
        new = "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        diff = diff_sudoers(current, new)
        assert diff.has_changes is False

    def test_comment_only_differences_are_unchanged(self):
        """Comment lines do not participate in the diff."""
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = "# old header\nuser1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        new = "# new header\n# extra context\nuser1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        diff = diff_sudoers(current, new)
        assert diff.has_changes is False

    def test_order_preserved_from_source_list(self):
        from fraisier.scaffold.sudoers_diff import diff_sudoers

        current = "z ALL=(root) NOPASSWD: /a\na ALL=(root) NOPASSWD: /b\n"
        new = ""
        diff = diff_sudoers(current, new)
        # Order of `removed` follows the order in `current`, not sorted.
        assert diff.removed == [
            "z ALL=(root) NOPASSWD: /a",
            "a ALL=(root) NOPASSWD: /b",
        ]
