# 医疗运营分析课程项目

以课程平台下载的数据开展开发和对账。当前公开源码覆盖已验收的采集、清洗、质量检查、四张维表及证书桥接，以及门诊看板演示；完整主题集市、指标服务、鉴权和主题页面尚未全部实现。宽表仍待集成验收。

## 组员入口

- [三人分工和 AI 操作步骤](TEAM.md)：任务、文件归属、交付物和交接顺序。
- [平台数据怎么准备](data/README.md)：仓库已有文件、另外五份文件的放置位置。
- [数据缺口与对应课程要求](docs/DATA_GAPS.md)：缺什么、影响哪个节点、暂时如何处理。
- [课程节点与当前交付边界](docs/COURSE_SCOPE.md)：区分本轮任务和后续目标。

本仓库不上传生成器产出的合成数据文件，不引入外部公开数据。保留生成器源码及原有自动化测试，因为它们属于既有课程代码；本轮启动教程不运行生成器。

## 先获取平台数据

仓库内已有原始平台财务汇总文件 `data/files/finance_excel.csv`，910 行，未改内容，可供 A 组员立即做财务日汇总。

另外五份平台明细含患者相关字段，尚无其全部虚构、允许公开再分发的明确依据，因此未进入公开仓库。组员从课程平台的数据材料下载对应数据集，按 [data/README.md](data/README.md) 放置。文件只能在本地读取；不要提交它们或把整表发给在线 AI。

只有财务文件时，A 可以先工作，B 可以先做接口/权限说明与现有单元测试；C 需要下载门诊 CSV 后才能看见真实平台样例的门诊看板。文件未准备好时显示错误，不用财务数冒充门诊量。

## 用平台门诊 CSV 启动

准备 Python 3.11+、Node.js 24 和 Git。克隆仓库后，在根目录执行。Python 环境必须只属于本项目。

### macOS 或 Linux

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
PYTHONPATH=backend .venv/bin/python -m etl.jobs.build_outpatient
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe -m etl.jobs.build_outpatient
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

加工命令只读 `data/mock_his/his_outpatient.csv`，生成本机 `data/derived/outpatient.json`，不修改平台 CSV。该聚合文件也不提交仓库。保持后端窗口运行，另开终端：

```sh
cd frontend
npm ci
npm run dev
```

打开前端终端显示的网址。后端健康地址是 `http://127.0.0.1:8765/health`。看板 503 通常要先检查加工步骤是否成功；不要通过显示固定数字掩盖错误。此门诊切片不依赖 MySQL，也不需要模型 API Key；后续数仓实库验收需要负责人安排隔离测试环境。

## 本轮自测

在 macOS/Linux 根目录执行以下不依赖外置数据集的应用测试：

```sh
PYTHONPATH=backend .venv/bin/python -m pytest -q backend/tests/test_api.py backend/tests/test_application.py backend/tests/test_outpatient.py backend/tests/test_s0_004_environment.py
```

PowerShell 先设置 `$env:PYTHONPATH = "backend"`，再使用 `.\.venv\Scripts\python.exe` 执行同一组 pytest 参数。前端在 `frontend` 目录执行 `npm test`、`npm run lint`、`npm run build`。

原完整 ODS 测试中有两项依赖生成器的默认 12 个月样例；本轮不提供该数据集，不能直接把缺文件导致的失败归咎于平台 CSV。MySQL 实库测试也要先配置专用环境。报告分别写测试通过、失败、跳过和未运行，不能把局部结果写成全项目完成。

## 提交约定

每人独立分支，通过 PR 交付，由负责人统一合并。只提交任务范围内的源码、说明、必要测试；不提交五份患者明细、生成数据、数据库、密钥、日志或课程最终材料。公开仓库不代表已经通过课程平台或教师验收。
