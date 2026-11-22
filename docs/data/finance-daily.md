# A 组交接｜平台财务日汇总

**统计期为 2025-04-01 至 2025-06-30，包含首尾两天和全部科室。** 按原始平台财务金额，以日期 × 科室代码直接汇总。910 条记录形成 910 个分组。日期连续 91 天，10 个科室每天各有一条记录。因此本版日汇总不会减少行数。

## 结果与总额对账

[下载本次交付的完整汇总表](finance-daily.csv)。运行下方命令可在 `data/derived/finance-daily.csv` 重新生成同一结果。结果表含日期、科室代码、来源行数及三列金额，按日期、科室代码排序。金额沿用源文件数值，保留两位小数，币种和单位待确认。三列分别加总。

| 期间 | 来源行数 | 收入 revenue | 药品收入 drug_revenue | 耗材收入 consumable_revenue |
|---|---:|---:|---:|---:|
| 2025-04 | 300 | 14,254,655.79 | 4,276,396.60 | 1,425,465.75 |
| 2025-05 | 310 | 13,836,939.99 | 4,151,081.81 | 1,383,694.09 |
| 2025-06 | 300 | 13,355,035.33 | 4,006,510.56 | 1,335,503.70 |
| **全期原表** | **910** | **41,446,631.11** | **12,433,988.97** | **4,144,663.54** |
| 日汇总 CSV 回读合计 | 910 | 41,446,631.11 | 12,433,988.97 | 4,144,663.54 |
| **差额** | **0** | **0.00** | **0.00** | **0.00** |

## 计算步骤

1. 从[仓库](https://github.com/ZhacoHewgo/medical-ops-analytics/blob/b289c523052b66e7e150df20133989d7cba34b0f/data/files/finance_excel.csv)取得 `data/files/finance_excel.csv`，用二进制原文件计算 SHA-256，结果与仓库的 `docs/platform-data-inventory.json` 一致。SHA-256 为 `ddcab4e7dc4983ebc7a42dcb6651efae098fffc69a7d4d999cb2f32923cd714f`。
2. 保留全部来源记录。按 `stat_date, dept_code` 分组，对三列分别执行 SUM，对行数执行 COUNT(*)。SQL 路径将金额乘 100 转为整数并确认无精度损失，汇总完成后除以 100 恢复源单位。
3. 独立用 Decimal 逐组加总，与 SQLite 的整数金额结果比较，910 组逐列一致。将日汇总写入 CSV 后重新打开，核对来源行数及三列总额。上表月度小计由该 CSV 按月份汇总而得。

**Excel 复算方法**：在“数据 → 从文本/CSV”导入原文件，确认三列金额为数值；创建数据透视表，将统计日期和科室代码依次放入“行”（日期保留到日），三列金额放入“值”并设为“求和”，记录编号设为“计数”。不设筛选，查看总计后再筛选下表两组。此处提供人工复算步骤，实际执行的是 SQLite 与 Python 两条计算路径。

## 两个分组的手算对照

按日期、科室代码排序选首组和末组。因所有组均只有一条记录，对任一金额列，手算即“0 + 该组唯一金额”。以下数字为分组金额，不含记录编号。独立程序复算与 SQL 均已执行，人工 Excel 复核尚未执行。

| 日期 | 科室 | 行数 | 直接相加（收入 / 药品 / 耗材） | SQLite 与直接相加之差 |
|---|---|---:|---|---|
| 2025-04-01 | EK | 1 | 44,873.02 / 13,461.91 / 4,487.30 | 0.00 / 0.00 / 0.00 |
| 2025-06-30 | XH | 1 | 11,313.92 / 3,394.18 / 1,131.39 | 0.00 / 0.00 / 0.00 |

另取两个整日，把当日 10 个科室的结果相加，并独立从原表筛出同日记录求和。两条路径都得到下表数值，比单行分组更能检查有无遗漏科室。

| 日期 | 科室数 | 收入 | 药品收入 | 耗材收入 |
|---|---:|---:|---:|---:|
| 2025-04-01 | 10 | 544,284.99 | 163,285.48 | 54,428.50 |
| 2025-06-30 | 10 | 455,848.57 | 136,754.57 | 45,584.85 |

再按三个已核对的月小计列出全期加法，供交接时用计算器复核：

- 收入：14,254,655.79 + 13,836,939.99 + 13,355,035.33 = 41,446,631.11。
- 药品收入：4,276,396.60 + 4,151,081.81 + 4,006,510.56 = 12,433,988.97。
- 耗材收入：1,425,465.75 + 1,383,694.09 + 1,335,503.70 = 4,144,663.54。

## 检查结果与待确认事项

| 检查 | 实际结果 | 处理 |
|---|---:|---|
| 重复的 record_id / 多出的重复行 | 0 / 0 | 保留全部记录 |
| 同日期、同科室多行组 | 0 | 本版组合键唯一，未来批次仍需检查 |
| 六列空白单元格（含纯空格） | 0 | 未填零 |
| 收入 / 药品 / 耗材的负值行数 | 0 / 0 / 0 | 未过滤负值 |
| 药品加耗材大于收入的行数 | 0 | 仅是数值检查，不能据此确定会计包含关系 |

**金额分布有一个值得向课程方确认的规律。** 全部 910 行中，药品收入与同一行收入 × 30% 的绝对差均 ≤ 0.005 个源金额单位，耗材收入与收入 × 10% 也均满足这一条件。请确认样例是否采用固定比例分摊，再解释科室费用结构。复算直接读取三列原金额，未用这两个比例倒算或调数。

### A 首轮数据缺口清单

| 缺口 | 影响 | 负责人需要确认或取得的资料 |
|---|---|---|
| 金额单位、收入确认日和退费处理未定义 | 当前可复算源金额，还不能认定为会计净收入 | 财务字段说明、日期归属、退费冲减时点 |
| 药品及耗材与收入的包含关系未定义 | 三列可各自求和，直接相加可能重复 | 收入项目范围、是否按比例分摊及收费对账规则 |
| 科室标准映射、生效期和记录修订规则待定 | 暂按原始科室代码分组，后续跨源关联、回刷可能重复计数 | 科室字典、同键追加或替换规则、批准的输入版本 |
| 财务样例仅覆盖 2025 年 4–6 月 | 无法验证连续 12 个月结果或上年同期变化 | 其余期间及可比基期的同口径数据 |
| 财务表没有就诊标识或逐笔收费关联键 | 无法从本表推导门诊人次或完成逐笔收费对账 | 节点 8 冻结后的宽表字段、归属键、对账粒度和输出格式 |

其他主题所缺资料见[项目数据缺口说明](../DATA_GAPS.md)。字段冻结后再开展门诊 DWS、指标结果与回刷验证。

## 给宽表负责人的对接说明

现有采集配置已将此文件映射到 `ods_finance_excel`。其三列金额在 ODS 均为 TEXT，唯一键为 `(dataset_id, source_row_number)`，并不保证日期与科室唯一。[只读 MySQL 草案](../../sql/dws/finance_daily.sql) 指定单个 dataset_id，先检查数值格式，再以 DECIMAL 求和；请负责人确认载入批次与源指纹后再试跑。MySQL 实库未执行。本文已执行的是下方 SQLite 和 Decimal 复算。

建议交接结果的键为同一数据版本内的 `(stat_date, dept_code)`。财务是科室日汇总，收费宽表是明细：若未来对账，先把两侧分别归到相同日期及科室，再比较金额和缺失侧。直接把财务日收入挂到每笔收费再求和，会按收费笔数重复计算。宽表字段、收入确认与退费归属仍待负责人冻结。

## 可执行复算命令

在包含 `data/files/finance_excel.csv` 的根目录用 PowerShell 执行以下代码。只用 Python 标准库，无需安装依赖。输出到仓库已忽略的 `data/derived/`。从仓库根目录运行，无需患者明细；原表保持只读。

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING='utf-8'
@'
from pathlib import Path
from decimal import Decimal
from collections import Counter, defaultdict
from datetime import date
import csv, hashlib, sqlite3

src = Path("data/files/finance_excel.csv")
assert hashlib.sha256(src.read_bytes()).hexdigest() == "ddcab4e7dc4983ebc7a42dcb6651efae098fffc69a7d4d999cb2f32923cd714f"
cols = ["revenue", "drug_revenue", "consumable_revenue"]
with src.open(encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    assert reader.fieldnames == ["record_id", "stat_date", "dept_code", *cols]
    rows = list(reader)
blanks = {c: sum(not r[c].strip() for r in rows) for c in rows[0]}
assert not any(blanks.values()), "空字段需登记并确认，不填零"
ids = Counter(r["record_id"] for r in rows)
dates = sorted({r["stat_date"] for r in rows})
depts = sorted({r["dept_code"] for r in rows})
groups = defaultdict(list)
negative = Counter()
source_total = [Decimal(0)] * 3
con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE finance(day TEXT, dept TEXT, a INTEGER, b INTEGER, c INTEGER)")
for r in rows:
    day = date.fromisoformat(r["stat_date"]).isoformat()
    amounts = [Decimal(r[c]) for c in cols]
    cents = [int(x * 100) for x in amounts]
    assert all(x.is_finite() and x * 100 == n for x,n in zip(amounts,cents))
    groups[day, r["dept_code"]].append(amounts)
    con.execute("INSERT INTO finance VALUES(?,?,?,?,?)", (day, r["dept_code"], *cents))
    for j,x in enumerate(amounts):
        source_total[j] += x
        negative[cols[j]] += int(x < 0)
sql_rows = con.execute("""SELECT day,dept,COUNT(*),SUM(a),SUM(b),SUM(c)
                          FROM finance GROUP BY day,dept ORDER BY day,dept""").fetchall()
directory = Path("data/derived")
directory.mkdir(parents=True, exist_ok=True)
outfile = directory / "finance-daily.csv"
with outfile.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["stat_date","dept_code","source_rows",*cols])
    for day,dept,n,*cents in sql_rows:
        direct = [sum(items) for items in zip(*groups[day,dept])]
        assert n == len(groups[day,dept]) and direct == [Decimal(v)/100 for v in cents]
        writer.writerow([day,dept,n,*[f"{x:.2f}" for x in direct]])
with outfile.open(encoding="utf-8-sig",newline="") as f:
    result = list(csv.DictReader(f))
result_total = [sum(Decimal(r[c]) for r in result) for c in cols]
assert source_total == result_total and sum(int(r["source_rows"]) for r in result)==len(rows)
print("dates:", dates[0], dates[-1], len(dates), "departments:", len(depts))
print("rows/groups:", len(rows),len(groups), "totals:", *source_total)
print("repeat_ids:",sum(v>1 for v in ids.values()), "excess_id_rows:",sum(v-1 for v in ids.values()))
print("multirow_groups:",sum(len(v)>1 for v in groups.values()), "blank_cells:",sum(blanks.values()),"negative:",dict(negative))
print("calendar_days:",(date.fromisoformat(dates[-1])-date.fromisoformat(dates[0])).days+1)
for key in [min(groups),max(groups)]:
    print("spot:", *key,len(groups[key]),*[sum(col) for col in zip(*groups[key])])
for day in [dates[0], dates[-1]]:
    raw_day = [r for r in rows if r["stat_date"] == day]
    out_day = [r for r in result if r["stat_date"] == day]
    totals = [sum(Decimal(r[c]) for r in raw_day) for c in cols]
    assert totals == [sum(Decimal(r[c]) for r in out_day) for c in cols]
    print("cross-dept:", day, len(out_day), *totals)
for c,factor in [(cols[1], Decimal(".30")), (cols[2], Decimal(".10"))]:
    gaps = [abs(Decimal(r[c]) - Decimal(r[cols[0]]) * factor) for r in rows]
    print("proportion check:", c, sum(x <= Decimal(".005") for x in gaps), max(gaps))
for month in sorted({r["stat_date"][:7] for r in result}):
    part = [r for r in result if r["stat_date"].startswith(month)]
    print("month:", month,len(part),*[sum(Decimal(r[c]) for r in part) for c in cols])
print("components_above_revenue:",sum(Decimal(r[cols[1]])+Decimal(r[cols[2]])>Decimal(r[cols[0]]) for r in rows))
'@ | python -
```
