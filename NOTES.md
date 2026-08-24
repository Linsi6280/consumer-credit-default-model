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

## Modeling (`scripts/model.py`)

- **Modeling population**: 170,661 of 198,778 loans with a target — restricted to
  `fully_observed_24m = 1`. Loans right-censored before month 24 (prepaid, or the
  performance extract just ends early) are dropped rather than labeled "no default,"
  since we genuinely don't know their outcome and mislabeling them would bias every
  model toward "safe."
- **Vintage-based split, not random**: train pool = 2007 + 2012 + 2016 (125,472 loans);
  test = the entire 2022 vintage (45,189 loans), held out completely from fitting and
  tuning. A random row-level split would put the same macro regime (rate environment,
  underwriting standards) in both train and test, inflating every metric relative to
  how the model would actually perform deployed against a genuinely new vintage — the
  real use case for a PD model built today. LightGBM's early stopping uses a random 15%
  carved out of the *train pool only* (never 2022), so tuning still never touches the
  evaluation regime.
- **Two macro-leakage features excluded on purpose**: `origination_year`/`quarter` (2022
  is a category the model never sees in training — useless or misleading as a
  predictor for the one vintage we care about), and `original_interest_rate` /
  `vintage_avg_interest_rate` (absolute rate level is almost entirely a macro signal —
  a 2012 loan at 3.5% vs. a 2022 loan at 6% reflects the Fed, not the borrower;
  including it would let the model partially reconstruct "which vintage is this,"
  defeating the point of the out-of-time test). `rate_spread_bps` — the borrower's rate
  minus their own vintage's average — is kept instead since it's macro-normalized by
  construction.
- **Two design-matrix bugs caught by a singular-Hessian error, not by eyeballing
  output**: (1) `pandas.get_dummies(..., dummy_na=True)` emits a `*_nan` indicator
  column even when a column has zero actual NaNs — six such all-zero columns made the
  logistic regression's design matrix exactly rank-deficient (`np.linalg.matrix_rank`
  showed 35 vs. 41 columns). Fixed by only requesting `dummy_na` when
  `series.isna().any()`. (2) `ltv_band` and `cltv_band` are near-duplicates of each
  other — 91% of loans have `ltv == cltv` (no simultaneous second lien) — which was
  severe enough multicollinearity to make the Hessian numerically singular even after
  fixing (1). Resolved by dropping `ltv_band` and keeping `cltv_band` alone, since CLTV
  is the more complete leverage measure (it folds in any second lien on top of the
  first).
- **Complete separation from rare zero-event categories**: `fico_band`/`cltv_band`'s
  tiny "Not available" buckets (39 and 4 loans in the train pool) and
  `first_time_homebuyer_flag`'s missing value (13 loans in the train pool) all have
  *zero* defaults among them — statsmodels can't estimate a finite coefficient for a
  category with no events (the MLE is literally -∞; showed up as a coefficient of -60
  with a std error of 3×10¹³ before the fix). Folded into each variable's reference
  category for the logistic regression only, since there's no way to estimate a
  meaningful effect from that few rows anyway. `dti_band`'s "Not available" bucket
  (19,225 loans, a real ~2% default rate) has plenty of both classes and is kept as its
  own dummy.
- **Results, held-out 2022 vintage**: logistic regression AUC 0.763 / KS 0.411;
  LightGBM AUC 0.747 / KS 0.389. LightGBM fits the training pool much better (AUC 0.907
  vs. 0.850) but generalizes slightly *worse* out-of-time — a clean, concrete
  illustration of why flexible models need the out-of-time test more than linear ones:
  the extra flexibility that helps in-sample is partly fitting vintage-specific noise in
  2007/2012/2016 that doesn't transfer to 2022's rate-hiking regime. On rank-ordering
  and decile capture the two models are close (both top decile captures 31–34% of all
  defaults, top 3 deciles ~68%), so the interpretability of the logistic regression
  isn't costing much predictive power here.
- **Calibration drift on the 2022 vintage**: both models systematically over-predict
  risk on 2022 (every decile point sits below the diagonal in `plots/calibration_plot.png`)
  — a model trained on 2007/2012/2016 (average default rate ~2.6%, dragged up by 2007)
  expects a riskier population than 2022 (2.12% observed) actually turned out to be.
  Rank-ordering (AUC/KS/gains) survives the vintage shift far better than the absolute
  probability scale does — a real-world reason a deployed PD model needs periodic
  recalibration (e.g., a Platt/isotonic refit against recent originations) even when its
  risk *ranking* is still sound.
- **Odds ratios read as a clean credit scorecard** (reference = 780+ FICO, ≤60% CLTV,
  ≤20% DTI, purchase, primary residence, not first-time buyer, all p<0.001 unless
  noted): FICO <620 → 33.4x the reference borrower's odds of default, 620–639 → 21.7x,
  monotonically down to 760–779 → 1.9x; CLTV >97% → 7.8x, monotonically down to 60–70%
  → 1.7x; DTI >50% → 3.7x; cash-out refi → 2.2x vs. purchase. Property type and
  occupancy status mostly don't clear significance once FICO/LTV/DTI are in the model —
  the risk they'd otherwise proxy for is already captured directly.
