SET @outpatient_department_index = IF(
    EXISTS(SELECT 1 FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ads_outpatient_theme'
             AND INDEX_NAME = 'ix_ads_outpatient_department_date'),
    'DO 0',
    'CREATE INDEX ix_ads_outpatient_department_date ON ads_outpatient_theme (dataset_id, dept_code, stat_date)'
);
PREPARE stmt FROM @outpatient_department_index;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @daily_department_index = IF(
    EXISTS(SELECT 1 FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'dws_dept_daily'
             AND INDEX_NAME = 'ix_dws_department_date'),
    'DO 0',
    'CREATE INDEX ix_dws_department_date ON dws_dept_daily (dataset_id, dept_code, stat_date)'
);
PREPARE stmt FROM @daily_department_index;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
