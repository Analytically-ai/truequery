# /truequery:review — run all checks plus the receipt

When: before merge, before handoff, or when asked "is this right?".
Any mode; any stack.

1. Call `truequery context [--target <path>]` first. Respect MODE and the
   contracts in scope.
2. Run `truequery review [<paths>]`. With no paths it reviews the project.
   Quote the receipt verbatim. A pass with unverified files is not a
   pass — say which files, and why.
3. Fix every error finding, or add the narrowest ignore with a reason
   (`truequery ignore add <rule> <path:line> --reason "..."`). Warnings get
   the same treatment unless you write down why not.
4. Then review what the checks can't see. Most real bugs live here, so
   do all of it, not only the items the receipt names:
   - **Read the whole path**: the files in scope and everything they
     depend on (upstream models, macros, schema and test YAML, and the
     build script or profile that sets session settings such as the
     time zone).
   - **Audit every definition**: for each rule in `DATA.md` and in the
     team's own definition docs, find the code that implements it and
     say whether it does. Re-check each known deviation in `DATA.md`:
     still there, or fixed?
   - **Per model**: grain and keys through every join
     (`model/grain-preserved-joins`); units, currencies and minor units
     in every sum; the time zone of every date cut
     (`consume/explicit-timezone-consumers`); test, deleted and status
     filters; NULLs and fallback defaults that hide missing data
     (`model/null-join-keys-excluded`); hard-coded dates and constants;
     comments that claim what the code doesn't do; tests on keys.
   - **Confirm in the data** when a warehouse or file is reachable,
     read-only: one count or sum per suspected bug. Label each finding
     "confirmed in data" or "by reading".
5. Report blockers first (wrong headline numbers, broken definitions),
   then should-fix, then minor, and end with a merge recommendation.
6. Close with the receipt again if anything changed.

Output: receipt + findings by severity + merge recommendation.

Conflict rule: a finding disposition that contradicts a contract or
definition (waiving a key test a contract requires, accepting a
computation a governed metric defines differently) is called out in
the receipt as a conflict, not silently waived (see `flow.md`).
