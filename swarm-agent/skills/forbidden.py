from __future__ import annotations


def forbidden_action(**kwargs) -> dict:
    raise PermissionError("该操作已被系统禁止，建议改用查询、扩缩容、回滚等安全手段。")
