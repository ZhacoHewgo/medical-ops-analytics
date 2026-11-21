CREATE TABLE IF NOT EXISTS source_registry_audit (
    audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NOT NULL,
    previous_version CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    new_version CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    previous_sources JSON NOT NULL,
    dataset_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    batch_date DATE NOT NULL,
    recorded_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY ix_registry_audit_actor (actor_id, audit_id)
) ENGINE=InnoDB;
