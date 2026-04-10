# Swarm Agent

`swarm-agent` 是一个独立于现有告警服务的 Docker Swarm 运维对话代理。

它的设计原则是：

- 界面使用 Chainlit
- 连接 Swarm 只走 `docker` 命令行和 `DOCKER_HOST`
- 不使用 Docker SDK
- 只读操作直接执行
- 写操作必须二次确认
- 禁止操作直接拦截

## 目录

- `app.py`: Chainlit 入口
- `agent.py`: 意图路由与技能调度
- `config.py`: 配置读取
- `docker_cli.py`: Docker CLI 封装
- `permissions.py`: 权限分层
- `tools_schema.py`: 技能说明与参数定义
- `skills/`: 各类只读/写入/禁止技能

## 运行

1. 复制环境变量模板

```powershell
Copy-Item .env.example .env
```

2. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

3. 启动 Chainlit

```powershell
chainlit run app.py --host 0.0.0.0 --port 8000
```

## 配置重点

- `DOCKER_HOST=tcp://169.24.216.227:3389`
- `DOCKER_BIN=docker`
- `MODEL_API_KEY`: OpenAI 兼容接口 API Key
- `MODEL_BASE_URL`: OpenAI 兼容接口地址
- `MODEL_NAME`: 模型名

如果需要兼容旧方案中的命名，也支持：

- `QWEN_API_KEY`
- `QWEN_BASE_URL`
- `QWEN_MODEL`

## 当前能力

只读：

- 查看服务列表
- 查看服务详情
- 查看服务状态
- 查看服务任务
- 查看服务日志
- 查看节点
- 查看网络
- 查看 Stack
- 查看 Config
- 查看集群概览

写操作：

- 更新服务镜像
- 扩缩容服务
- 强制重启服务
- 切换节点可用性

禁止：

- 删除服务
- 删除网络
- 删除节点
- 离开或初始化 Swarm

## Docker 化

构建：

```powershell
docker build -t swarm-agent-test .
```

运行：

```powershell
docker run --rm -p 8000:8000 --env-file .env swarm-agent-test
```

K8s 测试时只需要把 `.env` 中的远端 Docker API 和模型地址配置好即可。
