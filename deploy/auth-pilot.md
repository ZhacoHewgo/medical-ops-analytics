# 认证门诊试运行与回刷检查

本页用于已有隔离认证环境。部署前复核基线和运行身份。

| 检查项 | 试运行约定 |
| --- | --- |
| 应用版本 | 已试跑 #36 的 `dab17b0`，接入 #26 的 `38105ac` 发布一致性修复。启动配置按 #37 的四服务结构组织。 |
| 网络 | MySQL 和后端共用隔离网络空间。MySQL 监听 `127.0.0.1:13306`，后端通过该地址访问测试库，网关转发 API 且只在宿主回环发布一个端口。 |
| 数据 | 使用 `hospital_test` 和本次专用数据集。配置六源登记、数据库测试账号、签名密钥和脱敏密钥，通过私有运行配置注入。 |
| 标识 | 记录数据集、批次和当前宽表构建号。启动认证 API 前把三者配置成同一次已通过质量门的结果。 |

## 构建与回刷

在认证环境的项目根目录，运行已登记批次的 ETL。核对质量与宽表均通过，并记录构建号。门诊应用使用相同数据集和批次。

```sh
PYTHONPATH=backend python -m etl.jobs.run_all --env test --through dwd
```

把 `MOPS_PILOT_DATASET`、`MOPS_PILOT_BATCH`、`MOPS_REPLAY_START`、`MOPS_REPLAY_END` 设置为本次测试值。开始和结束日期均纳入回刷。首次发布使用认证覆盖区间，纠正历史时使用具体日期区间。

```sh
PYTHONPATH=backend python - "$MOPS_PILOT_DATASET" "$MOPS_PILOT_BATCH" "$MOPS_REPLAY_START" "$MOPS_REPLAY_END" <<'PY'
import sys
from etl.jobs.aggregate_dws import rerun

dataset, batch, first, last = sys.argv[1:]
result = rerun((first, last), dataset_id=dataset, batch_date=batch, env="test")
assert result["status"] == "PASS"
print(result["ads_status"], result["row_counts"])
PY
```

回刷前后按日期、科室核对宽表与 ADS 人次，并比较范围外的 DWS 和 ADS。重复运行同一区间时，汇总键和值应一致。

## 验收记录

| 验证 | 结果或记录项 |
| --- | --- |
| 运维入口 | 记录四服务健康状态、回环网关、当前构建和临时库清理。 |
| 登录和科室 | 管理员创建分析员和科室账号。无登录返回 401，跨科室返回 403。确认科室账号的图表及导出仅显示其授权科室。 |
| 区间 | 覆盖外日期返回 422。覆盖内真实空日期返回 0。 |
| 发布故障 | 构建处于待刷新时，认证总览和导出返回 503。回刷后重查，并核对范围外的日期。 |
| 导出及审计 | 打开中文 Excel，核对月份、科室和人次。按动作与状态对账业务审计。 |

隔离复验已记录 1 月 2 人次、2 月 3 人次、覆盖内空月 0、单科 4 人次。单日回刷连续执行两次，2 月数据保持一致，11 条业务审计与请求分类匹配。课程批次、自然日自动运行、持续成功率和自动告警见 Issue #18。

结束时停止本次 Compose 项目，只保留脱敏对账摘要和复现命令。
