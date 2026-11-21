from etl.config.source_config import COURSE_DATASET


SIM_VERSION = 'node07-sim-mapping-v1'
COURSE_VERSION = 'node07-course-mapping-v1'
DEPARTMENTS = tuple('EK FC GK HN HX ICU JZ PW SJ XH'.split())
DISEASE_CODES = tuple('A09 C16 E11 I10 I25 I63 J18 J44 K35 N18'.split())
FEE_TYPES = {
    'XY': 'DRUG', 'ZC': 'DRUG', 'JC': 'EXAM', 'JY': 'LAB', 'ZL': 'TREATMENT',
    'SS': 'SURGERY', 'CL': 'CONSUMABLE', 'CW': 'BED', 'HL': 'NURSING',
}
FEE_NAMES = {
    'DRUG': '药品费', 'EXAM': '检查费', 'LAB': '检验费', 'TREATMENT': '治疗费',
    'SURGERY': '手术费', 'CONSUMABLE': '材料费', 'BED': '床位费', 'NURSING': '护理费',
}
MAPPINGS = {
    'dept_code': {code: code for code in DEPARTMENTS},
    'visit_type': {'门诊': 'OUTPATIENT', '住院': 'INPATIENT'},
    'gender': {'男': 'M', '女': 'F'},
    'disease_code': {code: code for code in DISEASE_CODES},
    'fee_type_code': FEE_TYPES,
}
OUTPATIENT_MAPPINGS = {
    **MAPPINGS,
    'visit_type': {label: 'OUTPATIENT' for label in ('普通', '专家', '急诊', '门诊')},
}


def version(dataset_id):
    return COURSE_VERSION if dataset_id == COURSE_DATASET else SIM_VERSION


def source_mappings(dataset_id, source, defaults):
    if dataset_id != COURSE_DATASET:
        return defaults
    return OUTPATIENT_MAPPINGS if source == 'his_outpatient' else MAPPINGS
