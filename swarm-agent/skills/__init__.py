from skills.config_query import get_config_detail, list_configs
from skills.forbidden import forbidden_action
from skills.log_query import get_service_logs
from skills.network_query import get_network_detail, list_networks
from skills.node_query import get_cluster_info, get_node_detail, list_nodes
from skills.node_write import update_node_availability
from skills.service_query import (
    get_service_detail,
    get_service_status,
    get_service_tasks,
    list_services,
)
from skills.service_write import force_restart_service, scale_service, update_service_image
from skills.stack_query import get_stack_services, list_stacks

TOOL_REGISTRY = {
    "list_services": list_services,
    "get_service_detail": get_service_detail,
    "get_service_status": get_service_status,
    "get_service_tasks": get_service_tasks,
    "get_service_logs": get_service_logs,
    "list_nodes": list_nodes,
    "get_node_detail": get_node_detail,
    "list_networks": list_networks,
    "get_network_detail": get_network_detail,
    "list_stacks": list_stacks,
    "get_stack_services": get_stack_services,
    "list_configs": list_configs,
    "get_config_detail": get_config_detail,
    "get_cluster_info": get_cluster_info,
    "update_service_image": update_service_image,
    "scale_service": scale_service,
    "force_restart_service": force_restart_service,
    "update_node_availability": update_node_availability,
    "remove_service": forbidden_action,
    "remove_network": forbidden_action,
    "remove_node": forbidden_action,
    "leave_swarm": forbidden_action,
    "init_swarm": forbidden_action,
}
