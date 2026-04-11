# AI Ops Platform

这是一个统一的智能运维平台项目，不再区分“告警项目”和“Swarm 项目”。

当前项目里有三类核心能力：

- 告警接入与智能研判
- Zabbix 主机信息查询
- Docker Swarm 服务排障与状态查询

这三类能力现在共用同一个模型入口、同一套运行时和同一批 skill。

## 当前入口

API 服务：

```powershell
python app.py
```

统一前端对话入口：

```powershell
chainlit run chainlit_app.py --host 0.0.0.0 --port 8000
```

## 能做什么

1. 告警研判  
接收 Zabbix 或通知脚本发来的告警，自动补充上下文，交给火山模型做优先级、处置建议和报告判断。

2. 主机查询  
用户在前端直接问：

```text
我想了解 WYY-DB09 主机的所有硬盘当前情况
```

模型会自动调用 Zabbix skill，返回主机磁盘、CPU、内存、可用性等信息。

3. 容器排障  
用户在前端直接问：

```text
帮我看看 iiot-haitu_seatable 这个服务为什么起不来
```

模型会自动调用 Docker Swarm skill，检查服务健康、失败任务、错误日志、服务详情，并给出中文诊断报告。

## 统一架构

- [app.py](C:\Users\fengxiuli\Desktop\ai_alert\app.py)：Flask API 入口
- [chainlit_app.py](C:\Users\fengxiuli\Desktop\ai_alert\chainlit_app.py)：统一前端对话入口
- [runtime.py](C:\Users\fengxiuli\Desktop\ai_alert\runtime.py)：统一运行时装配
- [ops_agent/agent.py](C:\Users\fengxiuli\Desktop\ai_alert\ops_agent\agent.py)：统一模型调度器
- [ops_agent/skills.py](C:\Users\fengxiuli\Desktop\ai_alert\ops_agent\skills.py)：统一 skill registry
- [services/zabbix_client.py](C:\Users\fengxiuli\Desktop\ai_alert\services\zabbix_client.py)：Zabbix 数据查询
- [services/docker_swarm_client.py](C:\Users\fengxiuli\Desktop\ai_alert\services\docker_swarm_client.py)：Docker Swarm 查询
- [services/alert_service.py](C:\Users\fengxiuli\Desktop\ai_alert\services\alert_service.py)：落库版告警处理
- [services/alert_analysis_service.py](C:\Users\fengxiuli\Desktop\ai_alert\services\alert_analysis_service.py)：不落库版告警分析 skill

## 运行方式

1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

2. 复制环境变量模板

```powershell
Copy-Item .env.example .env
```

3. 配置 `.env`

至少要关注：

- `ZABBIX_BASE_URL`
- `ZABBIX_USERNAME`
- `ZABBIX_PASSWORD`
- `AI_BASE_URL`
- `AI_API_KEY`
- `AI_MODEL`
- `DOCKER_RUNNER`
- `DOCKER_HOST`

如果本机没有 Windows Docker Client，但 WSL 里有：

```ini
DOCKER_RUNNER=wsl
WSL_DISTRO=Ubuntu
DOCKER_HOST=tcp://169.24.216.227:3389
```

## 常用接口

- `POST /api/v1/alerts/zabbix`
- `POST /api/v1/alerts/notification`
- `POST /api/v1/alerts/wechat`
- `GET /health`
- `GET /api/v1/system/storage`
- `POST /api/v1/system/storage/cleanup`

## 命令行测试

统一 agent 测试脚本：

```powershell
python scripts/run_ops_agent_case.py "帮我看看 iiot-haitu_seatable 这个服务为什么起不来"
```

## 当前已验证

1. 统一 agent 已能用 Docker Swarm skill 诊断 `iiot-haitu_seatable`
2. 已定位真实根因：`JAVA_OPTS` 缺少 `-D` 前的空格，导致 JVM 把参数当成主类名
3. 已能通过 Zabbix skill 查询 `WYY-DB09` 的所有挂载点容量信息
