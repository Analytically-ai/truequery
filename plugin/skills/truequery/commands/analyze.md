# /truequery:analyze — answer a data question to the standard

When: an Explore-mode question ("why did revenue dip?", "how many
active users?"). Stack-neutral.

1. Call `truequery context [--target <path>]` first. Note MODE, the contracts
   in scope, and every CONTRACT_MISSING directive.
2. Restate the question, then fix the grain in words: "one row per
   <what>". If the grain is ambiguous, ask before querying.
3. Write the query against contracted tables where they exist. For
   CONTRACT_MISSING tables, state the grain assumption explicitly.
4. Sanity checks before answering: row counts vs a naive baseline,
   NULL rates on join keys and filters, one duplicated-grain probe
   (`GROUP BY <grain> HAVING COUNT(*) > 1`), and a spot check of two
   rows against source.
5. Answer with the number, the grain, the query, and caveats. The
   headline number uses the house definition's reading of each term;
   alternatives go below it. If the term isn't defined, state the
   reading first and put the closest house-anchored alternative next
   to it. Never present the first LIMIT-n rows as a sample (see
   `sql/explore-limit-is-not-a-sample`); profile filter columns
   before trusting a cut (see `sql/explore-profile-filter-columns`).

Output: answer + grain + query + caveats. No pipeline changes.

Conflict rule: if the question contradicts a definition or contract
(e.g. "revenue" against a contracted revenue definition, an average
over a column stored in minor units), say so before querying, follow the
definition unless the user confirms the override, and record the
conflict in the closing caveats (see `flow.md`).
