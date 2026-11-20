CREATE TABLE IF NOT EXISTS dws_dept_daily (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    outpatient_visits BIGINT UNSIGNED NOT NULL,
    outpatient_fee_samples BIGINT UNSIGNED NOT NULL,
    outpatient_total_fee DECIMAL(28,2) NULL,
    fee_status VARCHAR(32) NULL,
    ads_status ENUM('PENDING', 'PASS') NOT NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
