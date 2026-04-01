# AI Alert Flask

一个按 `ai_alert_flask_project.md` 搭出来的最小可运行版本，目标是先把告警接入、AI 规划/研判、上下文查询、incident 管理和存储切换链路跑通。

目前已经内置两类更细的研判：

- `磁盘空间不足`：会解析挂载点，并补充空间大小、剩余空间、24 小时增长趋势
- `高 CPU`：会补充平均值、峰值和负载信息

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
- `POST /api/v1/alerts/notification`
- `POST /api/v1/alerts/wechat`
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

## 通知脚本接入

如果你把原来的企业微信脚本改成调用本服务，推荐直接发 JSON 到：

```text
POST /api/v1/alerts/notification
```

示例字段：

```json
{
  "to_user": "77301",
  "subject": "Zabbix告警通知",
  "alert_message": "/mnt/data01: 磁盘空间严重不足 (used > 90%)",
  "alert_detail": "WYY-DB09 (169.24.7.117) Problem in 2026.04.01 11:25:15"
}
```

当前系统已内置更细的处理类型：

- 磁盘空间不足
- 高 CPU
- 内存使用率过高
- 磁盘 IO / 磁盘吞吐异常
- 主机不可达 / 关机 / agent 不可用

## 测试

```powershell
python -m pytest -q
```
