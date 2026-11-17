-- Run on selected hospital / hospital_test. Metadata only, no business values.
WITH expected AS (
    SELECT 'ods_his_outpatient_visit' AS table_name, 11 AS business_columns,
           JSON_ARRAY('visit_id','patient_id','patient_name','id_card','phone','gender','age',
                      'dept_code','visit_date','visit_type','fee_amount') AS business_names
    UNION ALL SELECT 'ods_his_inpatient_visit', 16,
           JSON_ARRAY('visit_id','patient_id','patient_name','id_card','phone','gender','age',
                      'dept_code','bed_no','admit_date','discharge_date','stay_days','total_fee',
                      'drug_fee','consumable_fee','disease_code')
    UNION ALL SELECT 'ods_his_charge_detail', 9,
           JSON_ARRAY('charge_id','visit_id','patient_id','visit_type','dept_code','fee_date',
                      'fee_type_code','fee_type_name','amount')
    UNION ALL SELECT 'ods_emr_record', 7,
           JSON_ARRAY('record_id','patient_id','visit_id','dept_code','visit_date','diagnosis','disease_code')
    UNION ALL SELECT 'ods_lis_report', 6,
           JSON_ARRAY('report_id','patient_id','visit_id','item_name','result_text','report_date')
    UNION ALL SELECT 'ods_finance_excel', 6,
           JSON_ARRAY('record_id','stat_date','dept_code','revenue','drug_revenue','consumable_revenue')
)
SELECT e.table_name,
       IF(t.ENGINE = 'InnoDB' AND t.TABLE_COLLATION = 'utf8mb4_bin', 'OK', 'FAIL') AS storage_status,
       IF(COUNT(c.COLUMN_NAME) = e.business_columns + 5, 'OK', 'FAIL') AS column_count_status,
       IF(SUM(c.DATA_TYPE = 'text' AND c.COLLATION_NAME = 'utf8mb4_bin') = e.business_columns,
          'OK', 'FAIL') AS business_type_status,
       IF(SUM(c.COLUMN_NAME = 'id' AND c.COLUMN_TYPE = 'bigint unsigned' AND c.EXTRA = 'auto_increment') = 1
          AND SUM(c.COLUMN_NAME = 'dataset_id' AND c.DATA_TYPE = 'varchar'
                  AND c.CHARACTER_MAXIMUM_LENGTH = 128 AND c.COLLATION_NAME = 'utf8mb4_bin'
                  AND c.IS_NULLABLE = 'NO') = 1
          AND SUM(c.COLUMN_NAME = 'source_row_number' AND c.COLUMN_TYPE = 'bigint unsigned'
                  AND c.IS_NULLABLE = 'NO') = 1
          AND SUM(c.COLUMN_NAME = 'etl_batch_date' AND c.DATA_TYPE = 'date' AND c.IS_NULLABLE = 'NO') = 1
          AND SUM(c.COLUMN_NAME = 'etl_load_time' AND c.DATA_TYPE = 'datetime'
                  AND c.DATETIME_PRECISION = 6 AND c.IS_NULLABLE = 'NO') = 1,
          'OK', 'FAIL') AS metadata_status,
       IF(SUM(JSON_CONTAINS(e.business_names, JSON_QUOTE(c.COLUMN_NAME))) = e.business_columns,
          'OK', 'FAIL') AS business_name_status
FROM expected e
LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = DATABASE() AND t.TABLE_NAME = e.table_name
LEFT JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA = DATABASE() AND c.TABLE_NAME = e.table_name
GROUP BY e.table_name, e.business_columns, t.ENGINE, t.TABLE_COLLATION
ORDER BY e.table_name;

SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE,
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS index_columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME IN ('ods_his_outpatient_visit', 'ods_his_inpatient_visit', 'ods_his_charge_detail',
                     'ods_emr_record', 'ods_lis_report', 'ods_finance_excel')
GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE
ORDER BY TABLE_NAME, INDEX_NAME;

SELECT t.TABLE_NAME, t.ENGINE, t.TABLE_COLLATION,
       IF(COUNT(c.COLUMN_NAME) = 5 AND SUM(c.IS_NULLABLE = 'NO' AND
          CASE CAST(c.COLUMN_NAME AS BINARY)
              WHEN 'name' THEN c.COLUMN_TYPE = 'varchar(64)' AND c.COLLATION_NAME = 'utf8mb4_bin'
              WHEN 'kind' THEN c.COLUMN_TYPE = 'varchar(8)' AND c.COLLATION_NAME = 'utf8mb4_bin'
              WHEN 'file_path' THEN c.COLUMN_TYPE = 'text' AND c.COLLATION_NAME = 'utf8mb4_bin'
              WHEN 'dataset_id' THEN c.COLUMN_TYPE = 'varchar(128)' AND c.COLLATION_NAME = 'utf8mb4_bin'
              WHEN 'etl_batch_date' THEN c.COLUMN_TYPE = 'date'
              ELSE FALSE
          END) = 5, 'OK', 'FAIL') AS fields_status,
       IF((SELECT COUNT(*) = 1
                  AND SUM(CAST(s.COLUMN_NAME AS BINARY) = 'name'
                          AND s.SEQ_IN_INDEX = 1 AND s.NON_UNIQUE = 0) = 1
           FROM information_schema.STATISTICS s
           WHERE s.TABLE_SCHEMA = DATABASE() AND s.TABLE_NAME = 'source_config'
             AND s.INDEX_NAME = 'PRIMARY'), 'OK', 'FAIL') AS primary_key_status
FROM information_schema.TABLES t
LEFT JOIN information_schema.COLUMNS c ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_NAME = 'source_config'
GROUP BY t.TABLE_NAME, t.ENGINE, t.TABLE_COLLATION;
