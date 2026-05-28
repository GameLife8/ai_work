"""``agent/agent.py`` 中 ``_build_argv`` 的行为回归测试。

为啥要专门测这个
================
``_build_argv`` 是节点诊断 agent 的核心：把平台传来的 ``{cmd, nsenter}`` 翻译成
真正要 subprocess.exec 的 argv。它的输出在两种部署形态下完全不一样：

  - ``AGENT_MODE=direct``：agent 自己有特权（K8s DaemonSet），直接 ``nsenter -t 1``。
  - ``AGENT_MODE=docker_proxy``：agent 无特权（老 Swarm 18.03），用 ``docker run``
    起一个 sibling 容器获得特权 + 进 host ns 的能力。

回归契约
========
1. docker_proxy + ``nsenter=""``（admin maintenance 扫 sibling 容器走这条路径）
   生成的 docker run argv **必须** 含 ``/var/run/docker.sock:/var/run/docker.sock``
   bind mount —— 否则 sibling 里的 docker CLI 看不到 host daemon socket，会报
   ``Cannot connect to the Docker daemon at unix:///var/run/docker.sock``。
2. docker_proxy + ``nsenter="muinp"`` 路径同样要带 docker.sock 挂载（保持对称；
   即使 nsenter 切到 host mount ns 后 bind 被"盖掉"，host 自己的 socket 就在
   同路径，所以挂着不冲突）。
3. direct 模式不走 docker run，``argv[0]`` 直接是 ``nsenter`` 或业务命令本身。
4. 白名单过滤、basename 匹配、nsenter 字符校验维持原状。
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def agent_mod(monkeypatch):
    """重新 import ``agent.agent`` 并把模块状态调成测试可控。

    ``_build_argv`` 读 ``AGENT_MODE`` / ``AGENT_TOOLS_IMAGE`` / ``DOCKER_BIN`` /
    ``_state['allowed_commands']`` —— 都是模块级常量/状态，必须 monkeypatch。
    """
    import agent.agent as a

    # 白名单：测试要用 sh / ss / cat
    monkeypatch.setitem(a._state, "allowed_commands", {"sh", "ss", "cat", "docker"})
    # docker_proxy 模式必须设镜像
    monkeypatch.setattr(a, "AGENT_TOOLS_IMAGE", "harbor.example/ai-ops-agent:test", raising=False)
    monkeypatch.setattr(a, "DOCKER_BIN", "docker", raising=False)
    return a


# ---------- docker_proxy 模式 ---------- #


def test_docker_proxy_no_nsenter_mounts_docker_sock(agent_mod, monkeypatch):
    """admin maintenance 扫 sibling 容器的 regression：
    cmd=["sh","-c","docker ps -a ..."] + nsenter="" 必须挂 docker.sock 才能 talk to host daemon。
    """
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "docker_proxy", raising=False)

    argv, _to, _mb = agent_mod._build_argv({
        "cmd": ["sh", "-c", "docker ps -a --filter status=exited"],
        "nsenter": "",
    })

    assert argv[0] == "docker"
    assert argv[1] == "run"
    # 关键断言：docker.sock 必须被挂进 sibling
    mount_args = [argv[i + 1] for i, v in enumerate(argv) if v == "--mount"]
    sock_mounts = [m for m in mount_args if "docker.sock" in m]
    assert sock_mounts, (
        f"docker_proxy 模式下 sibling 必须挂 /var/run/docker.sock，否则 docker CLI "
        f"报 'Cannot connect to the Docker daemon'。当前 argv: {argv}"
    )
    assert sock_mounts[0] == "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"

    # nsenter="" 时 entrypoint 直接是业务命令 sh，不是 nsenter
    assert "--entrypoint" in argv
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "sh"

    # sh 后跟 image，再跟 -c <script>
    image_idx = argv.index(agent_mod.AGENT_TOOLS_IMAGE)
    assert argv[image_idx + 1 : image_idx + 3] == ["-c", "docker ps -a --filter status=exited"]


def test_docker_proxy_with_nsenter_still_mounts_docker_sock(agent_mod, monkeypatch):
    """带 nsenter="muinp" 时 sibling 也带 docker.sock 挂载——对称即可，挂着不冲突。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "docker_proxy", raising=False)

    argv, _to, _mb = agent_mod._build_argv({
        "cmd": ["ss", "-ltnp"],
        "nsenter": "muinp",
    })

    # docker.sock 仍然要挂着（即使 nsenter 切了 mount ns 后 bind 看不到——
    # host 同路径上有真 socket，所以不冲突；这层保险让一次 image 适配两条路径）
    mount_args = [argv[i + 1] for i, v in enumerate(argv) if v == "--mount"]
    assert any("docker.sock" in m for m in mount_args), (
        f"docker_proxy 模式下 sibling 应一直挂 docker.sock 保持对称；argv: {argv}"
    )

    # 业务路径：entrypoint=nsenter，后面跟 nsenter flags + 业务命令
    ep_idx = argv.index("--entrypoint")
    assert argv[ep_idx + 1] == "nsenter"
    image_idx = argv.index(agent_mod.AGENT_TOOLS_IMAGE)
    # 镜像后立即是 ``-t 1 --mount --uts --ipc --net --pid -- ss -ltnp``
    assert argv[image_idx + 1 : image_idx + 4] == ["-t", "1", "--mount"]
    assert argv[-3:] == ["--", "ss", "-ltnp"]


def test_docker_proxy_keeps_pid_network_privileged(agent_mod, monkeypatch):
    """sibling 必须 --privileged --pid host --network host —— 没这仨 nsenter 进不了 host。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "docker_proxy", raising=False)

    argv, _to, _mb = agent_mod._build_argv({"cmd": ["cat", "/proc/uptime"], "nsenter": "muinp"})

    assert "--privileged" in argv
    pid_idx = argv.index("--pid")
    assert argv[pid_idx + 1] == "host"
    net_idx = argv.index("--network")
    assert argv[net_idx + 1] == "host"


# ---------- direct 模式 ---------- #


def test_direct_mode_no_nsenter_runs_cmd_inline(agent_mod, monkeypatch):
    """direct 模式 + ``nsenter=""`` 直接跑业务命令，不走 docker run。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)

    argv, _to, _mb = agent_mod._build_argv({"cmd": ["sh", "-c", "docker ps"], "nsenter": ""})
    assert argv == ["sh", "-c", "docker ps"]


def test_direct_mode_with_nsenter_wraps_with_nsenter_bin(agent_mod, monkeypatch):
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)

    argv, _to, _mb = agent_mod._build_argv({"cmd": ["ss", "-ltnp"], "nsenter": "muinp"})
    assert argv[0] == "nsenter"
    assert argv[1:3] == ["-t", "1"]
    # m/u/i/n/p 五个 flag 顺序映射
    assert set(argv[3:8]) == {"--mount", "--uts", "--ipc", "--net", "--pid"}
    assert argv[-3:] == ["--", "ss", "-ltnp"]


# ---------- 校验 / 边界 ---------- #


def test_whitelist_basename_match(agent_mod, monkeypatch):
    """允许 ``/usr/bin/ss``——按 basename 匹配白名单。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)
    argv, _to, _mb = agent_mod._build_argv({"cmd": ["/usr/bin/ss", "-ltn"], "nsenter": ""})
    assert argv[0] == "/usr/bin/ss"


def test_whitelist_rejects_unlisted(agent_mod, monkeypatch):
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)
    with pytest.raises(PermissionError, match="不在白名单"):
        agent_mod._build_argv({"cmd": ["rm", "-rf", "/"], "nsenter": ""})


def test_docker_proxy_rejects_direct_docker_call(agent_mod, monkeypatch):
    """docker_proxy 模式下用户不能直接调 docker——agent 自己起 sibling 那条才用 docker。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "docker_proxy", raising=False)
    with pytest.raises(PermissionError, match="不能直接调 docker"):
        agent_mod._build_argv({"cmd": ["docker", "ps"], "nsenter": ""})


def test_nsenter_bin_blocked_even_in_whitelist(agent_mod, monkeypatch):
    """``nsenter`` 不能由调用方直接传——agent 会自动包一层。"""
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)
    monkeypatch.setitem(agent_mod._state, "allowed_commands", {"nsenter", "ss"})
    with pytest.raises(PermissionError, match="不要直接调用"):
        agent_mod._build_argv({"cmd": ["nsenter", "-t", "1", "--", "ss"], "nsenter": ""})


def test_invalid_nsenter_char_rejected(agent_mod, monkeypatch):
    monkeypatch.setattr(agent_mod, "AGENT_MODE", "direct", raising=False)
    with pytest.raises(ValueError, match="nsenter 字符"):
        agent_mod._build_argv({"cmd": ["ss"], "nsenter": "muX"})
