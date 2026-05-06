"""管理员后台 Flask 蓝图。

挂在主 app.py 上，统一以 ``/admin/api/v1/...`` 暴露 JSON API。
前端 Vue (admin_ui/) 通过 axios 调用这些接口。
"""

from admin_app.blueprint import admin_bp, init_admin

__all__ = ["admin_bp", "init_admin"]
