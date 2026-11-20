CREATE TABLE IF NOT EXISTS ops_action_audit (
    action_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NOT NULL,
    action ENUM('source_check', 'extract_run') NOT NULL,
    target VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin,
    batch_date DATE,
    outcome ENUM('STARTED', 'SUCCESS', 'FAILED', 'INDETERMINATE') NOT NULL,
    code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    started_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at TIMESTAMP(6) NULL,
    KEY ix_ops_source (action, target, dataset_id, batch_date, action_id)
) ENGINE=InnoDB;
