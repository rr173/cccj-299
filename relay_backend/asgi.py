"""uvicorn 入口模块。

启动方式::

    RELAY_DB=/path/to/relay.db uvicorn relay_backend.asgi:app

数据库位置由环境变量 ``RELAY_DB`` 控制，默认 ``relay.db``（当前工作目录）。
"""

from __future__ import annotations

import os

from .main import create_app

app = create_app(os.environ.get("RELAY_DB", "relay.db"))
