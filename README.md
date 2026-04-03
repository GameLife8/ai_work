# AI Alert Flask

这是一个告警接入与智能研判服务。

它的职责是：

- 接收 Zabbix 或通知脚本发来的告警
- 到 Zabbix 查询相关真实上下文
- 把告警和上下文发给火山模型做研判
- 处理去重、并单、恢复关闭、工单字段
- 把告警、AI 计划、上下文、决策、incident 信息落库

## 快速开始

1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

2. 复制环境变量模板

```powershell
Copy-Item .env.example .env
```

3. 修改 `.env`

至少需要关注：

- `STORE_BACKEND`
- `DATABASE_URL`
- `USE_STUB_ZABBIX`
- `ZABBIX_BASE_URL`
- `ZABBIX_USERNAME`
- `ZABBIX_PASSWORD`
- `USE_STUB_AI`
- `AI_API_KEY`

4. 启动服务

```powershell
python app.py
```

## 常用接口

- `POST /api/v1/alerts/zabbix`
- `POST /api/v1/alerts/notification`
- `POST /api/v1/alerts/wechat`
- `GET /health`
- `GET /api/v1/system/storage`
- `POST /api/v1/system/storage/cleanup`

## 运行机制

当前系统的核心特点：

- 告警进入后会先解析成结构化对象
- 会从 Zabbix 查询最近 1 小时、每 5 分钟 1 次、共 12 个点的样本
- 程序负责查准数据，火山模型负责做研判
- 同一类告警 24 小时内只推一次
- 超过 24 小时未恢复，再次收到会重新推送
- 已恢复告警会关闭对应 incident
- `notify/merge` 且高优先级告警默认会保留转工单字段

## 详细说明

完整操作文档见：

- [项目操作与模型交互说明](C:\Users\fengxiuli\Desktop\ai_alert\docs\项目操作与模型交互说明.md)

这份文档包含：

- 项目怎么启动和配置
- 程序和火山模型的交互方式
- 服务上线后是否还需要人工介入
- 当前去重、incident、工单字段和数据库存储规则

## 测试

```powershell
python -m pytest -q
```
