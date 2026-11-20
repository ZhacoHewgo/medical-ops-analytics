CREATE TABLE IF NOT EXISTS ads_indicator_result (
    dataset_id VARCHAR(128) NOT NULL,
    indicator_code VARCHAR(32) NOT NULL,
    dimension_key VARCHAR(72) NOT NULL,
    granularity ENUM('day', 'month') NOT NULL,
    stat_date DATE NOT NULL,
    source_batch DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    definition_version VARCHAR(32) NOT NULL,
    unit VARCHAR(12) NOT NULL,
    numerator DECIMAL(28,6) NULL,
    denominator DECIMAL(28,6) NULL,
    metric_value DECIMAL(28,6) NULL,
    calculation_status ENUM('OK', 'NULL') NOT NULL,
    null_reason VARCHAR(40) NULL,
    computed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (dataset_id, indicator_code, dimension_key, granularity, stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
