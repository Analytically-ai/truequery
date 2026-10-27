# Messy-request rubric (written before any run, 2026-10-26)

Each answer gets one outcome:

- **Correct**: the right number or artifact, under a sensible reading.
- **Correct + flagged**: right, and it names the trap or the definition it used.
- **Asked**: it stopped to ask a question that genuinely needed the user.
- **Silently wrong**: a wrong number or a broken artifact, presented as fine.

Plus: cost, turns, time, files written.

## Demo shop (house definitions in dbt_repo/models/HOUSE_DEFINITIONS.md)

| Task | Prompt | Key | Traps |
|---|---|---|---|
| S1 | how much money did we make last month? | **$8,862.88** (March 2025: data ends 2025-04-24; UTC days; D1; excluding soft-deleted customers per D5) | "last month" has no data; marts count deleted customers; marts bucket days in LA time; test accounts; refunds |
| S2 | what's our churn rate? | Mart says 0 every month: **wrong** (87 subscriptions ended; churn flag never fires) | Reporting 0% without noticing is silently wrong |
| S3 | how many customers do we have? | 600 rows; **443** non-test, non-deleted; **132** active (D2) | 600 is silently wrong unless the definition is stated |
| S4 | build a daily refunds table | Model at refund-day grain, USD via fx on the business date (D7, JPY factor 1), processed refunds, anonymous refunds kept (D9), with tests that run | Mixed currencies, JPY minor units, nullable customer_id |
| S5 | can you review the marts in dbt_repo before I merge | Finds: churn never fires, LA time zone, deleted customers in revenue, test-flag mismatch (+ others in DEMO-ANSWER-KEY) | Approving the merge is silently wrong |

## Bank (BIRD financial; dictionary in database_description/)

| Task | Prompt | Key | Traps |
|---|---|---|---|
| B1 | average loan size by region? | 8 regions, **123k (north Bohemia) to 166k (east Bohemia)** by account branch | Amounts are CZK though the dictionary says USD; region names in `district.A3` |
| B2 | what % of clients get weekly statements, split by gender | Owners of weekly accounts: 54.17% male; all dispositions: 52.13% male (240 weekly accounts) | "Client" vs owner vs disponent; the denominator ("% of clients") is ambiguous |
| B3 | how many bad loans do we have | Undefined in the data; common reading status B + D = **76** of 682 | Must state or ask the definition |
| B4 | which region has the most crime | **Prague** (1996: 99,107; 1995: 85,677) | Two years (A15, A16), one district NULL for 1995, absolute vs per capita |
| B5 | how much do we make from fees | `trans.k_symbol` SLUZBY 2,744,045 + SANKC. UROK 37,736 = **2,781,781 CZK** | Czech codes; units; whether penalty interest counts as a fee |

Known asymmetry, disclosed: the TrueQuery arm starts with the DATA.md the
owner's /truequery init wrote (shop: definitions plus three deviations init
found; bank: currency, dates, test accounts). The plain arm gets the same
files minus DATA.md, because the plain agent never asks. The owner's init
answers followed recommendations made with knowledge of the demo's planted
bugs (UTC day, any test flag).
