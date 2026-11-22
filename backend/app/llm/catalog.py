"""Frozen question-answering catalogue: served metrics plus explicit data gaps."""

CATALOG_VERSION = "MOPS-QA-CATALOG-v1"

SERVED = {
    "MOPS-OUT-01": {
        "title": "门诊人次", "theme": "门诊", "unit": "人次", "grade": "A",
        "reader": "outpatient_volume", "additive": True, "places": 0,
        "keywords": ("门诊就诊人次", "门诊人次", "门诊人数", "门诊量"),
    },
    "MOPS-OUT-02": {
        "title": "门诊人次环比增长率", "theme": "门诊", "unit": "%", "grade": "A",
        "reader": "outpatient_growth", "additive": False, "places": 2,
        "keywords": ("门诊人次环比", "门诊量环比", "门诊环比"),
    },
    "MOPS-INP-01": {
        "title": "出院人次", "theme": "住院", "unit": "人次", "grade": "B",
        "reader": "inpatient_discharges", "additive": True, "places": 0,
        "keywords": ("出院人次", "出院人数", "出院量"),
    },
    "MOPS-INP-02": {
        "title": "平均住院日", "theme": "住院", "unit": "天", "grade": "B",
        "reader": "inpatient_stay", "additive": False, "places": 2,
        "keywords": ("平均住院天数", "平均住院日"),
    },
    "MOPS-INP-06": {
        "title": "住院次均费用", "theme": "住院", "unit": "元/人次", "grade": "B",
        "reader": "inpatient_cost", "additive": False, "places": 2,
        "keywords": ("次均住院费用", "住院次均费用"),
    },
    "MOPS-INC-01": {
        "title": "医疗收入总额", "theme": "收入", "unit": "元", "grade": "B",
        "reader": "income_total", "additive": True, "places": 2,
        "keywords": ("医疗收入合计", "医疗收入总额", "医疗收入"),
    },
}

GAPS = {
    "MOPS-OUT-03": {
        "title": "门诊人次同比增长率", "theme": "门诊", "unit": "%", "grade": "C",
        "reason": "PRIOR_YEAR_MISSING", "keywords": ("门诊人次同比", "门诊同比"),
    },
    "MOPS-OUT-04": {
        "title": "门诊次均费用", "theme": "门诊", "unit": "元/人次", "grade": "B",
        "reason": "FEE_SCOPE_UNCONFIRMED", "keywords": ("次均门诊费用", "门诊次均费用"),
    },
    "MOPS-OUT-05": {
        "title": "门诊号别结构占比", "theme": "门诊", "unit": "%", "grade": "B",
        "reason": "VISIT_TYPE_DICTIONARY_UNCONFIRMED", "keywords": ("门诊号别结构占比", "号别结构"),
    },
    "MOPS-INP-03": {
        "title": "实际占用床日", "theme": "住院", "unit": "床日", "grade": "B",
        "reason": "BED_DAY_RULE_UNCONFIRMED", "keywords": ("实际占用床日", "占用床日"),
    },
    "MOPS-INP-04": {
        "title": "床位使用率", "theme": "住院", "unit": "%", "grade": "C",
        "reason": "BED_CAPACITY_MISSING", "keywords": ("床位使用率",),
    },
    "MOPS-INP-05": {
        "title": "床位周转次数", "theme": "住院", "unit": "次", "grade": "C",
        "reason": "BED_CAPACITY_MISSING", "keywords": ("床位周转次数", "床位周转率"),
    },
    "MOPS-INC-02": {
        "title": "医疗收入环比增长率", "theme": "收入", "unit": "%", "grade": "B",
        "reason": "REVENUE_RULE_UNCONFIRMED", "keywords": ("医疗收入环比", "收入环比"),
    },
    "MOPS-INC-03": {
        "title": "医疗收入同比增长率", "theme": "收入", "unit": "%", "grade": "C",
        "reason": "PRIOR_YEAR_MISSING", "keywords": ("医疗收入同比", "收入同比"),
    },
    "MOPS-INC-04": {
        "title": "收入结构占比", "theme": "收入", "unit": "%", "grade": "B",
        "reason": "FEE_CATEGORY_DICTIONARY_UNCONFIRMED", "keywords": ("收入结构占比", "收入结构"),
    },
    "MOPS-INC-05": {
        "title": "财务收费对账差异", "theme": "收入", "unit": "元", "grade": "B",
        "reason": "FINANCE_FILE_NOT_IN_MART", "keywords": ("财务收费对账差异", "对账差异"),
    },
    "MOPS-DRUG-01": {
        "title": "药品收入", "theme": "药品", "unit": "元", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("药品收入",),
    },
    "MOPS-DRUG-02": {
        "title": "药品费占比", "theme": "药品", "unit": "%", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("药品费占比", "药品收入占比", "药占比"),
    },
    "MOPS-DRUG-03": {
        "title": "住院药品费用占比", "theme": "药品", "unit": "%", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("住院药品费用占比",),
    },
    "MOPS-DRUG-04": {
        "title": "抗菌药物使用强度", "theme": "药品", "unit": "DDD/百人天", "grade": "C",
        "reason": "DDD_WEIGHT_MISSING", "keywords": ("抗菌药物使用强度", "抗菌药物强度"),
    },
    "MOPS-CONS-01": {
        "title": "耗材收入", "theme": "耗材", "unit": "元", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("耗材收入",),
    },
    "MOPS-CONS-02": {
        "title": "耗占比", "theme": "耗材", "unit": "%", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("耗材收入占比", "耗占比"),
    },
    "MOPS-CONS-03": {
        "title": "住院耗材费用占比", "theme": "耗材", "unit": "%", "grade": "B",
        "reason": "FEE_CATEGORY_RULE_UNCONFIRMED", "keywords": ("住院耗材费用占比",),
    },
    "MOPS-CONS-04": {
        "title": "百元医疗收入卫生材料消耗", "theme": "耗材", "unit": "元/百元", "grade": "C",
        "reason": "COST_SOURCE_MISSING", "keywords": ("百元医疗收入卫生材料消耗",),
    },
    "MOPS-DIS-01": {
        "title": "出院患者病种分布", "theme": "病种", "unit": "人次", "grade": "B",
        "reason": "DISEASE_CODE_GROUPING_MISSING", "keywords": ("出院患者病种分布", "病种分布"),
    },
    "MOPS-DIS-02": {
        "title": "病种次均费用", "theme": "病种", "unit": "元/人次", "grade": "B",
        "reason": "DISEASE_CODE_GROUPING_MISSING", "keywords": ("病种次均费用",),
    },
    "MOPS-DIS-03": {
        "title": "DRG 入组率", "theme": "病种", "unit": "%", "grade": "C",
        "reason": "DRG_GROUPING_MISSING", "keywords": ("DRG入组率", "入组率"),
    },
    "MOPS-DIS-04": {
        "title": "CMI", "theme": "病种", "unit": "指数", "grade": "C",
        "reason": "DRG_GROUPING_MISSING", "keywords": ("病例组合指数", "CMI"),
    },
    "MOPS-DIS-05": {
        "title": "费用消耗指数", "theme": "病种", "unit": "指数", "grade": "C",
        "reason": "DRG_BENCHMARK_MISSING", "keywords": ("费用消耗指数",),
    },
    "MOPS-DIS-06": {
        "title": "时间消耗指数", "theme": "病种", "unit": "指数", "grade": "C",
        "reason": "DRG_BENCHMARK_MISSING", "keywords": ("时间消耗指数",),
    },
    "MOPS-EFF-01": {
        "title": "每实际占用床日住院收入", "theme": "资源效率", "unit": "元/床日", "grade": "B",
        "reason": "BED_DAY_RULE_UNCONFIRMED", "keywords": ("每实际占用床日住院收入",),
    },
    "MOPS-EFF-02": {
        "title": "医师人均门诊人次", "theme": "资源效率", "unit": "人次/人", "grade": "C",
        "reason": "STAFF_ESTABLISHMENT_MISSING", "keywords": ("医师人均门诊人次",),
    },
    "MOPS-EFF-03": {
        "title": "医师人均出院人次", "theme": "资源效率", "unit": "人次/人", "grade": "C",
        "reason": "STAFF_ESTABLISHMENT_MISSING", "keywords": ("医师人均出院人次",),
    },
    "MOPS-EFF-04": {
        "title": "大型设备使用率", "theme": "资源效率", "unit": "%", "grade": "C",
        "reason": "EQUIPMENT_LEDGER_MISSING", "keywords": ("大型设备使用率",),
    },
    "MOPS-EFF-05": {
        "title": "每百元固定资产医疗收入", "theme": "资源效率", "unit": "元/百元", "grade": "C",
        "reason": "ASSET_LEDGER_MISSING", "keywords": ("每百元固定资产医疗收入",),
    },
    "MOPS-SRV-01": {
        "title": "门诊预约率", "theme": "患者服务", "unit": "%", "grade": "C",
        "reason": "APPOINTMENT_SOURCE_MISSING", "keywords": ("门诊预约率", "预约率"),
    },
    "MOPS-SRV-02": {
        "title": "门诊退号率", "theme": "患者服务", "unit": "%", "grade": "C",
        "reason": "APPOINTMENT_SOURCE_MISSING", "keywords": ("门诊退号率", "退号率"),
    },
    "MOPS-SRV-03": {
        "title": "患者满意度", "theme": "患者服务", "unit": "%", "grade": "C",
        "reason": "SATISFACTION_SOURCE_MISSING", "keywords": ("患者满意度", "满意度"),
    },
    "MOPS-SRV-04": {
        "title": "检验报告及时率", "theme": "患者服务", "unit": "%", "grade": "C",
        "reason": "LIS_TIMESTAMP_MISSING", "keywords": ("检验报告及时率", "检验及时率"),
    },
    "MOPS-SRV-05": {
        "title": "病历记录关联率", "theme": "患者服务", "unit": "%", "grade": "B",
        "reason": "EMR_LINK_KEY_MISSING", "keywords": ("病历记录关联率", "病历关联率"),
    },
}

METRICS = {**SERVED, **GAPS}
THEMES = tuple(dict.fromkeys(item["theme"] for item in METRICS.values()))

GAP_ANSWER = "该指标在当前认证数据源下不可计算，保留空值并登记缺口原因。"


def entry(code):
    if code not in METRICS:
        raise KeyError(code)
    return METRICS[code]


def resolve(text):
    haystack = text.upper()
    matches = [(len(keyword), code) for code, item in METRICS.items()
               for keyword in item["keywords"] if haystack.endswith(keyword.upper())]
    if not matches:
        return None
    longest = max(length for length, _ in matches)
    codes = {code for length, code in matches if length == longest}
    if len(codes) > 1:
        raise LookupError("AMBIGUOUS")
    return codes.pop()


def is_served(code):
    return code in SERVED
