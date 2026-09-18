"""服务入口：python -m service.main 或 pyproject 中的 service 命令。

配置项（环境变量）：
- PORT        监听端口，默认 8000
- DB_PATH     SQLite 文件路径，默认 ./workbench.db；重启后数据保留，
              未完成的外部请求可通过 GET /external-requests/pending 恢复跟踪。
"""
import os

from .api import create_server
from .case_service import CaseService
from .db import Store


def build_service(db_path: str | None = None) -> CaseService:
    return CaseService(Store(db_path or os.getenv("DB_PATH", "workbench.db")))


def run():
    server = create_server(build_service(), port=int(os.getenv("PORT", "8000")))
    print(f"跨境争议客服工作台已启动: http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    run()
