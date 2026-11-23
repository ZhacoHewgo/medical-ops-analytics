import json


NL2SQL_PROMPT = (
    "只解析医疗运营聚合问数。仅输出一个 JSON 对象，字段为 indicator、start_date、end_date、"
    "scope、series、growth：indicator 取给定指标的名称或编码，start_date/end_date 为 YYYY-MM-DD，"
    "scope 取 全院、本科室 或 科室=<编码>，保留问句指定的科室编码。"
    "整月从 1 日到月末，单日时两个日期相同。每月或各月才令 series 为 true，明确问环比才令 growth 为 true。"
    "只选择给定指标与维度。缺信息时返回无法解析的原因。"
    "不要输出 SQL、患者字段或权限规则。实际可访问科室由应用独立校验。"
    "月份问句只统计该月，不能默认从年初开始。"
    '示例：问“2024-03 全院门诊量”，答 {"indicator":"MOPS-OUT-01","start_date":"2024-03-01",'
    '"end_date":"2024-03-31","scope":"全院","series":false,"growth":false}。'
)


def build_nl2sql_prompt(question, metric_meta, dim_schema):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 300:
        raise ValueError("Invalid question")
    return [
        {"role": "system", "content": NL2SQL_PROMPT},
        {"role": "user", "content": json.dumps({
            "metrics": metric_meta, "dimensions": dim_schema, "question": question,
        }, ensure_ascii=False, allow_nan=False)},
    ]
