# Vendored: ponytail-review

`SKILL.md` is copied unchanged from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
(`skills/ponytail-review/SKILL.md`, plugin version 5.1.0, commit
`9cc65d03aa2da1db7121b912d03596409ee340b8`), under the MIT licence in `LICENSE`.

`review-ticket` reads it and applies it to every candidate; in auto mode that review is the code
review that decides whether the PR merges. `review-ticket` also lists new `shortcut:` comments,
as upstream's `ponytail-debt` does (not bundled: one step of a procedure, not a skill of its own).
Checked against upstream main 9b58c1f on 2026-10-11: `ponytail-review` unchanged. Ponytail's core "build less" skill
is in `../ponytail/` (used by `implement-ticket`); its SessionStart hooks (which need Node) are
not bundled.

To update, copy the file from a newer upstream commit and change the commit above.
