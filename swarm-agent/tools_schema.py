from __future__ import annotations

TOOLS = [
    {
        "name": "list_services",
        "description": "列出 Swarm 服务，返回名称、模式、副本、镜像。",
        "parameters": {
            "type": "object",
            "properties": {
                "filter_name": {"type": "string", "description": "按服务名模糊过滤"}
            },
        },
    },
    {
        "name": "get_service_detail",
        "description": "查看单个服务完整详情。",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "get_service_status",
        "description": "查看服务当前状态和副本情况。",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "get_service_tasks",
        "description": "查看服务任务副本与节点分布。",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "get_service_logs",
        "description": "查看服务日志。",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "tail": {"type": "integer"},
                "since": {"type": "string"},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "list_nodes",
        "description": "列出 Swarm 节点。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_node_detail",
        "description": "查看节点详情。",
        "parameters": {
            "type": "object",
            "properties": {"node_name": {"type": "string"}},
            "required": ["node_name"],
        },
    },
    {
        "name": "list_networks",
        "description": "列出 Swarm 网络。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_network_detail",
        "description": "查看网络详情。",
        "parameters": {
            "type": "object",
            "properties": {"network_name": {"type": "string"}},
            "required": ["network_name"],
        },
    },
    {
        "name": "list_stacks",
        "description": "列出 Stack。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stack_services",
        "description": "查看 Stack 下服务。",
        "parameters": {
            "type": "object",
            "properties": {"stack_name": {"type": "string"}},
            "required": ["stack_name"],
        },
    },
    {
        "name": "list_configs",
        "description": "列出 Docker Config。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_config_detail",
        "description": "查看 Config 详情。",
        "parameters": {
            "type": "object",
            "properties": {"config_name": {"type": "string"}},
            "required": ["config_name"],
        },
    },
    {
        "name": "get_cluster_info",
        "description": "查看集群概览。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "update_service_image",
        "description": "更新服务镜像，危险操作，需要确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "new_image": {"type": "string"},
            },
            "required": ["service_name", "new_image"],
        },
    },
    {
        "name": "scale_service",
        "description": "调整服务副本数，危险操作，需要确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "replicas": {"type": "integer"},
            },
            "required": ["service_name", "replicas"],
        },
    },
    {
        "name": "force_restart_service",
        "description": "强制重启服务，危险操作，需要确认。",
        "parameters": {
            "type": "object",
            "properties": {"service_name": {"type": "string"}},
            "required": ["service_name"],
        },
    },
    {
        "name": "update_node_availability",
        "description": "调整节点可用性，危险操作，需要确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "node_name": {"type": "string"},
                "availability": {"type": "string", "enum": ["active", "pause", "drain"]},
            },
            "required": ["node_name", "availability"],
        },
    },
]


def get_tool_schema(name: str) -> dict | None:
    for tool in TOOLS:
        if tool["name"] == name:
            return tool
    return None
