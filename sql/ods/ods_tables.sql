-- O5 only. Select hospital or hospital_test through injected connection, never USE/DROP DATABASE.
CREATE TABLE IF NOT EXISTS ods_his_outpatient_visit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    visit_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    id_card TEXT NOT NULL,
    phone TEXT NOT NULL,
    gender TEXT NOT NULL,
    age TEXT NOT NULL,
    dept_code TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    visit_type TEXT NOT NULL,
    fee_amount TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ods_his_inpatient_visit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    visit_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    id_card TEXT NOT NULL,
    phone TEXT NOT NULL,
    gender TEXT NOT NULL,
    age TEXT NOT NULL,
    dept_code TEXT NOT NULL,
    bed_no TEXT NOT NULL,
    admit_date TEXT NOT NULL,
    discharge_date TEXT NOT NULL,
    stay_days TEXT NOT NULL,
    total_fee TEXT NOT NULL,
    drug_fee TEXT NOT NULL,
    consumable_fee TEXT NOT NULL,
    disease_code TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ods_his_charge_detail (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    charge_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    visit_type TEXT NOT NULL,
    dept_code TEXT NOT NULL,
    fee_date TEXT NOT NULL,
    fee_type_code TEXT NOT NULL,
    fee_type_name TEXT NOT NULL,
    amount TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ods_emr_record (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    record_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    dept_code TEXT NOT NULL,
    visit_date TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    disease_code TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ods_lis_report (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    report_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    result_text TEXT NOT NULL,
    report_date TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ods_finance_excel (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    record_id TEXT NOT NULL,
    stat_date TEXT NOT NULL,
    dept_code TEXT NOT NULL,
    revenue TEXT NOT NULL,
    drug_revenue TEXT NOT NULL,
    consumable_revenue TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    source_row_number BIGINT UNSIGNED NOT NULL,
    etl_batch_date DATE NOT NULL,
    etl_load_time DATETIME(6) NOT NULL,
    UNIQUE KEY uq_dataset_row (dataset_id, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS source_config (
    name VARCHAR(64) NOT NULL PRIMARY KEY,
    kind VARCHAR(8) NOT NULL,
    file_path TEXT NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    etl_batch_date DATE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
