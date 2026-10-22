# /truequery:harden — make Build/Change work production-ready

When: a model, loader or migration is about to go live or change
shape. Modes: Build, Change.

1. Call `truequery context [--target <path>]` first, then `truequery review`.
   Fix or narrowly ignore every finding before hardening — hardening
   an unchecked query bakes the bug in.
2. Idempotency: re-running the write must change nothing. Name the
   mechanism (merge key, insert-overwrite partition, delete+insert
   window) and the evidence for it.
3. Late data: state the lateness policy (watermark, lookback, grace).
   Show where out-of-order rows land, including NULL event times.
4. Schema evolution: new columns must not shift positional logic
   (name columns in INSERTs, avoid `SELECT *` across boundaries);
   dropped or renamed columns need a deprecation path, not an edit.
5. Null and edge guards: join keys, filter columns and division
   denominators get explicit NULL handling; division guards against
   zero and unknown types (integer division truncates).
6. Backfills and migrations: state the blast radius (partitions,
   downstream models), the rollback, and how you will verify row
   counts before and after.

Output: the six answers above, each with evidence or a TODO owner.
