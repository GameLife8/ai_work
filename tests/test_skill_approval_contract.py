"""审批 / 访问控制语义的设计契约测试。

钉死契约
========

1. ``visibility="admin"`` 已经从访问层把 skill 限制给 admin 用户(普通 user 的 LLM
   tool list 里根本看不到)。这种 skill 再加 ``requires_admin_approval=True`` 是冗余
   —— 审批人就是调用人,变成"自己审自己",UI 上还会显示"需 admin 审批"
   误导用户(尤其本人就是 admin 时)。

2. ``visibility="all"`` 的 skill 加 ``requires_admin_approval=True`` 是真实的
   职责分离场景:user 调起提议 → admin 审批拍板。这种**不该改**。

3. ``read_only=True`` 的 skill 不许设 ``requires_admin_approval=True`` —— 只读
   操作没有"批准"的概念。
"""

from __future__ import annotations

import importlib
import pkgutil

import skills as _skills_pkg


def _collect_manifests() -> dict[str, dict]:
    """扫 ``skills/`` 包,返回 ``{code: MANIFEST}``。"""
    out: dict[str, dict] = {}
    for info in pkgutil.iter_modules(_skills_pkg.__path__, prefix="skills."):
        mod = importlib.import_module(info.name)
        manifest = getattr(mod, "MANIFEST", None)
        if manifest and manifest.get("code"):
            out[manifest["code"]] = manifest
    return out


def test_no_admin_visibility_skill_requires_admin_approval() -> None:
    """关键设计契约:visibility=admin + requires_admin_approval=True 是冗余,
    任何这种组合都该被立即捕获。

    原因:
      - visibility=admin 已经把 skill 限制给 admin 用户(LLM tool list 不含)
      - 既然能调起的人都是 admin,审批人也只能是 admin —— "自己审自己"无意义
      - UI 还会显示"需 admin 审批",对一个 admin 用户来说纯属冗余信息

    要做"高危需二次拍板"的设计,应该改成 ``visibility=all``(user 调,admin 审)。
    """
    manifests = _collect_manifests()
    redundant = []
    for code, m in manifests.items():
        if m.get("visibility") == "admin" and m.get("requires_admin_approval"):
            redundant.append(code)
    assert redundant == [], (
        f"以下 skill 同时设了 visibility=admin + requires_admin_approval=True"
        f",这是冗余:{redundant}。\n"
        f"要么:1) requires_admin_approval=False(当前会话 admin 自己点确认即可)\n"
        f"要么:2) visibility=all(让普通 user 能调起,admin 来审批,真实分离)"
    )


def test_read_only_skill_does_not_require_approval() -> None:
    """只读 skill 不该有 ``requires_admin_approval=True`` —— 没"批准什么"可言。"""
    manifests = _collect_manifests()
    bad = []
    for code, m in manifests.items():
        if m.get("read_only") and m.get("requires_admin_approval"):
            bad.append(code)
    assert bad == [], f"只读 skill 不该需要审批:{bad}"


def test_high_risk_writes_still_need_some_confirmation() -> None:
    """确保关键写 skill 的执行控制没被改坏。

    至少这几个 skill 必须 read_only=False(否则就**没有 needs_confirmation 流程**了,
    模型一调直接执行,失控)。
    """
    manifests = _collect_manifests()
    must_be_writes = [
        "host_run_command", "host_run_command_async",
        "swarm_remove_service",
        "k8s_restart_deployment", "swarm_force_update_service",
        "swarm_scale_service", "swarm_update_service_image",
        "k8s_scale_deployment", "k8s_rollout_undo", "swarm_rollback_service",
    ]
    for code in must_be_writes:
        if code not in manifests:
            continue   # skill 删除了 / 重命名了 —— 不在本测试范围
        m = manifests[code]
        assert m["read_only"] is False, (
            f"写 skill ``{code}`` 不能设 read_only=True,否则绕开 needs_confirmation"
        )


def test_visibility_all_writes_keep_admin_approval() -> None:
    """visibility=all 的写 skill(user 能调),保留 admin approval 是真实的职责分离,
    钉死这点:这些 skill 仍然 requires_admin_approval=True。"""
    manifests = _collect_manifests()
    # 这些 skill 是 user 能 propose、admin 审批的典型场景
    must_keep_admin_approval = [
        "k8s_restart_deployment",
        "swarm_force_update_service",
    ]
    for code in must_keep_admin_approval:
        if code not in manifests:
            continue
        m = manifests[code]
        assert m.get("visibility") == "all", (
            f"``{code}`` 应当 visibility=all(让 user 能调起提议)"
        )
        assert m.get("requires_admin_approval") is True, (
            f"``{code}`` 是 user 调 + admin 审批的真实分离场景,"
            f"不能去 requires_admin_approval"
        )
