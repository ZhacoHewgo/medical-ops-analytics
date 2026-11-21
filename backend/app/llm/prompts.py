import json


NL2SQL_PROMPT = (
    "只解析医疗运营聚合问数。仅输出一个 JSON 对象，字段为 indicator、start_date、end_date、"
    "scope、series、growth：indicator 取给定指标的名称或编码，start_date/end_date 为 YYYY-MM-DD，"
    "scope 取 全院、本科室 或 科室=<编码>，series 表示是否按月展开，growth 表示是否门诊人次自然月环比。"
    "只选择给定指标与维度。缺信息时返回无法解析的原因。"
    "不要输出 SQL、患者字段或权限规则。实际可访问科室由应用独立校验。"
)


def build_nl2sql_prompt(question, metric_meta, dim_schema):
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 300:
        raise ValueError("Invalid question")
    return [
        {"role": "system", "content": NL2SQL_PROMPT},
        {"role": "user", "content": json.dumps({
            "question": question, "metrics": metric_meta, "dimensions": dim_schema,
        }, ensure_ascii=False, allow_nan=False)},
    ]
