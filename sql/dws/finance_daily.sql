-- A / Issue #1. MySQL 8 read-only draft. No DDL or writes.
-- Bind the approved dataset_id in the client before running either SELECT:
-- SET @finance_dataset_id = '...';
-- A missing or wrong parameter produces zero rows: STOP if source_rows != 910
-- for the documented SHA-256. Review the period/expected count for other versions.
-- Run preflight first. Continue only after verifying dataset fingerprint at import,
-- row count, date bounds and zero invalid rows. Never cast invalid text to zero.
SELECT COUNT(*) AS source_rows, MIN(stat_date) AS first_day, MAX(stat_date) AS last_day,
       COUNT(DISTINCT record_id) AS distinct_record_ids,
       SUM(CASE WHEN TRIM(record_id) = '' OR TRIM(dept_code) = ''
                 OR NOT REGEXP_LIKE(stat_date, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
                 OR NOT REGEXP_LIKE(revenue, '^-?[0-9]{1,16}([.][0-9]{1,2})?$')
                 OR NOT REGEXP_LIKE(drug_revenue, '^-?[0-9]{1,16}([.][0-9]{1,2})?$')
                 OR NOT REGEXP_LIKE(consumable_revenue, '^-?[0-9]{1,16}([.][0-9]{1,2})?$')
                THEN 1 ELSE 0 END) AS invalid_rows
FROM ods_finance_excel
WHERE dataset_id = @finance_dataset_id;

-- Period is the source business date, not etl_batch_date.
SELECT stat_date, dept_code, COUNT(*) AS source_rows,
       SUM(CAST(revenue AS DECIMAL(18,2))) AS revenue,
       SUM(CAST(drug_revenue AS DECIMAL(18,2))) AS drug_revenue,
       SUM(CAST(consumable_revenue AS DECIMAL(18,2))) AS consumable_revenue
FROM ods_finance_excel
WHERE dataset_id = @finance_dataset_id
  AND stat_date >= '2025-04-01' AND stat_date < '2025-07-01'
GROUP BY stat_date, dept_code
ORDER BY stat_date, dept_code;
