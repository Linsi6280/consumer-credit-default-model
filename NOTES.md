# Interview / README notes

Working notes on decisions and findings worth talking about in an interview or writing
up in the README. Not polished prose — just the substance, organized by theme.

## Target definition (`sql/derive_target.sql`)

- **Default = ever reaching 90+ days delinquent (MBA method) within 24 months of
  origination**, including REO acquisition (worse than 90+ DPD by definition).
- Deliberately did **not** use the raw performance-file `loan_age` column to define the
  24-month window. Per Freddie Mac's spec, `loan_age` is computed from the
  *modification* First Payment Date for modified loans, so it resets to a low number
  after a loan mod — a loan modified 5 years in could show `loan_age = 1` again. ~2,600
  of the loans in the original single-vintage build had at least one modification, so
  trusting `loan_age` would have misclassified later-life delinquency as an early
  default. Instead, months-on-book is computed directly from calendar dates
  (`monthly_reporting_period` − origination `first_payment_date`), which is immune to
  modification resets. Good "found a subtle correctness bug before it shipped" story.
- `fully_observed_24m` separates loans that were actually observable for the full
  24-month window from right-censored loans (prepaid/paid off early, data ends early) —
  avoids silently treating "we don't know" as "didn't default."

## Multi-vintage design

- Dataset spans 4 origination vintages — 2007 (pre-crisis), 2012 (post-crisis
  recovery), 2016 (low-rate), 2022 (rate-hiking cycle) — 12,500 loans/quarter × 4
  quarters × 4 years = 200,000 loans total. Deliberately chosen to span different
  macro/rate regimes rather than a single snapshot, which matters for a PD model since
  default base rates and rate-spread dynamics vary a lot by vintage (see LTV finding
  below).

## Feature engineering (`sql/features.sql`)

- **Sentinel cleanup**: Freddie Mac encodes "not available" as out-of-range sentinel
  codes, not NULL — `credit_score = 9999`, `ltv/cltv = 999`, `dti = 999`,
  `first_time_homebuyer_flag = '9'`. Converted all to NULL before banding, with an
  explicit "Not available" bucket in every band so missingness stays visible instead of
  being read as extreme risk (a 9999 FICO score would otherwise look catastrophic).
- **Origination year/quarter parsed from `loan_sequence_number`** (e.g.
  `F07Q10000023` → 2007 Q1), not from `first_payment_date`. Freddie Mac assigns the
  sequence number's vintage at origination; `first_payment_date` lands 1–2 months later
  and can slip into the next calendar quarter for loans originated near quarter-end —
  the sequence number is the authoritative cohort.
- **Rate spread proxy**: "spread over the prevailing rate for the vintage" ideally
  means an external benchmark (Freddie Mac's PMMS survey), which isn't in this dataset.
  Used the average `original_interest_rate` within the same origination year/quarter
  cohort (window function) as a stand-in, and documented that as an explicit modeling
  assumption/limitation rather than hiding it.
- **Bands use real GSE risk-tier cutpoints**, not arbitrary round numbers: FICO bands
  match the loan-level-pricing-adjustment (LLPA) grid (620/640/660/680/700/720/740/760/
  780); LTV/CLTV bands mirror the same LLPA structure (60/70/75/80/85/90/95/97 — the
  jumps track MI-requirement and pricing-tier thresholds); DTI bands center on the 36%
  conventional and 43% QM (ability-to-repay) regulatory thresholds. Shows domain
  knowledge, not just "binned into deciles."

## Sanity checks (good "show your rigor" material)

- **1,222 of 200,000 loans (~0.6%) don't get a target** and were root-caused rather
  than shrugged off: split cleanly into two structural buckets —
  - 125 loans whose only performance record predates `first_payment_date` and is
    already flagged `zero_balance_code = 01` (paid off before the first payment was
    ever due) — no data point falls inside the 24-month window.
  - 1,097 loans whose first-ever performance record starts 25–148 months after
    `first_payment_date` — a reporting gap that skips the entire window (a known
    artifact of Freddie Mac's public sample extract, more common in 2012/2016
    vintages).
  Verified every unmatched loan falls into one of these two buckets (zero "should have
  matched but didn't" cases) — confirms `derive_target.sql`'s join logic is correct,
  not buggy.
- **FICO band default rate is cleanly monotonic**: 15.9% (`<620`) straight down to
  0.36% (`780+`), no exceptions — a clean validation the target and feature both behave
  as expected.
- **LTV band default rate is *not* monotonic when pooled across vintages** — it dips at
  90–95%/95–97% before jumping at `>97%`. Diagnosed this as **Simpson's paradox /
  vintage-composition confounding**, not a broken feature: the `>97%` band is 82%
  2012-vintage + 16% 2007-vintage loans with **zero** 2022 loans in it, while the
  90–95%/95–97% bands are majority 2016/2022 (safer, post-crisis-underwriting, low-rate
  vintages). Splitting by vintage restores clean monotonicity in both 2007 alone and
  2012 alone (2012: 0.16% → 2.47% straight up; 2007: 2.65% → 18.53% straight up, one
  small-sample blip at n=93). Strong interview story: shows you don't just eyeball a
  pooled default-rate table and move on — you check whether a confound explains an
  unexpected pattern before concluding the feature or data is broken. Also motivates why
  `origination_year`/`origination_quarter` need to be in the model or the LTV effect
  will look muddier than it is.

## Fixed data bug — column mapping in `build_database.py`

- **`ORIGINATION_COLUMNS` had a phantom `servicer_name` field that doesn't exist in the
  origination file** (only the performance file has one), which shifted every column
  from position 25 onward by one. Root cause confirmed against Freddie Mac's official
  August 2018 General User Guide: the origination file goes straight from Seller Name
  (24) to Super Conforming Flag (25) — no Servicer Name in between. Caught it initially
  by cross-checking observed value domains against the documented spec (e.g. the column
  labeled `property_valuation_method` was a constant `"N"`, which fits Interest-Only
  Indicator's `Y`/`N` domain, not a 1/2/3/4/9 valuation-method code); confirmed and
  corrected after manually rechecking the layout against the source file.
- The **last column (31) isn't Interest Only Indicator — it's Vantage Score**, an
  alternate credit-score field not present in the 2018 doc (added in a later layout
  revision). Post-fix, it's a clean constant `9999` ("Not Available") across all 200,000
  loans in every vintage — consistent with a disclosed-but-not-yet-populated field,
  which fits far better than a numeric sentinel ever did for a Y/N indicator.
- Post-fix sanity check: `super_conforming_flag` is now `Y` for 4,202/200,000 loans
  (2.1%) — a real signal, not a constant — which is exactly the kind of small-but-real
  variance you'd expect from actual super-conforming (high-cost-area jumbo-conforming)
  loans in a national sample, and confirms the corrected mapping.
- Rebuilt `data/freddie_mac.db` and regenerated `loan_target`/`loan_features` after the
  fix; row counts unchanged (200,000 / 198,778) since neither table used the affected
  columns. Good interview material either way: shows the value of checking a claimed
  schema against the raw byte-level data instead of trusting a data dictionary (or a
  first-pass docstring) at face value.
