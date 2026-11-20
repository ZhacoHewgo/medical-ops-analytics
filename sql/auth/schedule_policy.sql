CREATE TABLE IF NOT EXISTS ops_schedule_policy (
    env ENUM('dev', 'test') PRIMARY KEY,
    enabled BOOLEAN NOT NULL,
    run_hour TINYINT UNSIGNED NOT NULL CHECK (run_hour BETWEEN 0 AND 23),
    run_minute TINYINT UNSIGNED NOT NULL CHECK (run_minute BETWEEN 0 AND 59),
    version INT UNSIGNED NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_schedule_audit (
    audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NOT NULL,
    env ENUM('dev', 'test') NOT NULL,
    previous_enabled BOOLEAN NOT NULL,
    previous_hour TINYINT UNSIGNED NOT NULL,
    previous_minute TINYINT UNSIGNED NOT NULL,
    enabled BOOLEAN NOT NULL,
    run_hour TINYINT UNSIGNED NOT NULL,
    run_minute TINYINT UNSIGNED NOT NULL,
    version INT UNSIGNED NOT NULL,
    recorded_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ops_schedule_dispatch (
    env ENUM('dev', 'test') NOT NULL,
    local_day DATE NOT NULL,
    policy_version INT UNSIGNED NOT NULL,
    PRIMARY KEY (env, local_day)
) ENGINE=InnoDB;
