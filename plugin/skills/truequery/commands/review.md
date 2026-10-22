# /truequery:review — run all checks plus the receipt

When: before merge, before handoff, or when asked "is this right?".
Any mode; any stack.

1. Call `truequery context [--target <path>]` first. Respect MODE and the
   contracts in scope.
2. Run `truequery review [<paths>]`. With no paths it reviews the project.
3. Quote the receipt verbatim: what was checked at each evidence
   level, what was NOT checked and why, and the findings. A pass with
   unverified files is not a pass — say which files, and why.
4. Fix every error finding, or add the narrowest ignore with a reason
   (`truequery ignore add <rule> <path:line> --reason "..."`). Warnings get
   the same treatment unless you write down why not.
5. Then critique the judgment-only practices in scope yourself
   (the receipt names them). Deterministic checks can't see grain
   drift, business logic in marts, or stale contracts — you can.
   State which you applied and which you waived, with reasons.
6. Close with the receipt again if anything changed.

Output: findings disposition + judgment critique + final receipt.

Conflict rule: a finding disposition that contradicts a contract or
definition (waiving a key test a contract requires, accepting a
computation a governed metric defines differently) is called out in
the receipt as a conflict, not silently waived (see `flow.md`).
