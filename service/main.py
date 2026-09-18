"""服务入口：启动 HTTP 服务，并在启动时恢复未完成的外部请求。

环境变量：
- PORT：监听端口（默认 8000）
- DATABASE_PATH：SQLite 文件路径（默认 workbench.db），重启后案件与在途请求继续可追踪
"""
import os

from .app import AppState, serve
from .db import Database
from .workbench import ExternalGateway


class LoggingGateway(ExternalGateway):
    """默认网关：真实环境替换为收单行/商户/翻译的 HTTP 客户端。"""

    def send(self, request: dict) -> bool:
        # 占位：集成时在此发起外部调用；当前仅表示已可发出
        return True


def build_state() -> AppState:
    db_path = os.getenv("DATABASE_PATH", os.path.join(os.getcwd(), "workbench.db"))
    state = AppState(Database(db_path), gateway=LoggingGateway())
    # 程序再次运行：未完成（PENDING/DISPATCHED）的外部请求继续可追踪
    state.workbench.recover_external_requests()
    return state


def run():
    state = build_state()
    httpd = serve(state)
    try:
        httpd.serve_forever()
    finally:
        state.db.close()


if __name__ == "__main__":
    run()
