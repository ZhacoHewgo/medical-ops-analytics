CREATE TABLE `dim_department` (
  `dataset_id` VARCHAR(128) NOT NULL,
  `dept_code` VARCHAR(64) NOT NULL,
  `dept_name` VARCHAR(128) NOT NULL,
  `hierarchy_level` SMALLINT NULL,
  `parent_dept_code` VARCHAR(64) NULL,
  `campus_code` VARCHAR(64) NULL,
  `campus_name` VARCHAR(128) NULL,
  `attribute_status` VARCHAR(16) NOT NULL,
  `mapping_version` VARCHAR(64) NOT NULL,
  `scope` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`dataset_id`,`dept_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dim_date` (
  `dataset_id` VARCHAR(128) NOT NULL,
  `calendar_date` DATE NOT NULL,
  `calendar_year` SMALLINT UNSIGNED NOT NULL,
  `calendar_month` TINYINT UNSIGNED NOT NULL,
  `day_of_month` TINYINT UNSIGNED NOT NULL,
  `iso_week_year` SMALLINT UNSIGNED NOT NULL,
  `iso_week` TINYINT UNSIGNED NOT NULL,
  `iso_weekday` TINYINT UNSIGNED NOT NULL,
  `is_weekend` TINYINT UNSIGNED NOT NULL,
  `is_holiday` TINYINT UNSIGNED NULL,
  `holiday_status` VARCHAR(16) NOT NULL,
  `mapping_version` VARCHAR(64) NOT NULL,
  `scope` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`dataset_id`,`calendar_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dim_disease` (
  `dataset_id` VARCHAR(128) NOT NULL,
  `disease_code` VARCHAR(64) NOT NULL,
  `disease_name` VARCHAR(128) NOT NULL,
  `icd_code` VARCHAR(64) NULL,
  `drg_group` VARCHAR(64) NULL,
  `dip_group` VARCHAR(64) NULL,
  `attribute_status` VARCHAR(16) NOT NULL,
  `mapping_version` VARCHAR(64) NOT NULL,
  `scope` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`dataset_id`,`disease_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dim_fee_type` (
  `dataset_id` VARCHAR(128) NOT NULL,
  `fee_type_code` VARCHAR(64) NOT NULL,
  `fee_type_name` VARCHAR(128) NOT NULL,
  `source_supported` TINYINT UNSIGNED NOT NULL,
  `origin` VARCHAR(16) NOT NULL,
  `mapping_version` VARCHAR(64) NOT NULL,
  `scope` VARCHAR(16) NOT NULL,
  PRIMARY KEY (`dataset_id`,`fee_type_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
