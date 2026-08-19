-- Basic look at the combined dataset (loan_origination + loan_target).
-- Mirrors the queries in scripts/explore_dataset.py.

-- Overall default rate
SELECT
    COUNT(*) AS loans,
    SUM(default_flag_24m) AS defaults,
    ROUND(100.0 * SUM(default_flag_24m) / SUM(fully_observed_24m), 2) AS default_rate_pct
FROM loan_target;

-- A few joined rows: origination features next to the outcome
SELECT
    o.loan_sequence_number,
    o.credit_score,
    o.ltv,
    o.dti,
    o.property_state,
    t.default_flag_24m
FROM loan_origination o
JOIN loan_target t USING (loan_sequence_number)
LIMIT 10;

-- Default rate by credit score bucket
SELECT
    CASE
        WHEN o.credit_score < 620 THEN '<620'
        WHEN o.credit_score < 700 THEN '620-699'
        WHEN o.credit_score < 740 THEN '700-739'
        ELSE '740+'
    END AS credit_score_bucket,
    COUNT(*) AS n_loans,
    ROUND(100.0 * SUM(t.default_flag_24m) / COUNT(*), 2) AS default_rate_pct
FROM loan_origination o
JOIN loan_target t USING (loan_sequence_number)
WHERE t.fully_observed_24m = 1
GROUP BY credit_score_bucket
ORDER BY MIN(o.credit_score);
