-- Feature engineering for the PD model: turn loan_origination into a model-
-- ready feature table, loan_features, using only information knowable on
-- day one (the origination file). No performance data is referenced here.
--
-- Sentinel cleanup: Freddie Mac uses out-of-range sentinel codes for "not
-- available" rather than true NULLs - credit_score = 9999, ltv/cltv = 999,
-- dti = 999, first_time_homebuyer_flag = '9'. Left as-is, these sentinels
-- would look like extreme high-risk values (e.g. a 9999 FICO score) and
-- corrupt both the bands and any downstream model. We convert each to NULL
-- before banding, and every band CASE expression has an explicit
-- 'Not available' bucket so missingness is visible as its own category
-- rather than silently dropped.
--
-- Origination year/quarter: derived from the loan_sequence_number (e.g.
-- 'F07Q10000023' -> 2007, Q1), not from first_payment_date. Freddie Mac
-- assigns the sequence number's vintage at origination, whereas
-- first_payment_date can land 1-2 months later and cross a quarter
-- boundary for loans originated near quarter-end - the sequence number is
-- the authoritative origination cohort.
--
-- Rate spread: "prevailing rate for the vintage" ideally means an external
-- benchmark like Freddie Mac's Primary Mortgage Market Survey (PMMS), which
-- isn't part of this dataset. As a proxy, we use the average
-- original_interest_rate across all loans originated in the same
-- year/quarter cohort in this sample, and express each loan's rate spread
-- relative to that cohort average, in basis points.

DROP TABLE IF EXISTS loan_features;

CREATE TABLE loan_features AS
WITH cleaned AS (
    SELECT
        loan_sequence_number,
        CAST(SUBSTR(loan_sequence_number, 2, 2) AS INTEGER) + 2000 AS origination_year,
        CAST(SUBSTR(loan_sequence_number, 5, 1) AS INTEGER) AS origination_quarter,
        CASE WHEN credit_score = 9999 THEN NULL ELSE credit_score END AS credit_score,
        CASE WHEN ltv = 999 THEN NULL ELSE ltv END AS ltv,
        CASE WHEN cltv = 999 THEN NULL ELSE cltv END AS cltv,
        CASE WHEN dti = 999 THEN NULL ELSE dti END AS dti,
        loan_purpose,
        occupancy_status,
        property_type,
        CASE WHEN first_time_homebuyer_flag IN ('Y', 'N') THEN first_time_homebuyer_flag ELSE NULL END
            AS first_time_homebuyer_flag,
        original_interest_rate
    FROM loan_origination
),
with_vintage_rate AS (
    SELECT
        *,
        AVG(original_interest_rate) OVER (
            PARTITION BY origination_year, origination_quarter
        ) AS vintage_avg_interest_rate
    FROM cleaned
)
SELECT
    loan_sequence_number,
    origination_year,
    origination_quarter,

    -- FICO
    credit_score,
    CASE
        WHEN credit_score IS NULL THEN 'Not available'
        WHEN credit_score < 620 THEN '<620'
        WHEN credit_score < 640 THEN '620-639'
        WHEN credit_score < 660 THEN '640-659'
        WHEN credit_score < 680 THEN '660-679'
        WHEN credit_score < 700 THEN '680-699'
        WHEN credit_score < 720 THEN '700-719'
        WHEN credit_score < 740 THEN '720-739'
        WHEN credit_score < 760 THEN '740-759'
        WHEN credit_score < 780 THEN '760-779'
        ELSE '780+'
    END AS fico_band,

    -- Original LTV
    ltv,
    CASE
        WHEN ltv IS NULL THEN 'Not available'
        WHEN ltv <= 60 THEN '<=60'
        WHEN ltv <= 70 THEN '60-70'
        WHEN ltv <= 75 THEN '70-75'
        WHEN ltv <= 80 THEN '75-80'
        WHEN ltv <= 85 THEN '80-85'
        WHEN ltv <= 90 THEN '85-90'
        WHEN ltv <= 95 THEN '90-95'
        WHEN ltv <= 97 THEN '95-97'
        ELSE '>97'
    END AS ltv_band,

    -- Original CLTV (includes any simultaneous second lien)
    cltv,
    CASE
        WHEN cltv IS NULL THEN 'Not available'
        WHEN cltv <= 60 THEN '<=60'
        WHEN cltv <= 70 THEN '60-70'
        WHEN cltv <= 75 THEN '70-75'
        WHEN cltv <= 80 THEN '75-80'
        WHEN cltv <= 85 THEN '80-85'
        WHEN cltv <= 90 THEN '85-90'
        WHEN cltv <= 95 THEN '90-95'
        WHEN cltv <= 97 THEN '95-97'
        ELSE '>97'
    END AS cltv_band,

    -- DTI
    dti,
    CASE
        WHEN dti IS NULL THEN 'Not available'
        WHEN dti <= 20 THEN '<=20'
        WHEN dti <= 30 THEN '20-30'
        WHEN dti <= 36 THEN '30-36'
        WHEN dti <= 43 THEN '36-43'
        WHEN dti <= 50 THEN '43-50'
        ELSE '>50'
    END AS dti_band,

    -- Categoricals, kept as Freddie Mac's raw codes (small, well-known
    -- domains - C/N/P, P/S/I, SF/PU/CO/CP/MH):
    loan_purpose,               -- C = cash-out refi, N = no cash-out refi, P = purchase
    occupancy_status,           -- P = primary, S = second home, I = investment
    property_type,              -- SF, PU, CO, CP, MH
    first_time_homebuyer_flag,  -- Y / N / NULL (not available)

    -- Rate and vintage spread
    original_interest_rate,
    ROUND(vintage_avg_interest_rate, 4) AS vintage_avg_interest_rate,
    ROUND((original_interest_rate - vintage_avg_interest_rate) * 100, 1) AS rate_spread_bps

FROM with_vintage_rate;

CREATE UNIQUE INDEX idx_features_lsn ON loan_features(loan_sequence_number);
