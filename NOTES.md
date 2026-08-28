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
- **Feature set expanded** (`number_of_units`, `number_of_borrowers`, `mi_pct`,
  `original_upb`, `channel`) after a review of what else was sitting in
  `loan_origination` but never made it into `loan_features`. Added to **both** models,
  not just LightGBM, on purpose: giving the challenger model extra information the
  linear model never sees would confound "which model is better" with "which model had
  more data" — the point of the comparison is to isolate model *flexibility*
  (nonlinearity/interactions), so both models get the same information, just encoded
  differently per their needs (bands/dummies for the LR, raw for LightGBM — same
  pattern already used for FICO/LTV/CLTV/DTI). Two encoding choices worth calling out:
  - `number_of_borrowers_band` collapses to `1` vs. `2+` rather than splitting out 3/4
    borrowers as their own tiers. Reason beyond tidiness: 3- and 4-borrower loans exist
    **only in the 2022 test vintage** in this sample (zero in 2007/2012/2016) — keeping
    them as separate dummies would mean the logistic regression sees a category at test
    time it never trained on, the identical problem `origination_year` has. Collapsing
    into `2+` (which is well-populated in every vintage) sidesteps it.
  - `original_upb` is left **unbanded** — a continuous `log_original_upb` term instead
    of GSE-style bands, since (unlike FICO/LTV/CLTV/DTI) there's no official risk-tier
    grid for loan size, and the empirical default rate by dollar band isn't cleanly
    monotonic (flat ~2.4–2.6% from `<=100k` through `300–417k`, only dropping at
    `>417k`) — forcing bands onto it would manufacture structure that isn't really
    there.
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
  tiny "Not available" buckets (39 and 4 loans in the train pool),
  `first_time_homebuyer_flag`'s missing value (13 loans in the train pool),
  `mi_pct_band`'s "Not available" bucket (2 loans total), and
  `number_of_borrowers_band`'s "Not available" bucket (23 loans, all 2007 vintage) all
  have *zero* defaults among them — statsmodels can't estimate a finite coefficient for
  a category with no events (the MLE is literally -∞; showed up as a coefficient of -60
  with a std error of 3×10¹³ before the fix). Folded into each variable's reference
  category for the logistic regression only, since there's no way to estimate a
  meaningful effect from that few rows anyway. `dti_band`'s "Not available" bucket
  (19,225 loans, a real ~2% default rate) and `number_of_units_band`'s "Not available"
  bucket (52 loans, 1 default) both have enough events to keep as their own dummy.
- **Results, held-out 2022 vintage, after the feature expansion**: logistic regression
  AUC 0.756 / KS 0.395 (down slightly from 0.763 / 0.411 pre-expansion); LightGBM AUC
  0.770 / KS 0.427 (up from 0.747 / 0.389) — **LightGBM now edges out the logistic
  regression on the held-out vintage**, a reversal from the original 9-feature version.
  Both new fields the GBM leans on hardest are exactly the newly added ones:
  `original_upb` ties for 3rd in split-gain importance (behind only `credit_score` and
  `dti`), `number_of_borrowers` is 5th; `number_of_units` contributes almost nothing
  (split gain 3 of ~1,300 total) — consistent with the LR, where
  `number_of_units_band` is nowhere near significant (p = 0.75–0.95) but
  `number_of_borrowers_band` is (p < 0.001, odds ratio 2.19). LightGBM's train-pool AUC
  actually *dropped* slightly (0.892 vs. 0.907 before) and early stopping now kicks in
  at just 49 trees — the new features are informative enough that the model needs less
  raw tree-count to extract the signal, which also shows up as a smaller train/test AUC
  gap (0.122 vs. 0.160 before) than the original run.
- **Why the linear model's out-of-time score slipped**: `log_original_upb` is likely
  the culprit. Average `original_upb` drifts steadily by vintage in this sample — 2007:
  \$182.6k, 2012: \$206.8k, 2016: \$234.0k, 2022: \$299.3k — the same kind of macro/
  inflation drift that motivated normalizing `rate_spread_bps` instead of using the raw
  interest rate. Loan size was included anyway (unlike absolute rate) because it's a
  much weaker, less mechanical proxy for vintage than the Fed-driven rate level is, and
  it's a real information gap otherwise — but it's a plausible, checkable explanation
  for why the LR's out-of-time metrics dipped slightly even though nothing about the
  fitting procedure changed. Good self-critique material: "I made a judgment call to
  keep it despite a smaller version of the same leakage risk I'd already flagged for
  interest rate, and I can point to evidence for why."
- **A genuine Simpson's-paradox-style sign flip on loan size**: univariately, larger
  loans look *safer* (`>417k` band: 1.89% default rate, the lowest of any UPB band).
  But `log_original_upb`'s fitted coefficient in the multivariate logistic regression is
  *positive* (odds ratio 1.61, p < 0.001) — once FICO, CLTV, DTI, and channel are held
  fixed, larger loans read as *riskier*. Read together, this says the univariate
  "bigger loans are safer" pattern was confounded by loan size correlating with lower-
  risk borrower characteristics (stronger FICO, lower leverage) in the raw population;
  once those are controlled for, size itself pushes risk up slightly (a plausible story:
  a bigger loan means a bigger monthly payment and more payment-shock exposure for two
  otherwise-identical borrowers). Same "check for a confound before trusting a marginal
  relationship" instinct as the LTV/vintage finding above, just surfaced by the model
  instead of a manual pivot table this time.
- **`channel` is the strongest new scorecard entry**: relative to Correspondent (`C`,
  the lowest empirical default rate and chosen as the reference), TPO (`T`) carries a
  7.14x odds ratio — by far the largest effect of any single dummy in the model, FICO
  bands included. Retail (`R`) is 2.90x, Broker (`B`) 1.53x. Worth flagging as a data
  quirk during a walkthrough: in this sample, `T` loans are **100% from the 2007
  vintage** (26,720 of them, zero in 2012/2016/2022) — the effect is real (not a
  zero-event artifact, there's plenty of both classes), but its entire evidence base
  comes from one macro regime, so "TPO loans are 7x riskier" should be read as "TPO
  loans *in the pre-crisis vintage in this sample* were 7x riskier," not asserted as a
  channel-intrinsic effect independent of era.
- **Calibration on the 2022 vintage, post feature-expansion**: the miscalibration story
  got more interesting (and more decile-dependent) once `original_upb`/`number_of_borrowers`/
  `channel`/`mi_pct`/`number_of_units` were added — see `output/calibration_table_logit_2022.csv`
  and `..._lightgbm_2022.csv` for the exact numbers behind `plots/calibration_plot.png`.
  Logistic regression now *under*-predicts in 9 of 10 deciles (e.g. decile 1: 0.08%
  predicted vs. 0.22% actual) and only over-predicts in the single riskiest decile
  (7.52% predicted vs. 7.10% actual). LightGBM does the opposite in the safe majority of
  loans — it over-predicts through the bottom 70% (decile 1: 0.82% predicted vs. 0.24%
  actual) — but then **badly under-predicts its own riskiest decile**: 5.24% predicted
  vs. 7.61% observed, a >2-point gap, the single worst miscalibration either model
  produces anywhere in the table. That's a sharper interview point than "the model
  over-predicts everywhere": LightGBM has the better AUC/KS of the two models, but its
  probability scale is least trustworthy in exactly the segment — the top risk decile —
  that any real credit-risk action (manual review, pricing add-ons, reserve sizing)
  would actually be keyed on. Rank-ordering metrics looking strong doesn't mean the
  probabilities are usable as-is; a deployed model needs its calibration checked
  specifically in the decile(s) that drive decisions, not just a top-line AUC number,
  and would need periodic recalibration (e.g. a Platt/isotonic refit against recent
  originations) before those probabilities could be used directly.
- **Odds ratios read as a clean credit scorecard** (reference = 780+ FICO, ≤60% CLTV,
  ≤20% DTI, purchase, primary residence, not first-time buyer, ≤1 unit, 2+ borrowers,
  no MI, Correspondent channel; all p<0.001 unless noted; current numbers, post
  feature-expansion — see `output/logit_odds_ratios.csv` for the full table). FICO
  <620 → 24.1x the reference borrower's odds of default, 620–639 → 16.7x, monotonically
  down to 760–779 → 1.9x; CLTV >97% → 4.0x, monotonically down to 60–70% → 1.5x; DTI
  >50% → 2.2x; cash-out refi → 1.9x vs. purchase; single-borrower → 2.2x vs. 2+
  borrowers; TPO channel → 7.1x vs. Correspondent (see the `channel` note above on
  reading this one with the single-vintage caveat), Retail → 2.9x; heaviest MI coverage
  (26–35%) → 1.5x. The FICO and CLTV odds ratios are noticeably smaller than the
  pre-expansion run (FICO <620 was 33.4x, CLTV >97% was 7.8x) — adding `channel` in
  particular pulled some of the risk that FICO/CLTV were previously absorbing onto
  itself, since channel correlates with both the borrower population and (per the
  single-vintage caveat above) the macro era. A useful reminder that a scorecard's
  individual coefficients are conditional on what else is in the model, not fixed
  properties of the variable. Property type and `number_of_units` mostly don't clear
  significance once FICO/LTV/DTI are in the model — the risk they'd otherwise proxy for
  is already captured directly.
