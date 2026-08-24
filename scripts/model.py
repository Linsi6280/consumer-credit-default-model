# %%
"""
Models the probability that a mortgage borrower reaches 90+ days delinquent
within 24 months of origination (the target derived in loan_target), using
only information knowable on day one (loan_features - the origination
file). No performance data is used as a feature anywhere below.

Two models, deliberately contrasted:
  1. Logistic regression (statsmodels) on the hand-built risk bands from
     sql/features.sql - the model a credit risk team would actually sign
     off on, because every coefficient is a readable odds ratio with a
     p-value attached.
  2. LightGBM gradient boosted trees on the raw continuous fields - a
     challenger model that can find nonlinearities and interactions the
     manual bands miss, at the cost of interpretability.

Train/test split is by ORIGINATION VINTAGE, not random:
  - Train pool:  2007 + 2012 + 2016 vintages (pre-crisis, post-crisis
                 recovery, low-rate regimes)
  - Test:        2022 vintage (rate-hiking regime) - held out completely,
                 never touched during fitting or tuning
  - Val (GBM only): a random 15% carved out of the TRAIN pool, used solely
                 for early stopping. This never touches the 2022 test
                 vintage, so it doesn't leak the evaluation regime - it's
                 just how the challenger model decides when to stop adding
                 trees.

A random row-level split would let the model see the same macro regime
(rate environment, underwriting standards) in both train and test, which
inflates every metric relative to how the model would actually perform on
a genuinely new vintage - the real deployment scenario for a PD model.

Features EXCLUDED on purpose:
  - origination_year / origination_quarter: 2022 is a category the model
    never sees during training - including it as a predictor would be
    meaningless (or actively misleading) for the one vintage we actually
    care about generalizing to.
  - original_interest_rate / vintage_avg_interest_rate: the absolute rate
    level is almost entirely a MACRO signal (a 2012 loan at 3.5% vs a 2022
    loan at 6% reflects the Fed, not the borrower). Feeding it to the model
    would let it partially reconstruct "which vintage is this," defeating
    the point of testing generalization to an unseen regime.
    rate_spread_bps (the borrower's rate minus their own vintage's average)
    is kept instead - it's macro-normalized by construction.
"""
import sqlite3
import warnings
from pathlib import Path

import lightgbm as lgb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.metrics import roc_auc_score, roc_curve
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parent.parent
con = sqlite3.connect(ROOT / "data" / "freddie_mac.db")

PLOTS_DIR = ROOT / "plots"
OUTPUT_DIR = ROOT / "output"
PLOTS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 20)
# This LightGBM build (4.7.0) warns that the list-of-tuples `eval_set` arg
# is being replaced by `eval_X`/`eval_y`; both still work identically here,
# and eval_set is the form documented/used across other LightGBM versions,
# so it's kept and the noisy warning is silenced rather than chasing a
# very-new API on a single point release.
warnings.filterwarnings("ignore", category=lgb.basic.LGBMDeprecationWarning)

RANDOM_STATE = 42
TRAIN_VINTAGES = [2007, 2012, 2016]
TEST_VINTAGE = 2022

# Chart palette - validated categorical palette (dataviz skill default).
BLUE = "#2a78d6"      # logistic regression
ORANGE = "#eb6834"    # LightGBM challenger
GRID = "#e1e0d9"
MUTED = "#898781"
INK = "#0b0b0b"
SECONDARY = "#52514e"
plt.rcParams.update({
    "figure.facecolor": "#fcfcfb",
    "axes.facecolor": "#fcfcfb",
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": SECONDARY,
    "ytick.color": SECONDARY,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "font.size": 11,
    "font.family": "sans-serif",
})

# %% ------------------------ Load modeling data -----------------------------
# Restricted to loans actually observed for the full 24-month window
# (fully_observed_24m = 1). Censored loans (prepaid, or the data just ends
# early without ever hitting 90+ DPD) are dropped rather than labeled
# "good" - we genuinely don't know their outcome, and mislabeling them
# would bias the model toward "safe."
df = pd.read_sql_query(
    """
    SELECT f.*, t.default_flag_24m
    FROM loan_features f
    JOIN loan_target t USING (loan_sequence_number)
    WHERE t.fully_observed_24m = 1
    """,
    con,
)
print(f"Modeling population: {len(df):,} loans "
      f"({df['default_flag_24m'].mean():.2%} default rate)\n")

is_train_pool = df["origination_year"].isin(TRAIN_VINTAGES)
is_test = df["origination_year"] == TEST_VINTAGE

print("Train pool by vintage:")
print(
    df[is_train_pool]
    .groupby("origination_year")["default_flag_24m"]
    .agg(n_loans="size", default_rate="mean")
)
print(f"\nTest vintage {TEST_VINTAGE}: {is_test.sum():,} loans, "
      f"{df.loc[is_test, 'default_flag_24m'].mean():.2%} default rate")

y_train = df.loc[is_train_pool, "default_flag_24m"].reset_index(drop=True)
y_test = df.loc[is_test, "default_flag_24m"].reset_index(drop=True)

# %% ------------- Logistic regression feature matrix (statsmodels) ---------
# Credit-scorecard style: hand-built risk bands (real GSE LLPA cutpoints,
# see sql/features.sql) as dummy variables. The reference category for
# every band is the SAFEST tier, so every fitted coefficient reads directly
# as "how much riskier than the best borrowers."
#
# ltv_band is deliberately left out: 91% of loans in this sample have
# ltv == cltv (no simultaneous second lien), so the ltv_band and cltv_band
# dummies are near-duplicates of each other for most of the data. Fitting
# both made the Hessian numerically singular (statsmodels couldn't invert
# it to get standard errors) - a textbook multicollinearity symptom, not a
# data bug. CLTV is the more complete leverage measure (it folds in any
# second lien on top of the first), so it's kept and LTV is dropped rather
# than the other way around.
LR_REFERENCE = {
    "fico_band": "780+",
    "cltv_band": "<=60",
    "dti_band": "<=20",
    "loan_purpose": "P",            # purchase
    "occupancy_status": "P",        # primary residence
    "property_type": "SF",          # single family
    "first_time_homebuyer_flag": "N",
}


def one_hot(series, reference):
    """One-hot encode a categorical column, dropping `reference` as the
    baseline so every remaining dummy reads relative to it. Only adds an
    explicit "_missing" indicator column if the series actually has NaNs -
    pandas' dummy_na=True always emits that column, even an all-zero one
    when there's no missingness, which silently makes the design matrix
    rank-deficient (a constant-zero column is a trivial linear dependency)."""
    has_na = series.isna().any()
    d = pd.get_dummies(series, prefix=series.name, dummy_na=has_na)
    d = d.drop(columns=[f"{series.name}_{reference}"])
    if has_na:
        d = d.rename(columns={f"{series.name}_nan": f"{series.name}_missing"})
    return d.astype(int)


# fico_band / cltv_band each have a tiny "Not available" bucket (39 and 4
# loans respectively, out of 170,661 - the credit_score/cltv sentinel is
# almost always populated), and first_time_homebuyer_flag is missing for
# only 15 loans overall (13 in the train pool). All of these tiny buckets
# have ZERO defaults among them. Left as their own dummies, statsmodels
# can't estimate a finite coefficient for a category with no events at all
# (complete separation - the MLE for that coefficient is -infinity, which
# is exactly what showed up as an exploding coefficient/std error on
# first_time_homebuyer_flag_missing before this fix). Since there's no way
# to estimate a meaningful effect from that few rows anyway, they're folded
# into the reference (safest/most common) tier for the LR design matrix
# only; dti_band's "Not available" bucket (19,225 loans, a real ~2%
# prevalence) has plenty of both classes and is kept as its own dummy.
lr_df = df.copy()
for col in ["fico_band", "cltv_band"]:
    lr_df[col] = lr_df[col].replace("Not available", LR_REFERENCE[col])
lr_df["first_time_homebuyer_flag"] = lr_df["first_time_homebuyer_flag"].fillna(
    LR_REFERENCE["first_time_homebuyer_flag"]
)

lr_parts = [one_hot(lr_df[col], ref) for col, ref in LR_REFERENCE.items()]
lr_parts.append((lr_df[["rate_spread_bps"]] / 100).rename(
    columns={"rate_spread_bps": "rate_spread_pctpt"}))
X_lr_full = sm.add_constant(pd.concat(lr_parts, axis=1))

X_lr_train = X_lr_full[is_train_pool].reset_index(drop=True).astype(float)
X_lr_test = X_lr_full[is_test].reset_index(drop=True).astype(float)

logit_result = sm.Logit(y_train.astype(float), X_lr_train).fit(maxiter=100)
print(logit_result.summary())
(OUTPUT_DIR / "logistic_regression_summary.txt").write_text(str(logit_result.summary()))

# Odds ratios are the readable version of the raw log-odds coefficients:
# odds_ratio = 2.0 means "this group's odds of default are double the
# 780+/<=60 LTV/purchase/primary-residence reference borrower's," holding
# everything else fixed.
or_table = pd.DataFrame({
    "odds_ratio": np.exp(logit_result.params),
    "ci_low_95": np.exp(logit_result.conf_int()[0]),
    "ci_high_95": np.exp(logit_result.conf_int()[1]),
    "p_value": logit_result.pvalues,
}).drop(index="const").sort_values("odds_ratio", ascending=False)
print("\nOdds ratios relative to the reference borrower "
      "(780+ FICO, <=60 LTV/CLTV, <=20 DTI, purchase, primary residence, "
      "not first-time buyer):")
print(or_table.round(3))
or_table.round(4).to_csv(OUTPUT_DIR / "logit_odds_ratios.csv")

p_lr_train = logit_result.predict(X_lr_train)
p_lr_test = logit_result.predict(X_lr_test)

# %% ------------------- LightGBM feature matrix (challenger) ---------------
# Raw continuous fields instead of bands - trees find their own split
# points, so there's no need to hand-bin FICO/LTV/DTI the way the logistic
# regression requires. Missing values (the sentinel-cleaned NULLs from
# sql/features.sql) are passed through as NaN; LightGBM routes them natively
# instead of needing an imputed value.
GBM_NUMERIC = ["credit_score", "ltv", "cltv", "dti", "rate_spread_bps"]
GBM_CATEGORICAL = ["loan_purpose", "occupancy_status", "property_type",
                    "first_time_homebuyer_flag"]

X_gbm_full = df[GBM_NUMERIC + GBM_CATEGORICAL].copy()
for c in GBM_CATEGORICAL:
    X_gbm_full[c] = X_gbm_full[c].astype("category")

X_gbm_train_pool = X_gbm_full[is_train_pool].reset_index(drop=True)
X_gbm_test = X_gbm_full[is_test].reset_index(drop=True)

# Random validation carve-out from the TRAIN pool only (never 2022) - used
# purely for early stopping, not for evaluation.
X_gbm_fit, X_gbm_val, y_fit, y_val = train_test_split(
    X_gbm_train_pool, y_train, test_size=0.15, stratify=y_train,
    random_state=RANDOM_STATE,
)

gbm = lgb.LGBMClassifier(
    n_estimators=2000,
    learning_rate=0.03,
    num_leaves=31,
    min_child_samples=100,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=RANDOM_STATE,
    verbosity=-1,
)
gbm.fit(
    X_gbm_fit, y_fit,
    eval_set=[(X_gbm_val, y_val)],
    eval_metric="auc",
    callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
)
print(f"\nLightGBM stopped at {gbm.best_iteration_} trees "
      f"(early-stopping validation AUC {gbm.best_score_['valid_0']['auc']:.4f})")

importances = (
    pd.Series(gbm.feature_importances_, index=X_gbm_fit.columns, name="split_gain")
    .rename_axis("feature")
    .sort_values(ascending=False)
)
print("\nLightGBM feature importance (split gain):")
print(importances)
importances.to_csv(OUTPUT_DIR / "lightgbm_feature_importance.csv")

p_gbm_train = gbm.predict_proba(X_gbm_train_pool)[:, 1]
p_gbm_test = gbm.predict_proba(X_gbm_test)[:, 1]

# %% --------------------------- Evaluation utilities ------------------------

def ks_statistic(y_true, y_score):
    """KS statistic = max separation between the true-positive and
    false-positive rate curves across all thresholds - the standard credit-
    risk measure of how well a score separates goods from bads."""
    fpr, tpr, _ = roc_curve(y_true, y_score)
    return float(np.max(tpr - fpr))


def gains_lift_table(y_true, y_score, n_bins=10):
    """Rank loans from highest to lowest predicted risk, split into n_bins
    equal-sized deciles (decile 1 = riskiest 10%), and report how well the
    model concentrates actual defaults into the top deciles."""
    d = pd.DataFrame({"y": np.asarray(y_true), "score": np.asarray(y_score) * 100})
    d = d.sort_values("score", ascending=False).reset_index(drop=True)
    d["decile"] = np.arange(len(d)) * n_bins // len(d) + 1

    overall_rate_pct = d["y"].mean() * 100
    g = d.groupby("decile").agg(
        n_loans=("y", "size"),
        n_defaults=("y", "sum"),
        min_predicted_pct=("score", "min"),
        max_predicted_pct=("score", "max"),
    )
    g["default_rate_pct"] = 100 * g["n_defaults"] / g["n_loans"]
    g["lift"] = g["default_rate_pct"] / overall_rate_pct
    g["pct_defaults_captured"] = 100 * g["n_defaults"].cumsum() / g["n_defaults"].sum()
    g["pct_population"] = 100 * g["n_loans"].cumsum() / g["n_loans"].sum()
    g["cum_lift"] = g["pct_defaults_captured"] / g["pct_population"]
    return g


def calibration_table(y_true, y_score, n_bins=10):
    d = pd.DataFrame({"y": np.asarray(y_true), "score": np.asarray(y_score)})
    d["bin"] = pd.qcut(d["score"], n_bins, duplicates="drop")
    return (
        d.groupby("bin", observed=True)
        .agg(n=("y", "size"), mean_predicted=("score", "mean"), mean_actual=("y", "mean"))
        .reset_index(drop=True)
    )


def report(name, y_true, y_score):
    auc = roc_auc_score(y_true, y_score)
    ks = ks_statistic(y_true, y_score)
    print(f"{name:<45s}  AUC = {auc:.4f}   KS = {ks:.4f}")
    return auc, ks


# %% --------------------------- AUC / KS by split ----------------------------
print("\n" + "=" * 78)
print("AUC / KS by split (train pool = 2007+2012+2016, test = held-out 2022)")
print("=" * 78)
metrics_rows = []
for split_name, y_s, p_lr, p_gbm in [
    ("train pool", y_train, p_lr_train, p_gbm_train),
    (f"test ({TEST_VINTAGE}, held out)", y_test, p_lr_test, p_gbm_test),
]:
    auc_lr, ks_lr = report(f"Logistic regression - {split_name}", y_s, p_lr)
    auc_gbm, ks_gbm = report(f"LightGBM - {split_name}", y_s, p_gbm)
    metrics_rows += [
        {"split": split_name, "model": "logistic_regression", "auc": auc_lr, "ks": ks_lr},
        {"split": split_name, "model": "lightgbm", "auc": auc_gbm, "ks": ks_gbm},
    ]
metrics_df = pd.DataFrame(metrics_rows)
metrics_df.to_csv(OUTPUT_DIR / "model_comparison_metrics.csv", index=False)

# %% --------------------- Gains / lift table by decile (test) ---------------
print(f"\nLogistic regression - gains/lift table, test vintage {TEST_VINTAGE}")
lr_gains = gains_lift_table(y_test, p_lr_test)
print(lr_gains.round(2))
lr_gains.round(4).to_csv(OUTPUT_DIR / f"gains_table_logit_{TEST_VINTAGE}.csv")

print(f"\nLightGBM - gains/lift table, test vintage {TEST_VINTAGE}")
gbm_gains = gains_lift_table(y_test, p_gbm_test)
print(gbm_gains.round(2))
gbm_gains.round(4).to_csv(OUTPUT_DIR / f"gains_table_lightgbm_{TEST_VINTAGE}.csv")

# %% --------------------------- Plot: ROC curve ------------------------------
fig, ax = plt.subplots(figsize=(6, 6))
for label, p, color in [
    ("Logistic regression", p_lr_test, BLUE),
    ("LightGBM", p_gbm_test, ORANGE),
]:
    fpr, tpr, _ = roc_curve(y_test, p)
    auc = roc_auc_score(y_test, p)
    ax.plot(fpr, tpr, color=color, linewidth=2, label=f"{label} (AUC {auc:.3f})")
ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1.5, linestyle="--", label="Random")
ax.set_xlabel("False positive rate")
ax.set_ylabel("True positive rate")
ax.set_title(f"ROC curve — held-out {TEST_VINTAGE} vintage")
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.legend(loc="lower right", frameon=False)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(PLOTS_DIR / "roc_curve.png", dpi=150)
plt.close(fig)

# %% --------------------------- Plot: cumulative gains -----------------------
fig, ax = plt.subplots(figsize=(6, 6))
for label, g, color in [
    ("Logistic regression", lr_gains, BLUE),
    ("LightGBM", gbm_gains, ORANGE),
]:
    x = np.concatenate([[0], g["pct_population"].values])
    y = np.concatenate([[0], g["pct_defaults_captured"].values])
    ax.plot(x, y, color=color, linewidth=2, marker="o", markersize=4, label=label)
ax.plot([0, 100], [0, 100], color=MUTED, linewidth=1.5, linestyle="--", label="Random")
ax.set_xlabel("% of loans, ranked highest → lowest predicted risk")
ax.set_ylabel("% of actual defaults captured")
ax.set_title(f"Cumulative gains — held-out {TEST_VINTAGE} vintage")
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.legend(loc="lower right", frameon=False)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(PLOTS_DIR / "cumulative_gains.png", dpi=150)
plt.close(fig)

# %% --------------------------- Plot: calibration ----------------------------
cal_lr = calibration_table(y_test, p_lr_test)
cal_gbm = calibration_table(y_test, p_gbm_test)
max_val = 100 * max(
    cal_lr[["mean_predicted", "mean_actual"]].values.max(),
    cal_gbm[["mean_predicted", "mean_actual"]].values.max(),
)

fig, ax = plt.subplots(figsize=(6, 6))
for label, cal, color in [
    ("Logistic regression", cal_lr, BLUE),
    ("LightGBM", cal_gbm, ORANGE),
]:
    ax.plot(cal["mean_predicted"] * 100, cal["mean_actual"] * 100, color=color,
            linewidth=2, marker="o", markersize=5, label=label)
lims = [0, max_val * 1.1]
ax.plot(lims, lims, color=MUTED, linewidth=1.5, linestyle="--", label="Perfect calibration")
ax.set_xlim(lims)
ax.set_ylim(lims)
ax.set_xlabel("Mean predicted default probability (%)")
ax.set_ylabel("Observed default rate (%)")
ax.set_title(f"Calibration — held-out {TEST_VINTAGE} vintage (deciles)")
ax.legend(loc="upper left", frameon=False)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(PLOTS_DIR / "calibration_plot.png", dpi=150)
plt.close(fig)

print(f"\nSaved charts to {PLOTS_DIR}")
print(f"Saved tables to {OUTPUT_DIR}")
