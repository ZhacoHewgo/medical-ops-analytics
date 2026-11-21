START TRANSACTION;

INSERT INTO dim_department (dataset_id, dept_code, dept_name, attribute_status, mapping_version, scope)
SELECT @mops_sim_dataset, dept_code, dept_name, attribute_status, 'node07-sim-mapping-v1', 'SIM'
FROM (
    SELECT 'SIM_DEPT_01' AS dept_code, 'SIM_科室1' AS dept_name, 'UNKNOWN' AS attribute_status UNION ALL
    SELECT 'SIM_DEPT_02', 'SIM_科室2', 'UNKNOWN' UNION ALL
    SELECT 'SIM_DEPT_03', 'SIM_科室3', 'UNKNOWN' UNION ALL
    SELECT 'SIM_DEPT_04', 'SIM_科室4', 'UNKNOWN'
) AS seed
WHERE @mops_sim_dataset REGEXP '^SIM_[A-Za-z0-9_-]{1,120}$'
ON DUPLICATE KEY UPDATE dataset_id = dim_department.dataset_id;

INSERT INTO dim_disease (dataset_id, disease_code, disease_name, attribute_status, mapping_version, scope)
SELECT @mops_sim_dataset, disease_code, disease_name, attribute_status, 'node07-sim-mapping-v1', 'SIM'
FROM (
    SELECT 'SIM_DX_01' AS disease_code, 'SIM_病种1' AS disease_name, 'UNKNOWN' AS attribute_status UNION ALL
    SELECT 'SIM_DX_02', 'SIM_病种2', 'UNKNOWN' UNION ALL
    SELECT 'SIM_DX_03', 'SIM_病种3', 'UNKNOWN' UNION ALL
    SELECT 'SIM_DX_04', 'SIM_病种4', 'UNKNOWN' UNION ALL
    SELECT 'SIM_DX_05', 'SIM_病种5', 'UNKNOWN'
) AS seed
WHERE @mops_sim_dataset REGEXP '^SIM_[A-Za-z0-9_-]{1,120}$'
ON DUPLICATE KEY UPDATE dataset_id = dim_disease.dataset_id;

INSERT INTO dim_fee_type (dataset_id, fee_type_code, fee_type_name, source_supported, origin, mapping_version, scope)
SELECT @mops_sim_dataset, fee_type_code, fee_type_name, source_supported, origin, 'node07-sim-mapping-v1', 'SIM'
FROM (
    SELECT 'SERVICE' AS fee_type_code, 'SIM_服务费' AS fee_type_name, 1 AS source_supported, 'SIM' AS origin UNION ALL
    SELECT 'DRUG', 'SIM_药品费', 1, 'SIM' UNION ALL
    SELECT 'CONSUMABLE', 'SIM_耗材费', 1, 'SIM' UNION ALL
    SELECT 'REGISTRATION', 'SIM_挂号费', 0, 'COURSE_CATALOG' UNION ALL
    SELECT 'EXAM', 'SIM_检查费', 0, 'COURSE_CATALOG' UNION ALL
    SELECT 'TREATMENT', 'SIM_治疗费', 0, 'COURSE_CATALOG' UNION ALL
    SELECT 'BED', 'SIM_床位费', 0, 'COURSE_CATALOG'
) AS seed
WHERE @mops_sim_dataset REGEXP '^SIM_[A-Za-z0-9_-]{1,120}$'
ON DUPLICATE KEY UPDATE dataset_id = dim_fee_type.dataset_id;

COMMIT;
