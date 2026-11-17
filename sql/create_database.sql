-- Creates only the two UTF-8 databases; ODS table DDL is maintained separately.
CREATE DATABASE IF NOT EXISTS hospital
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS hospital_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
