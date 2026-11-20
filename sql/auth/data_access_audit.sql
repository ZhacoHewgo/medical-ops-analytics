CREATE TABLE IF NOT EXISTS auth_data_access (
    audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NOT NULL,
    resource VARCHAR(48) NOT NULL,
    action ENUM('query', 'export') NOT NULL,
    department VARCHAR(64) NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    recorded_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY ix_actor_time (actor_id, recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
