"""Publish the seven additional theme aggregates under the existing Node 9 transaction."""
from collections import Counter
from datetime import date, timedelta

from etl.config.source_config import OdsError


TABLES = {name: f"ads_{name}_theme" for name in
          ("inpatient", "income", "drug", "consumable", "drg", "efficiency", "service")}
KEYS = ("dataset_id", "stat_date", "dept_code", "batch_date", "source_build_run_id")
CHARGE = ("charge_count", "amount_samples", "charge_amount", "charge_status")
COLUMNS = {
    TABLES["inpatient"]: (*KEYS, "discharges", "stay_samples", "discharge_stay_days", "stay_status",
                          "fee_samples", "discharge_total_fee", "fee_status"),
    TABLES["income"]: (*KEYS, *CHARGE, "net_revenue", "net_revenue_reason"),
    TABLES["drug"]: (*KEYS, *CHARGE, "drug_count", "drug_amount_samples",
                    "drug_charge_amount", "drug_status", "drug_quantity", "ddd", "quantity_reason"),
    TABLES["consumable"]: (*KEYS, *CHARGE, "consumable_count", "consumable_amount_samples",
                          "consumable_charge_amount", "consumable_status", "consumption_cost", "cost_reason"),
    TABLES["drg"]: (*KEYS, "discharges", "source_diagnosis_cases", "drg_grouped_cases", "cmi", "grouping_reason"),
    TABLES["efficiency"]: (*KEYS, "discharges", "available_bed_days", "occupied_bed_days",
                          "available_reason", "occupied_reason"),
    TABLES["service"]: (*KEYS, "outpatient_visits", "inpatient_admissions", "emr_link_rate",
                       "appointment_count", "cancellation_count", "satisfaction_rate",
                       "lab_turnaround_minutes", "link_reason", "service_reason"),
}
STATUS = "ads_theme_status"
STATUS_COLUMNS = ("dataset_id", "stat_date", "theme", "batch_date", "source_build_run_id",
                  "rows_published", "data_status", "status_reason")
OUTPATIENT = "outpatient"
COURSE_NAMES = {
    "门诊": OUTPATIENT, "住院": "inpatient", "收入": "income", "药品": "drug",
    "耗材": "consumable", "病种": "drg", "资源效率": "efficiency", "患者服务": "service",
}
ALIASES = {
    **COURSE_NAMES,
    "disease": "drg",
    "resource": "efficiency",
    **{table: theme for theme, table in TABLES.items()},
    **{table.removeprefix("ads_").removesuffix("_theme"): theme for theme, table in TABLES.items()},
}
CANONICAL = (OUTPATIENT, *TABLES)
_LIMITS = {
    "inpatient": ("READY", None),
    "income": ("PARTIAL", "NET_REVENUE_RULE_UNCONFIRMED"),
    "drug": ("PARTIAL", "DRUG_QUANTITY_DDD_MISSING"),
    "consumable": ("PARTIAL", "CONSUMPTION_COST_MISSING"),
    "drg": ("HOLD", "OFFICIAL_DRG_GROUPING_MISSING"),
    "efficiency": ("PARTIAL", "BED_CAPACITY_OR_OCCUPANCY_UNCONFIRMED"),
    "service": ("PARTIAL", "PATIENT_SERVICE_SOURCES_OR_RULES_MISSING"),
}


def canonical(theme):
    if not isinstance(theme, str):
        raise OdsError("N9_THEME_INVALID")
    name = ALIASES.get(theme.strip(), theme.strip())
    if name not in CANONICAL:
        raise OdsError("N9_THEME_INVALID")
    return name


def validate_selection(themes):
    if not isinstance(themes, (tuple, list)):
        raise OdsError("N9_ARGUMENTS_INVALID")
    selected = []
    for theme in themes:
        name = canonical(theme)
        if name == OUTPATIENT or name in selected:
            raise OdsError("N9_THEME_INVALID")
        selected.append(name)
    return tuple(selected)


def _measure(count, samples, amount):
    if count == 0:
        return None, "NO_RECORDS"
    if count != samples:
        return None, "INCOMPLETE_INPUT"
    return amount, None


def _discharges(cursor, keys):
    cursor.execute(
        "SELECT discharge_date,dept_code,COUNT(*),COUNT(source_stay_days),SUM(source_stay_days),"
        "COUNT(source_total_fee),SUM(source_total_fee),COUNT(disease_code) "
        "FROM dwd_visit_detail WHERE dataset_id=%s AND discharge_date BETWEEN %s AND %s "
        "AND visit_type='INPATIENT' GROUP BY discharge_date,dept_code ORDER BY discharge_date,dept_code",
        keys,
    )
    result = {theme: [] for theme in ("inpatient", "drg", "efficiency")}
    for day, dept, count, stay_n, days, fee_n, total, diagnosis in cursor.fetchall():
        result["inpatient"].append((day, dept, count, stay_n, *_measure(count, stay_n, days),
                                    fee_n, *_measure(count, fee_n, total)))
        result["drg"].append((day, dept, count, diagnosis, None, None, "UNCONFIRMED_RULE"))
        result["efficiency"].append((day, dept, count, None, None, "MISSING_SOURCE", "UNCONFIRMED_RULE"))
    return result


def _charge_groups(cursor, keys):
    cursor.execute(
        "SELECT c.fee_date,c.dept_code,COUNT(*),COUNT(c.amount),SUM(c.amount),"
        "COUNT(CASE WHEN c.fee_type_code='DRUG' THEN 1 END),"
        "COUNT(CASE WHEN c.fee_type_code='DRUG' THEN c.amount END),"
        "SUM(CASE WHEN c.fee_type_code='DRUG' THEN c.amount END),"
        "COUNT(CASE WHEN c.fee_type_code='CONSUMABLE' THEN 1 END),"
        "COUNT(CASE WHEN c.fee_type_code='CONSUMABLE' THEN c.amount END),"
        "SUM(CASE WHEN c.fee_type_code='CONSUMABLE' THEN c.amount END),"
        "COUNT(CASE WHEN f.source_supported=0 THEN 1 END) "
        "FROM dwd_charge_detail c JOIN dim_fee_type f ON f.dataset_id=c.dataset_id "
        "AND f.fee_type_code=c.fee_type_code WHERE c.dataset_id=%s AND c.fee_date BETWEEN %s AND %s "
        "GROUP BY c.fee_date,c.dept_code ORDER BY c.fee_date,c.dept_code",
        keys,
    )
    result = {theme: [] for theme in ("income", "drug", "consumable")}
    for day, dept, count, samples, total, drugs, drug_n, drug_fee, goods, goods_n, goods_fee, unsupported in cursor.fetchall():
        prefix = (day, dept, count, samples, *_measure(count, samples, total))
        result["income"].append((*prefix, None, "UNCONFIRMED_RULE"))
        drug = (None, None, None, "UNCONFIRMED_RULE") if unsupported else (drugs, drug_n, *_measure(drugs, drug_n, drug_fee))
        goods_result = (None, None, None, "UNCONFIRMED_RULE") if unsupported else (goods, goods_n, *_measure(goods, goods_n, goods_fee))
        result["drug"].append((*prefix, *drug, None, None, "MISSING_SOURCE"))
        result["consumable"].append((*prefix, *goods_result, None, "MISSING_SOURCE"))
    return result


def _service_groups(cursor, keys):
    cursor.execute(
        "SELECT visit_date,dept_code,COUNT(CASE WHEN visit_type='OUTPATIENT' THEN 1 END),"
        "COUNT(CASE WHEN visit_type='INPATIENT' THEN 1 END) FROM dwd_visit_detail "
        "WHERE dataset_id=%s AND visit_date BETWEEN %s AND %s "
        "GROUP BY visit_date,dept_code ORDER BY visit_date,dept_code",
        keys,
    )
    return {
        "service": [(day, dept, outpatient, inpatient, None, None, None, None, None,
                     "UNCONFIRMED_RULE", "MISSING_SOURCE") for day, dept, outpatient, inpatient in cursor.fetchall()]
    }


def publish(cursor, themes, dataset, batch, run, start, end):
    """Caller owns the locked, freshly certified transaction and commits all destinations together."""
    tables = [*(TABLES[theme] for theme in themes), STATUS]
    binds = ", ".join("%s" for _ in tables)
    cursor.execute("SELECT table_name,engine FROM information_schema.tables WHERE table_schema=DATABASE() "
                   f"AND table_name IN ({binds})", tuple(tables))
    if dict(cursor.fetchall()) != dict.fromkeys(tables, "InnoDB"):
        raise OdsError("N9_MIGRATION_REQUIRED")
    wanted = set(themes)
    keys = (dataset, start, end)
    groups = {}
    if wanted & {"inpatient", "drg", "efficiency"}:
        groups.update(_discharges(cursor, keys))
    if wanted & {"income", "drug", "consumable"}:
        groups.update(_charge_groups(cursor, keys))
    if "service" in wanted:
        groups.update(_service_groups(cursor, keys))

    where = "dataset_id=%s AND stat_date BETWEEN %s AND %s"
    first, last = map(date.fromisoformat, (start, end))
    row_counts, states = {STATUS: 0}, {}
    for theme in themes:
        table = TABLES[theme]
        scoped = groups[theme]
        rows = [(dataset, day, dept, batch, run, *metrics) for day, dept, *metrics in scoped]
        _replace(cursor, table, COLUMNS[table], where, keys, rows)
        row_counts[table] = len(rows)

        totals = Counter(row[0] for row in scoped)
        incomplete = {row[0] for row in scoped if "INCOMPLETE_INPUT" in row}
        base_state, base_reason = _LIMITS[theme]
        daily = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            if day in incomplete and base_state == "READY":
                state, reason = "PARTIAL", "INCOMPLETE_INPUT"
            else:
                state, reason = base_state, base_reason
            daily.append((dataset, day, theme, batch, run, totals[day], state, reason))
        _replace(cursor, STATUS, STATUS_COLUMNS, where + " AND theme=%s", (*keys, theme), daily)
        row_counts[STATUS] += len(daily)
        states[theme] = "PARTIAL" if incomplete and base_state == "READY" else base_state
    return row_counts, states


def _replace(cursor, table, columns, where, keys, rows):
    names = ", ".join(columns)
    cursor.execute(f"DELETE FROM {table} WHERE {where}", keys)
    if rows:
        values = ", ".join("%s" for _ in columns)
        cursor.executemany(f"INSERT INTO {table} ({names}) VALUES ({values})", rows)
    order = "stat_date" if table == STATUS else "stat_date,dept_code"
    cursor.execute(f"SELECT {names} FROM {table} WHERE {where} ORDER BY {order}", keys)
    if tuple(cursor.fetchall()) != tuple(rows):
        raise OdsError("N9_RECONCILE_FAILED")
