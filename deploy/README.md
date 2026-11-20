# 本地隔离部署

从公开仓库的独立副本启动 MySQL、API、网页和 Nginx。网关使用本机回环随机端口。门诊演示读取本地聚合文件，MySQL 同时执行独立健康检查。认证试运行、历史回刷和课程批次验收见 Issue #18。

## 准备数据和临时密码

在仓库根目录运行。需要 Python 3.10 或以上、Docker 和 Docker Compose。

```sh
mops_runtime="$(mktemp -d)"
export MOPS_BUILD_CONTEXT="$(pwd -P)"
export MOPS_AGGREGATE_FILE="$mops_runtime/outpatient.json"
export MOPS_MYSQL_SECRET_FILE="$mops_runtime/mysql-root"

python3 -m venv "$mops_runtime/python"
mops_python="$mops_runtime/python/bin/python"
"$mops_python" -m pip install -r scripts/data/requirements.txt
mops_project="mops-local-$("$mops_python" -c 'import secrets; print(secrets.token_hex(4))')"
mops_dataset="data/generated/$mops_project"
"$mops_python" scripts/data/generate_mock.py --months 1 --output-dir "$PWD/$mops_dataset"

PYTHONPATH=backend "$mops_python" - "$mops_dataset" <<'PY'
from pathlib import Path
import os
import secrets
import sys

from etl.jobs.build_outpatient import build_outpatient

source = Path(sys.argv[1]) / "mock_his" / "his_outpatient.csv"
aggregate = Path(os.environ["MOPS_AGGREGATE_FILE"])
build_outpatient(source, aggregate)
aggregate.chmod(0o444)
secret = Path(os.environ["MOPS_MYSQL_SECRET_FILE"])
descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as stream:
    stream.write(secrets.token_urlsafe(32) + "\n")
PY
```

## 启动和检查

```sh
docker compose -f deploy/docker-compose.yml -p "$mops_project" config --quiet
docker compose -f deploy/docker-compose.yml -p "$mops_project" up --build --wait --wait-timeout 120
mops_address="$(docker compose -f deploy/docker-compose.yml -p "$mops_project" port nginx 8080)"
curl --fail "http://$mops_address/health"
curl --fail "http://$mops_address/api/dashboard/overview"
```

在浏览器打开该回环地址查看门诊总览。结束后运行下列命令停止本次服务并移除本次网络。生成的数据、聚合文件和临时密码保存在上面两个本地路径中。

```sh
docker compose -f deploy/docker-compose.yml -p "$mops_project" down
```
