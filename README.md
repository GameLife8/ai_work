# AI Alert Flask

一个按 `ai_alert_flask_project.md` 搭出来的最小可运行版本，目标是先把告警接入、AI 规划/研判、上下文查询、incident 管理和存储切换链路跑通。

## 运行

1. 安装依赖
```powershell
python -m pip install -r requirements.txt
```

2. 复制环境变量
```powershell
Copy-Item .env.example .env
```

3. 启动服务
```powershell
python app.py
```

## 关键接口

- `POST /api/v1/alerts/zabbix`
- `GET /health`
- `GET /api/v1/system/storage`

## 数据库模式

默认使用内存模式。

如果要切到真实数据库：

```powershell
$env:STORE_BACKEND="sql"
$env:DATABASE_URL="sqlite:///local.db"
python scripts/init_db.py
python scripts/check_storage.py
```

如果要接 TiDB，把 `.env` 里的 `DATABASE_URL` 改成：

```text
mysql+pymysql://ai_alert:ai_alert@169.24.1.87:4000/ai_alert?charset=utf8mb4
```

## Zabbix

默认使用 stub 指标。

如果要接真实 Zabbix：

```powershell
$env:USE_STUB_ZABBIX="false"
$env:ZABBIX_BASE_URL="http://169.24.2.90:80/zabbix/api_jsonrpc.php"
$env:ZABBIX_USERNAME="your-user"
$env:ZABBIX_PASSWORD="your-password"
python scripts/check_zabbix.py
```

拉某台主机的指标摘要可以用：

```powershell
$env:USE_STUB_ZABBIX="false"
$env:ZABBIX_BASE_URL="http://169.24.2.90:80/zabbix/api_jsonrpc.php"
$env:ZABBIX_USERNAME="your-user"
$env:ZABBIX_PASSWORD="your-password"
python scripts/check_metric_summary.py "app-prod-01" "10.0.0.12"
```

## 测试

```powershell
python -m pytest -q
```
