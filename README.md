# 医疗运营数据主题分析平台

使用课程数据展示门诊、住院、收入、病种和资源效率。管理驾驶舱支持日期与科室筛选。多维报表可导出 Excel，综合运营周报和月报可导出 PDF。

## 页面预览

![管理驾驶舱](docs/screenshots/01-overview.png)

- [门诊分析](docs/screenshots/02-outpatient.png)
- [收入分析](docs/screenshots/03-revenue.png)
- [资源效率](docs/screenshots/04-resource.png)
- [多维报表](docs/screenshots/05-multidimensional-report.png)
- [综合运营月报](docs/screenshots/06-operations-report.png)

## 本地运行

1. 按 [数据目录说明](data/README.md) 准备课程数据，按 [认证环境与集市发布](deploy/auth-pilot.md) 初始化隔离 MySQL、登记数据源、构建宽表并发布集市。
2. 通过私有配置注入数据库连接和 JWT 签名密钥，启用 `AUTH_ENABLED=1`，并将 `OUTPATIENT_PREVIEW_DATASET_ID`、`OUTPATIENT_PREVIEW_BATCH_DATE`、`OUTPATIENT_PREVIEW_BUILD_RUN_ID`、`OUTPATIENT_PREVIEW_ENV` 对齐同一次发布。
3. 安装依赖，在两个终端分别启动应用。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
(cd frontend && npm ci)
```

```sh
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

```sh
cd frontend
npm run dev
```

打开 [本地网页](http://127.0.0.1:5173)。使用在隔离环境配置的账号登录。原始课程明细、运行密钥和数据库文件保存在本地。

## 测试

```sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests
(cd frontend && npm test && npm run lint && npm run build)
```
