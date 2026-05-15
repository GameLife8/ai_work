"""Host 磁盘 / 文件系统概览 —— 一次性拉 ``df -h`` + ``mount`` + 关键挂载点的
``du --max-depth=1`` 子目录占用 + 大文件抽样。

跟 ``zabbix_get_host_storage_overview`` 区别：
  - 后者走 Zabbix 历史采样数据（粒度受 trapper 限制，看的是过去某时刻）
  - 本 skill 走 host_agent 进**宿主机**实时取证（df 当下、目录占用细节）

跟 ``host_run_command`` 区别：
  - host_run_command 是 admin 审批的逃生口，太重
  - 本 skill **read_only**，普通用户即可调用，是 disk 高水位告警的**首选**

输出 + signal pivot
-------------------
- 每个挂载点 use_percent ≥ 80 → SIG_DISK_PRESSURE warning
- ≥ 90 → critical，建议下一步追 ``host_kernel_events`` 看 fs error
"""

from __future__ import annotations

import re

from ops_platform.signals import (
    SEV_CRITICAL,
    SEV_WARNING,
    SIG_DISK_PRESSURE,
    SIG_NO_SPACE,
    attach,
    signal,
)


MANIFEST = {
    "code": "host_storage_overview",
    "name": "宿主机磁盘 + 挂载点概览",
    "description": (
        "拉取指定节点的实时磁盘状态：``df -h`` 全挂载点使用率 + ``mount`` 挂载选项 + "
        "可选 ``du --max-depth=1 <path>`` 看子目录占用 + ``find <path> -size +500M`` "
        "找近期大文件。"
        "**典型场景**：(1) 主机 /xxx 目录高水位告警的第一手取证；"
        "(2) 怀疑某容器/服务把宿主机磁盘吃满了，定位是哪个子目录；"
        "(3) 排查 Docker /var/lib/docker 或 /data 高占用。"
        "返回的 _signals 自动标出 use% ≥ 80 / ≥ 90 的高危挂载点，并建议下一步调"
        "``host_kernel_events`` 看是否有 ext4/xfs 错误。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "path": {
                "type": "string",
                "description": (
                    "可选；指定一个挂载点路径，会额外跑 ``du --max-depth=1 <path>`` 和"
                    "``find <path> -size +500M -mtime -7`` 找大/近文件。例：``/data``"
                ),
            },
            "large_file_min_mb": {
                "type": "integer", "default": 500, "minimum": 100, "maximum": 10240,
                "description": "find 大文件阈值（MB），仅当 path 给定时生效",
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


# df -h 输出每行例：
# Filesystem  Size  Used Avail Use%  Mounted on
# /dev/sdb1   100G   51G   50G  51%  /data
_DF_LINE = re.compile(
    r"^(?P<fs>\S+)\s+(?P<size>\S+)\s+(?P<used>\S+)\s+(?P<avail>\S+)\s+"
    r"(?P<pct>\d+)%\s+(?P<mount>\S.*)$"
)


def _parse_df(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        m = _DF_LINE.match(line.strip())
        if not m:
            continue
        rows.append({
            "filesystem":  m["fs"],
            "size":        m["size"],
            "used":        m["used"],
            "avail":       m["avail"],
            "use_percent": int(m["pct"]),
            "mounted_on":  m["mount"].strip(),
        })
    return rows


def _filter_real_mounts(rows: list[dict]) -> list[dict]:
    """剔除 tmpfs / shm / overlay / devtmpfs / squashfs 这类容器虚拟挂载——
    告警关心的是真实磁盘。"""
    junk = ("tmpfs", "shm", "overlay", "devtmpfs", "squashfs", "fuse.lxcfs")
    junk_prefix = (
        "/run/", "/sys/", "/dev/", "/proc/",
        "/var/lib/docker/containers/", "/var/lib/docker/overlay2/",
        "/var/lib/kubelet/pods/", "/var/lib/containerd/",
    )
    out = []
    for r in rows:
        if r["filesystem"] in junk:
            continue
        if any(r["mounted_on"].startswith(p) for p in junk_prefix):
            continue
        out.append(r)
    return out


def _extract_signals(node: str, real_rows: list[dict]) -> list[dict]:
    sigs: list[dict] = []
    for r in real_rows:
        pct = r["use_percent"]
        mount = r["mounted_on"]
        if pct >= 90:
            sigs.append(signal(
                SIG_NO_SPACE if pct >= 95 else SIG_DISK_PRESSURE,
                severity=SEV_CRITICAL,
                evidence=(
                    f"节点 {node} 挂载点 {mount} 使用率 {pct}% "
                    f"({r['used']}/{r['size']})，仅剩 {r['avail']}"
                ),
                next_skill="host_kernel_events",
                next_args={"node": node, "keyword": "ext4|xfs|i/o error|no space"},
                context={"node": node, "mount": mount, "use_percent": pct},
            ))
        elif pct >= 80:
            sigs.append(signal(
                SIG_DISK_PRESSURE, severity=SEV_WARNING,
                evidence=f"节点 {node} 挂载点 {mount} 使用率 {pct}%，接近高水位",
                context={"node": node, "mount": mount, "use_percent": pct},
            ))
    return sigs


def run(
    ctx,
    *,
    node: str,
    path: str | None = None,
    large_file_min_mb: int = 500,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)

    # 1) df -h（agent 直接调 df，不走 sh）
    df_res = client.nsenter_on_node(node, ["df", "-h"])
    rows = _parse_df(df_res.stdout)
    real_rows = _filter_real_mounts(rows)

    # 2) mount —— 取真实挂载行
    mnt_res = client.nsenter_on_node(node, ["mount"])
    mount_lines = []
    for line in (mnt_res.stdout or "").splitlines():
        if any(t in line for t in ["ext4", "xfs", "ext3", "btrfs"]) and " on " in line:
            mount_lines.append(line)

    payload: dict = {
        "node": node,
        "all_mounts": rows,                  # 包含 tmpfs，给 debug
        "real_mounts": real_rows,            # 真磁盘，告警关心这个
        "mount_options": mount_lines,
        "ok": df_res.ok and mnt_res.ok,
    }

    # 3) 可选 path 的细节
    if path:
        # du --max-depth=1 (agent 直接调 du，不走 sh)
        du_res = client.nsenter_on_node(node, ["du", "-sh", "--max-depth=1", path])
        payload["du_subdirs"] = (du_res.stdout or "")[:8000]
        payload["du_ok"] = du_res.ok

        # find 大文件 —— 这里用 sh -c 因为要做 sort | head 复杂管道；
        # 依赖 v1.2+ agent 白名单已含 sh
        threshold_kb = int(large_file_min_mb) * 1024
        find_cmd = ["sh", "-c",
                    f"find {path} -xdev -type f -size +{threshold_kb}k -mtime -7 "
                    f"-printf '%TY-%Tm-%Td %s %p\\n' 2>/dev/null | sort -k2 -rn | head -20"]
        find_res = client.nsenter_on_node(node, find_cmd)
        payload["large_recent_files"] = (find_res.stdout or "")[:8000]
        payload["large_recent_files_threshold_mb"] = large_file_min_mb

    return attach(payload, _extract_signals(node, real_rows))
