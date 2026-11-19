# 门诊人次最小版与验收

对应 [节点 9A](https://github.com/ZhacoHewgo/medical-ops-analytics/issues/7)、[节点 11 接口](https://github.com/ZhacoHewgo/medical-ops-analytics/issues/11)、[节点 15 看板](https://github.com/ZhacoHewgo/medical-ops-analytics/issues/12) 和 [节点 19 验收](https://github.com/ZhacoHewgo/medical-ops-analytics/issues/16)。

## 统计与来源

只统计当前已通过节点 8 全量认证构建中的门诊就诊记录。固定数据集、批次、构建号和环境，在 MySQL 的同一只读可重复读事务内验证认证报告与库中数据，再按 `visit_date × dept_code` 聚合 `dwd_visit_detail` 中 `visit_type = OUTPATIENT` 的行数。范围含首尾。来回同一人的多次就诊计为多个人次。响应只包括聚合结果与构建元数据。

默认 `GET /api/dashboard/overview` 保持 CSV 入口，原健康检查继续使用 `GET /health`。宽表预览有独立入口且需要显式启用。预览故障时页面显示错误并可重试。

## 运行授权环境的宽表预览

1. 在已有节点 8 可运行的 Linux 环境确认一个允许使用的隔离 MySQL 全量构建。使用现有数据库环境配置 `ODS_DATABASE_URL`，遵守项目的本地端口与库名限制。确认相应全量构建认证报告在现有日志目录。
2. 在后端进程的环境设置 `OUTPATIENT_PREVIEW_ENABLED=1`、`OUTPATIENT_PREVIEW_ENV=test`、`OUTPATIENT_PREVIEW_DATASET_ID`、`OUTPATIENT_PREVIEW_BATCH_DATE`、`OUTPATIENT_PREVIEW_BUILD_RUN_ID`。后三项从经确认的同一份认证结果取值。隔离库名为 `hospital_test` 时使用 `test`。不要把命令里填入的真实值或本地配置提交到仓库。
3. 按 README 的后端启动方法，从项目根目录运行 `PYTHONPATH=backend python -m uvicorn app.main:app --host 127.0.0.1 --port 8765`。在 `frontend` 执行 `npm ci` 和 `npm run dev`，打开本机 `http://127.0.0.1:5173/?source=preview`。真实构建认证复用节点 8 的 Linux 文件与报告检查。

## API 契约

`GET /api/dashboard/outpatient-preview`。可选参数 `start_date`、`end_date` 为严格 `YYYY-MM-DD`。每个指定日期都须落在认证报告的 `effective_start` 至 `effective_end`（含首尾），省略时使用对应边界。可选 `dept_code` 必须是该构建的门诊科室代码，省略为全科。覆盖期内可选择无记录的区间。

| HTTP | 行为 |
| --- | --- |
| 200 | `{code: 0, msg: "ok", data: ...}`，数据含 `filters`、`outpatient_visits`、`daily_trend`、`department_ranking`。合法空范围人次为 0，两组序列为空。 |
| 403 | 非本机请求拒绝，`data` 为 null。 |
| 422 | 日期格式不合法、任一日期超出认证覆盖期、起止顺序错误或科室未知，`data` 为 null。 |
| 503 | 预览未启用、源不可用或构建不匹配，`data` 为 null。错误只包含固定消息。 |

`meta` 的预览字段为 `source=dwd_visit_detail`、`batch_date`、`build_run_id`、`available_start_date`、`available_end_date` 和 `department_codes`。日期边界取认证报告，覆盖期边缘即使没有门诊也仍可查询。前端检查预览元数据、日期范围及趋势、排行与人次总额的一致性。默认 CSV 接口沿用原筛选规则，空区间仍返回 200 和 0 人次。

## 节点 19 独立对账

在负责人确认的同一构建上，按任务卡的下列 SQL 独立复算。若按科室筛选，在查询中追加 `AND dept_code = @dept_code`，参数使用经确认的科室代码。

```sql
SELECT visit_date, dept_code, COUNT(*) AS outpatient_visits
FROM dwd_visit_detail
WHERE dataset_id = @dataset_id
  AND visit_type = 'OUTPATIENT'
  AND visit_date BETWEEN @start_date AND @end_date
GROUP BY visit_date, dept_code
ORDER BY visit_date, dept_code;
```

分别汇总 SQL 结果的日趋势、科室排行及总量，对比同构建号的 API 和看板。依次验证默认全科、单科、覆盖期内无记录、日期越界，以及旧构建或来源不可用。记录实际筛选、构建号、三方数值差和 API 状态。测试仅需聚合结果，保持源文件和运行日志在授权环境。

## 复验命令

按 README 安装依赖并设置 `PYTHONPATH=backend`，在仓库根目录执行：

```sh
python -m pytest -q backend/tests/test_api.py backend/tests/test_application.py backend/tests/test_outpatient.py backend/tests/test_s0_004_environment.py backend/tests/test_outpatient_preview.py
```

在前端执行 `npm test`、`npm run lint`、`npm run build`。如需复跑本机浏览器演练，在前端构建后分别设置 `MOPS_BROWSER_TEST=1`、`MOPS_CHROMIUM` 为本机 Chromium 可执行文件绝对路径、`MOPS_PLAYWRIGHT_CORE` 为本机安装的 `playwright-core/index.mjs` 绝对路径，再执行：

```sh
python -m pytest -q -s backend/tests/test_outpatient_preview.py -k browser_and_local_latency
```

浏览器验收启动回环测试进程，编译后的前端走真实 API，宽表数据来源由测试夹具切换为 SQLite，认证由固定夹具模拟。截图与延迟证据输出至 Git 忽略的 `artifacts/outpatient-mvp/`。未配置浏览器时该项显式跳过。

在配置好隔离 MySQL 和节点 8 测试环境的 Linux 中，可额外运行下例。用例在原有临时全量构建上执行独立 SQL 和预览对账，并按原测试流程清理临时批次。

```sh
python -m pytest -q 'backend/tests/test_s3_003_pipeline.py::test_mysql_dwd_cli_self_contained[clean]'
```

## 2026-11-19 本地验收记录

基线为 `9ab64f5`。环境为 Windows、Python 3.13、Node.js、Chromium。测试数据版本为 `COURSE_TEST`，统计期 2026-01-01 至 2026-01-03。SQLite 夹具包含 4 条目标数据集门诊记录、1 条目标数据集住院记录和 1 条其他数据集门诊记录。后二者从门诊汇总排除。以下数字仅用于自动验收。

| 检查 | 独立查询结果 | API | Chromium | 差值或状态 |
| --- | ---: | ---: | ---: | --- |
| 全科，2026-01-01 至 2026-01-03 | 4 | 4 | 4 | 0 |
| CARD，2026-01-01 至 2026-01-03 | 3 | 3 | 3 | 0 |
| RESP，2026-01-01 | 0 | 0 | 0 | 0 |
| 预览越界，2026-03-01 至 2026-03-02，或部分超出 2026-01-02 至 2026-01-05 | — | 422 | 验证部分越界显示 422 且清空旧数 | 通过 |
| 日趋势及科室排行 | 2、1、1；CARD 3、RESP 1 | 逐项一致 | 趋势及排行正常渲染 | 0 |
| 来源故障及恢复 | — | 503 | 错误态清空旧数，恢复后重试显示 4 | 通过 |
| 异常日期、未知科室、构建更换 | — | 422、422、503 | — | API 通过 |
| 非回环请求 | — | 403 | — | API 通过 |
| 切回 CSV、390px 窄屏 | — | CSV 200 | CSV 夹具显示 4，预览页无横向溢出 | 通过 |

PR #20 审查后新增日期边界用例。修复前，5 个越界请求错误返回 200，另有 1 个认证覆盖期与末条门诊记录不同的元数据用例失败。修复后重跑，**后端 124 项通过**，含 Chromium 验收和默认 CSV 越界返回空结果的回归。所选测试下 `backend/app` 行覆盖率 **92%**，新增预览数据访问模块 **75%**。**前端 14 项通过**，ESLint 与 Vite 生产构建通过。Vite 提示单个打包文件超过 500 kB。

返修后在同一夹具的本机 HTTP 测量中预热 5 次，随后以 5 并发发送 30 请求，轮换全科、CARD 与覆盖期内无记录的 RESP 日期。实测 **P95 38.75 ms、0 个请求失败**。计时包含应用响应，不含真实 MySQL 认证扫描和生产规模。

正式待验需要负责人提供已批准的隔离 MySQL 当前全量构建。届时按上方 SQL 核对真实日期、科室、人次、构建号，并重测本地延迟。八主题落表和回刷、完整 RBAC、导出、问数、10 指标对账及节点 19 完整验收均待后续任务。
