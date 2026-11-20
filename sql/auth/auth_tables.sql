CREATE TABLE IF NOT EXISTS auth_users (
    user_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
    password_hash VARBINARY(60) NOT NULL,
    role ENUM('admin', 'analyst', 'department') NOT NULL,
    department VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    revision TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    CHECK ((role = 'department' AND department IS NOT NULL AND department <> '')
        OR (role <> 'department' AND department IS NULL))
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS auth_audit (
    audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NULL,
    action ENUM('login') NOT NULL,
    outcome ENUM('success', 'denied') NOT NULL,
    recorded_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS auth_admin_audit (
    audit_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_id BIGINT UNSIGNED NOT NULL,
    target_id BIGINT UNSIGNED NOT NULL,
    action ENUM('create', 'update') NOT NULL,
    previous_role ENUM('admin', 'analyst', 'department'),
    previous_department VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    previous_enabled BOOLEAN,
    role ENUM('admin', 'analyst', 'department') NOT NULL,
    department VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
    enabled BOOLEAN NOT NULL,
    recorded_at TIMESTAMP(6) DEFAULT CURRENT_TIMESTAMP(6) NOT NULL,
    KEY ix_admin_audit_target (target_id, audit_id),
    KEY ix_admin_audit_actor (actor_id, audit_id)
) ENGINE=InnoDB;
