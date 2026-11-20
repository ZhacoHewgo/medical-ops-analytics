CREATE TABLE IF NOT EXISTS ads_inpatient_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    discharges BIGINT UNSIGNED NOT NULL,
    stay_samples BIGINT UNSIGNED NOT NULL,
    discharge_stay_days DECIMAL(28,2) NULL,
    stay_status VARCHAR(48) NULL,
    fee_samples BIGINT UNSIGNED NOT NULL,
    discharge_total_fee DECIMAL(28,2) NULL,
    fee_status VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_income_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    charge_count BIGINT UNSIGNED NOT NULL,
    amount_samples BIGINT UNSIGNED NOT NULL,
    charge_amount DECIMAL(28,2) NULL,
    charge_status VARCHAR(48) NULL,
    net_revenue DECIMAL(28,2) NULL,
    net_revenue_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_drug_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    charge_count BIGINT UNSIGNED NOT NULL,
    amount_samples BIGINT UNSIGNED NOT NULL,
    charge_amount DECIMAL(28,2) NULL,
    charge_status VARCHAR(48) NULL,
    drug_count BIGINT UNSIGNED NULL,
    drug_amount_samples BIGINT UNSIGNED NULL,
    drug_charge_amount DECIMAL(28,2) NULL,
    drug_status VARCHAR(48) NULL,
    drug_quantity DECIMAL(28,2) NULL,
    ddd DECIMAL(28,2) NULL,
    quantity_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_consumable_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    charge_count BIGINT UNSIGNED NOT NULL,
    amount_samples BIGINT UNSIGNED NOT NULL,
    charge_amount DECIMAL(28,2) NULL,
    charge_status VARCHAR(48) NULL,
    consumable_count BIGINT UNSIGNED NULL,
    consumable_amount_samples BIGINT UNSIGNED NULL,
    consumable_charge_amount DECIMAL(28,2) NULL,
    consumable_status VARCHAR(48) NULL,
    consumption_cost DECIMAL(28,2) NULL,
    cost_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_drg_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    discharges BIGINT UNSIGNED NOT NULL,
    source_diagnosis_cases BIGINT UNSIGNED NOT NULL,
    drg_grouped_cases BIGINT UNSIGNED NULL,
    cmi DECIMAL(28,2) NULL,
    grouping_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_efficiency_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    discharges BIGINT UNSIGNED NOT NULL,
    available_bed_days DECIMAL(28,2) NULL,
    occupied_bed_days DECIMAL(28,2) NULL,
    available_reason VARCHAR(48) NULL,
    occupied_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_service_theme (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    dept_code VARCHAR(64) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    outpatient_visits BIGINT UNSIGNED NOT NULL,
    inpatient_admissions BIGINT UNSIGNED NOT NULL,
    emr_link_rate DECIMAL(28,2) NULL,
    appointment_count BIGINT UNSIGNED NULL,
    cancellation_count BIGINT UNSIGNED NULL,
    satisfaction_rate DECIMAL(28,2) NULL,
    lab_turnaround_minutes DECIMAL(28,2) NULL,
    link_reason VARCHAR(48) NULL,
    service_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, stat_date, dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ads_theme_status (
    dataset_id VARCHAR(128) NOT NULL,
    stat_date DATE NOT NULL,
    theme VARCHAR(16) NOT NULL,
    batch_date DATE NOT NULL,
    source_build_run_id CHAR(36) NOT NULL,
    rows_published BIGINT UNSIGNED NOT NULL,
    data_status ENUM('READY', 'PARTIAL', 'HOLD') NOT NULL,
    status_reason VARCHAR(48) NULL,
    PRIMARY KEY (dataset_id, theme, stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
