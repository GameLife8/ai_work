from __future__ import annotations

READ_ONLY_TOOLS = {
    "list_services",
    "get_service_detail",
    "get_service_status",
    "get_service_tasks",
    "get_service_logs",
    "list_nodes",
    "get_node_detail",
    "list_networks",
    "get_network_detail",
    "list_stacks",
    "get_stack_services",
    "list_configs",
    "get_config_detail",
    "get_cluster_info",
}

WRITE_TOOLS = {
    "update_service_image",
    "scale_service",
    "force_restart_service",
    "update_node_availability",
}

FORBIDDEN_TOOLS = {
    "remove_service",
    "remove_network",
    "remove_node",
    "leave_swarm",
    "init_swarm",
}


def get_tool_level(tool_name: str) -> str:
    if tool_name in READ_ONLY_TOOLS:
        return "read"
    if tool_name in WRITE_TOOLS:
        return "write"
    if tool_name in FORBIDDEN_TOOLS:
        return "forbidden"
    return "unknown"


def ensure_allowed(tool_name: str) -> None:
    level = get_tool_level(tool_name)
    if level == "forbidden":
        raise PermissionError(f"操作 {tool_name} 已被系统禁止。")
    if level == "unknown":
        raise PermissionError(f"未注册的操作 {tool_name}，已拒绝执行。")
