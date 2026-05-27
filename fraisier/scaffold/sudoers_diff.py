"""Compute a line-level diff between two `/etc/sudoers.d/<project>` fragments.

Pure-function module used by `scaffold-install` (#224) to detect rules that
would be silently removed when the generated sudoers fragment overwrites the
one currently on disk. The diff treats each non-comment, non-blank line as an
opaque normalized string — sudoers files are line-oriented and structural
parsing is unnecessary for our "set of rules" comparison.

Caveat: `_normalize_whitespace` collapses interior whitespace, which means
``Defaults env_keep += "A  B"`` and ``Defaults env_keep += "A B"`` compare
equal. Fraisier-generated rules never contain interior quoted whitespace; a
hand-edited rule that depends on it is vanishingly rare and would be a
false negative (not a false positive — we'd say "no change" when there is
one), so the safety net never warns incorrectly. Accept the trade.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SudoersDiff:
    """Result of comparing two sudoers fragments as sets of normalized rules.

    `added` and `removed` preserve the order they appear in their source file
    (new rules iterated against current set; current rules iterated against
    new set).
    """

    added: list[str]
    removed: list[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed)


def diff_sudoers(current: str, new: str) -> SudoersDiff:
    """Return the rules added in `new` and removed from `current`.

    Comments and blank lines are ignored; whitespace within rules is normalized
    (see module docstring caveat).
    """
    cur_rules = _parse_rules(current)
    new_rules = _parse_rules(new)
    cur_set = set(cur_rules)
    new_set = set(new_rules)
    return SudoersDiff(
        added=[r for r in new_rules if r not in cur_set],
        removed=[r for r in cur_rules if r not in new_set],
    )


def _parse_rules(content: str) -> list[str]:
    out: list[str] = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(_normalize_whitespace(stripped))
    return out


def _normalize_whitespace(rule: str) -> str:
    return " ".join(rule.split())
