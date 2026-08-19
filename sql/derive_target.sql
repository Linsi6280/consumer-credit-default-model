-- Derive the PD model target: did the loan reach 90+ days delinquent (or
-- worse) within the first 24 months of its life (calendar months 0-24
-- since the First Payment Date)?
--
-- Current Loan Delinquency Status coding (Freddie Mac MBA method):
--   '00' = current, '01' = 30-59 DPD, '02' = 60-89 DPD, '03' = 90-119 DPD,
--   '04', '05', ... = each additional 30-day bucket, 'RA' = REO Acquisition
--   (loan already foreclosed - by definition worse than 90+ DPD).
-- So "hit 90+ DPD" = status is numeric and >= 3, OR status = 'RA'.
--
-- IMPORTANT: we do NOT use the raw performance-file "loan_age" column to
-- define the window. Per Freddie Mac's spec, Loan Age is computed from the
-- *modification* First Payment Date for modified loans, so it resets to a
-- low number after a loan mod - a loan modified 5 years in could show
-- loan_age = 1 again long after origination. ~2,600 of the 50,000 loans in
-- this vintage have at least one modification, so trusting loan_age would
-- misclassify later-life delinquency as an early default. Instead we
-- compute months-on-book directly from calendar dates
-- (Monthly Reporting Period minus the origination First Payment Date),
-- which is immune to modification resets.

DROP TABLE IF EXISTS loan_target;

CREATE TABLE loan_target AS
WITH loan_calendar AS (
    SELECT
        p.loan_sequence_number,
        p.current_loan_delinquency_status AS status,
        p.zero_balance_code,
        (CAST(p.monthly_reporting_period / 100 AS INTEGER) * 12 + CAST(p.monthly_reporting_period % 100 AS INTEGER))
            - (CAST(o.first_payment_date / 100 AS INTEGER) * 12 + CAST(o.first_payment_date % 100 AS INTEGER))
            AS months_on_book
    FROM loan_performance p
    JOIN loan_origination o USING (loan_sequence_number)
),
window_records AS (
    SELECT
        loan_sequence_number,
        months_on_book,
        CASE
            WHEN status = 'RA' THEN 1
            WHEN status GLOB '[0-9]*' AND CAST(status AS INTEGER) >= 3 THEN 1
            ELSE 0
        END AS bad_this_month
    FROM loan_calendar
    WHERE months_on_book BETWEEN 0 AND 24
),
per_loan AS (
    SELECT
        loan_sequence_number,
        MAX(bad_this_month) AS default_flag_24m,
        MIN(CASE WHEN bad_this_month = 1 THEN months_on_book END) AS months_to_default
    FROM window_records
    GROUP BY loan_sequence_number
),
loan_span AS (
    -- whether each loan actually had 24 months of observable history,
    -- or terminated (any reason) before reaching month 24
    SELECT
        loan_sequence_number,
        MAX(months_on_book) AS max_months_on_book,
        MAX(CASE WHEN zero_balance_code IS NOT NULL AND zero_balance_code != ''
                  THEN 1 ELSE 0 END) AS ever_terminated
    FROM loan_calendar
    GROUP BY loan_sequence_number
)
SELECT
    p.loan_sequence_number,
    p.default_flag_24m,
    p.months_to_default,
    CASE
        WHEN p.default_flag_24m = 1 THEN 1               -- defaulted: fully observed by definition
        WHEN s.max_months_on_book >= 24 THEN 1            -- survived the full window without defaulting
        WHEN s.ever_terminated = 1 THEN 0                 -- prepaid/paid off/etc before month 24: censored
        ELSE 0                                            -- data ends before month 24 for another reason: censored
    END AS fully_observed_24m
FROM per_loan p
JOIN loan_span s USING (loan_sequence_number);

CREATE UNIQUE INDEX idx_target_lsn ON loan_target(loan_sequence_number);
