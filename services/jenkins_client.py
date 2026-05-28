"""Jenkins HTTP client。

针对老版本 Jenkins (2.190 实测) 设计:
- 鉴权:Basic Auth (admin password or admin/api_token);新老版本都兼容
- CSRF crumb:自动探测;``useCrumbs=False`` 的实例直接跳过(本部署即如此)
- 连接池:``requests.Session`` 复用 TCP+TLS,避免每次 list_jobs 都重新握手
- 路径处理:job 名带 ``/`` 时需要 URL-encode (folder 嵌套)——如 ``app/build`` 实际是
  ``/job/app/job/build/``;client 提供 helper 自动处理

API 参考
========
Jenkins 公开 REST API:每个 URL 加 ``/api/json``:
- ``/api/json``                              —— 顶层(jobs/views)
- ``/job/{name}/api/json``                   —— job 详情
- ``/job/{name}/{n}/api/json``               —— build 详情
- ``/job/{name}/{n}/consoleText``            —— build 控制台输出
- ``/queue/api/json``                        —— 排队中任务
- ``/computer/api/json``                     —— 节点(master + agents)
- ``/job/{name}/build`` (POST)               —— 触发无参构建
- ``/job/{name}/buildWithParameters`` (POST) —— 触发带参构建

每个 GET 都支持 ``?tree=...`` 字段裁剪(省 token)和 ``?depth=N`` 嵌套展开。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)


@dataclass
class JenkinsResult:
    """统一返回封装。``ok`` False 时 ``error`` 含原因。"""
    ok: bool
    status_code: int
    data: Any                  # JSON 解析后的对象(GET .../api/json)或文本(consoleText)
    error: str = ""
    url: str = ""              # 实际请求的 URL,便于排错


class JenkinsClient:
    """Jenkins HTTP API 薄封装。

    线程安全:``requests.Session`` 内部用 ``urllib3.PoolManager`` 是线程安全的,
    多个 skill 调用同一个 client 不需要外加锁。
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        api_token: str = "",
        password: str = "",
        timeout_seconds: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        self.username = username
        # api_token 和 password 二选一(都填优先 api_token,Jenkins 安全建议)
        self._secret = api_token or password
        self.timeout_seconds = max(5, int(timeout_seconds or 30))
        self.verify_ssl = bool(verify_ssl)

        self._session = requests.Session()
        self._session.verify = self.verify_ssl
        if username and self._secret:
            self._session.auth = (username, self._secret)
        # Connection 池调大,适应 25 并发 session 同时查
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        # 默认期望 JSON;Jenkins 老版本对 Accept 不严格,带上无坏处
        self._session.headers.update({"Accept": "application/json"})

        # CSRF crumb 缓存:Jenkins 老版本不一定开,首次调用时探测一次
        self._crumb: tuple[str, str] | None = None
        self._crumb_probed = False

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:    # pragma: no cover
            pass

    # ---------- helpers ----------

    @staticmethod
    def _encode_job_path(name: str) -> str:
        """``audit-management`` → ``job/audit-management``,``app/sub`` → ``job/app/job/sub``。

        Jenkins REST URL 约定:所有 job 路径都以 ``job/`` 开头;folder 嵌套时
        每层都要再加 ``job/`` 段(URL 编码避免特殊字符)。
        """
        if not name:
            raise ValueError("job 名不能为空")
        # 已经是 job/x/job/y 形式直接返回(便于内部 chain)
        if name.startswith("job/"):
            return name
        parts = [p for p in name.split("/") if p]
        return "job/" + "/job/".join(quote(p, safe="") for p in parts)

    def _ensure_crumb(self) -> None:
        """如果是开了 useCrumbs 的 Jenkins,POST 必须带 ``Jenkins-Crumb`` 头。
        探测一次缓存结果。404/403 → useCrumbs=False,以后不再探。"""
        if self._crumb_probed:
            return
        self._crumb_probed = True
        try:
            r = self._session.get(
                f"{self.base_url}/crumbIssuer/api/json",
                timeout=self.timeout_seconds,
            )
            if r.status_code == 200:
                j = r.json()
                self._crumb = (j.get("crumbRequestField", "Jenkins-Crumb"),
                               j.get("crumb", ""))
                logger.info("Jenkins useCrumbs=true; crumb 已缓存")
            else:
                # 404 (老版本) / 403 (关掉了 CSRF) 都视为无 crumb 需求
                logger.debug("Jenkins crumbIssuer 返回 %d,视为 useCrumbs=False", r.status_code)
        except Exception as exc:    # pragma: no cover
            logger.warning("Jenkins crumb 探测失败 (%s),POST 写操作可能 403", exc)

    # ---------- low-level ----------

    def get(self, path: str, *, params: dict | None = None) -> JenkinsResult:
        """GET ``base_url + path``。``path`` 自带前导 ``/``。"""
        url = f"{self.base_url}{path}"
        try:
            r = self._session.get(url, params=params, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            return JenkinsResult(ok=False, status_code=-1, data=None,
                                 error=f"network error: {exc}", url=url)
        if r.status_code >= 400:
            # Jenkins 错误页是 HTML,截前 500 字方便 debug
            return JenkinsResult(ok=False, status_code=r.status_code, data=None,
                                 error=r.text[:500], url=r.url)
        # consoleText 等是纯文本,其他大多是 JSON
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                data = r.json()
            except ValueError as exc:
                return JenkinsResult(ok=False, status_code=r.status_code, data=None,
                                     error=f"bad JSON: {exc}", url=r.url)
        else:
            data = r.text
        return JenkinsResult(ok=True, status_code=r.status_code, data=data, url=r.url)

    def post(self, path: str, *, params: dict | None = None,
             data: dict | None = None) -> JenkinsResult:
        """POST(写操作专用,会自动带 crumb)。"""
        self._ensure_crumb()
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if self._crumb:
            headers[self._crumb[0]] = self._crumb[1]
        try:
            r = self._session.post(url, params=params, data=data, headers=headers,
                                   timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            return JenkinsResult(ok=False, status_code=-1, data=None,
                                 error=f"network error: {exc}", url=url)
        if r.status_code >= 400:
            return JenkinsResult(ok=False, status_code=r.status_code, data=None,
                                 error=r.text[:500], url=r.url)
        # 触发 build:Jenkins 返回 201 Created + Location header 指向 queue item
        # 我们把 Location 也回传方便 caller 跟进
        return JenkinsResult(
            ok=True, status_code=r.status_code,
            data={"location": r.headers.get("Location", "")},
            url=r.url,
        )

    # ---------- high-level (skill 用) ----------

    def healthcheck(self) -> dict[str, Any]:
        """admin 表单"验证 Config"用。"""
        r = self.get("/api/json", params={"tree": "numExecutors,useSecurity,useCrumbs"})
        if not r.ok:
            return {"healthy": False, "error": r.error, "status_code": r.status_code}
        return {
            "healthy": True,
            "version_header_only": True,
            "details": r.data,
        }

    def list_jobs(self, *, depth: int = 0, tree: str | None = None) -> JenkinsResult:
        """列所有 job(顶层 + folder 内)。

        默认 tree 选关键字段省 token: ``jobs[name,url,color,_class,lastBuild[number,result]]``。
        想拉更多就传 tree;想递归看文件夹内层就 depth=2。
        """
        params = {"tree": tree or "jobs[name,url,color,_class,lastBuild[number,result,timestamp]],numExecutors,mode"}
        if depth:
            params["depth"] = str(depth)
        return self.get("/api/json", params=params)

    def get_job(self, name: str, *, tree: str | None = None) -> JenkinsResult:
        path = f"/{self._encode_job_path(name)}/api/json"
        params = {"tree": tree} if tree else None
        return self.get(path, params=params)

    def get_build(self, name: str, build_number: int | str = "lastBuild",
                  *, tree: str | None = None) -> JenkinsResult:
        """build_number 支持 int 或 'lastBuild' / 'lastSuccessfulBuild' / 'lastFailedBuild'。"""
        path = f"/{self._encode_job_path(name)}/{build_number}/api/json"
        params = {"tree": tree} if tree else None
        return self.get(path, params=params)

    def get_console(self, name: str, build_number: int | str = "lastBuild",
                    *, tail: int | None = None) -> JenkinsResult:
        """build 控制台输出。``tail`` 限制返回的尾部行数(超长时 caller 控)。"""
        path = f"/{self._encode_job_path(name)}/{build_number}/consoleText"
        r = self.get(path)
        if r.ok and tail and isinstance(r.data, str):
            lines = r.data.splitlines()
            if len(lines) > tail:
                r = JenkinsResult(
                    ok=True, status_code=r.status_code,
                    data="\n".join(lines[-tail:]),
                    url=r.url,
                )
        return r

    def list_queue(self) -> JenkinsResult:
        return self.get("/queue/api/json",
                        params={"tree": "items[id,task[name,url],why,inQueueSince,stuck,blocked]"})

    def list_nodes(self) -> JenkinsResult:
        return self.get("/computer/api/json",
                        params={"tree": "computer[displayName,offline,temporarilyOffline,"
                                "offlineCause[*],numExecutors,monitorData[*]]"})

    def trigger_build(self, name: str, *, params: dict | None = None) -> JenkinsResult:
        """触发构建。``params=None`` 走 ``/build``,否则走 ``/buildWithParameters``。"""
        encoded = self._encode_job_path(name)
        if params:
            return self.post(f"/{encoded}/buildWithParameters", data=params)
        return self.post(f"/{encoded}/build")
