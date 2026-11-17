# 医疗运营分析 · 课程协作版

课程项目的公开开发仓库。当前可演示门诊总量、日期趋势、科室排行和筛选；包含已在本地验收的数据采集、清洗、质量检查和公共维表代码。节点 8 宽表、后续集市、完整权限和主题看板仍在开发。课内验收以课程平台和教师要求为准。

**组员先读：[三人分工与 AI 上手手册](TEAM.md)**。首次只领一张任务卡，完成后用 Pull Request（PR）发起代码审阅。

## 从零跑起门诊演示

准备 Git、Python 3.11+、Node.js 24。解压 GitHub 的 Download ZIP 也能本地启动，但要交 PR，请用 Git 或 GitHub Desktop 克隆。以下命令在项目根目录运行，适用于 macOS、Linux 或 Windows Git Bash。Windows 用 `.venv/Scripts/python.exe` 替换 `.venv/bin/python`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt -r scripts/data/requirements.txt
.venv/bin/python scripts/data/generate_mock.py --months 12
PYTHONPATH=backend .venv/bin/python -c 'from pathlib import Path; from app.outpatient import DEFAULT_DATA_PATH; from etl.jobs.build_outpatient import build_outpatient; build_outpatient(Path("data/generated/synthetic-v1_seed42_2025-01-01_12m/mock_his/his_outpatient.csv"), DEFAULT_DATA_PATH)'
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Windows 如果没有 `python3`，可用 `py -3` 创建虚拟环境。保持后端窗口运行，再开一个终端：

```sh
cd frontend
npm ci
npm run dev
```

打开前端终端显示的本地网址，核对门诊总量、趋势和科室排行。先访问 `http://127.0.0.1:8765/health` 排查后端是否启动；看板返回 503 时，检查聚合文件是否成功生成。聚合格式沿用旧版固定的 `meta.source_file` 字段；本教程实际输入是命令中明确指定的合成 CSV。演示无需 MySQL、账号或模型 API Key。

## 自测

- 后端和合成数据：根目录执行 `PYTHONPATH=backend .venv/bin/python -m pytest -q backend/tests tests/data`。先执行上方 12 个月合成数据生成命令，ODS 回归会读取这套默认样例。未配置隔离 MySQL 时，实库用例可能跳过；报告应写清通过、失败和跳过各多少项。
- 前端：在 `frontend` 目录执行 `npm test`、`npm run lint`、`npm run build`。
- 要做 MySQL 实库验证，先和负责人对齐本地隔离测试环境。首次任务用小型纯合成样例或现有测试即可，不需要连接课程云数据库。

公开仓库只收源码、手写的纯合成测试样例和公开协作说明。不提交原始课程数据、生成的数据文件、数据库备份、口令、模型密钥、测试日志或课程提交材料。
