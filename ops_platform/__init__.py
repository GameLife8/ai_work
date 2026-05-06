"""统一运维平台 kernel。

把"接入(Connection)、技能(Skill)、模型(Model)、用户/会话"四个维度从原本散落在
runtime.py / ops_agent/skills.py 中的硬编码抽出来，组成一个可被 chainlit / Flask API /
未来 MCP server 复用的执行内核。
"""

from ops_platform.context import SkillContext
from ops_platform.invoker import SkillInvoker
from ops_platform.registry import SkillRegistry
from ops_platform.connection_manager import ConnectionManager
from ops_platform.model_manager import ModelManager

__all__ = [
    "SkillContext",
    "SkillInvoker",
    "SkillRegistry",
    "ConnectionManager",
    "ModelManager",
]
