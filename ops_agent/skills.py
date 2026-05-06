"""[已弃用] 旧的硬编码 skill registry。

skill 现在通过 ``ops_platform.SkillRegistry`` + ``skills/`` 目录的插件机制管理。
本文件仅为兼容旧 import，新代码请使用 runtime.skill_registry。
"""

from ops_platform.registry import SkillRegistry as UnifiedSkillRegistry  # noqa: F401
