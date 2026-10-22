# /truequery:ask — a plain-English answer to a data question

When: a non-technical user asks "how much money did we make last
month?" or "are we losing customers?". Uses `analyze` underneath;
follows `flow.md`.

1. **Look** quietly (flow step 1): `truequery context`, the relevant
   contracts and definitions. Do not narrate tooling to the user.
2. **Conflict check first**: if the question contradicts a definition
   ("money we made" where revenue has a house meaning), use the house
   meaning and say so in everyday words in the answer ("I counted only
   completed, paid orders — refunds taken off"). Never silently pick
   the other reading (flow conflict rule).
3. **Headline follows the house definition.** When the definitions
   decide how a term reads, the headline number uses that reading;
   alternatives go in a table below. When the term isn't defined, lead
   with the reading closest to a house definition, say so in the first
   sentence, and put the other readings right next to it, not further
   down.
4. Run the `analyze` procedure: fix the grain, write the query, run
   the sanity checks (row counts, NULL rates, duplicate-grain probe,
   spot check).
5. Answer in this order, no jargon:
   - **The number** (or the plain answer), with its unit and period.
   - **What it means**: one or two sentences a colleague would use.
   - **How sure I am, and why**: e.g. "Solid — this comes from the
     audited revenue table" or "Rough — 1 in 3 tickets has no account
     attached, so the per-account split is approximate."
   - **What I assumed**, in everyday words (the flow step-2 defaults).
6. Then a collapsed section, exactly headed `How I got this`, with the
   SQL and the checks run. The main answer must stand without it.

Never present a technical term (grain, SCD, idempotent) in the main
answer without translating it. If the data can't answer the question,
say what is missing instead of guessing.

Output: plain answer + confidence + assumptions + collapsed SQL.
