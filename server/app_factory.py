"""Flask + Socket.IO 애플리케이션 조립."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flask import Flask, request
from flask_socketio import SocketIO

from . import config, security
from .app_config import AppConfig
from .assets import Assets, register_static
from .http_routes import register_routes
from .manager import RoomManager
from .socket_events import SocketApi, start_ticker
from .storage import RoomStore

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AuctionApp:
    flask: Flask
    socketio: SocketIO
    manager: RoomManager
    api: SocketApi
    cfg: AppConfig

    def start_background(self) -> None:
        start_ticker(self.socketio, self.manager, self.api)


def create_app(cfg: AppConfig, *, manager: Optional[RoomManager] = None) -> AuctionApp:
    app = Flask(
        __name__,
        static_folder=str(ROOT / "static"),
        static_url_path="/static",
        template_folder=str(ROOT / "templates"),
    )
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_REQUEST_BYTES
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    if cfg.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # type: ignore[method-assign]

    if manager is None:
        manager = RoomManager(RoomStore(cfg.data_dir), retention_days=cfg.retention_days, async_save=True)
        manager.load_from_disk()
        manager.purge_expired()

    assets = Assets.load(ROOT / "static")
    register_static(app, assets)

    @app.after_request
    def _headers(resp):
        is_static = request.path.startswith(("/static/", "/s/"))
        secure = cfg.secure or request.is_secure
        for k, v in security.security_headers(request.host, secure, is_static).items():
            resp.headers.setdefault(k, v)
        resp.headers.pop("Server", None)
        return resp

    admin_failures = security.FailureLimiter(limit=10, window=60, lockout=300)
    register_routes(app, manager, cfg, admin_failures)

    # cors_allowed_origins 를 지정하지 않으면 "같은 출처"의 웹소켓 연결만 허용된다 (Cross-Site WebSocket Hijacking 방지)
    sio = SocketIO(
        app,
        async_mode="threading",
        max_http_buffer_size=config.MAX_SOCKET_BYTES,
        ping_interval=10,
        ping_timeout=20,
        logger=False,
        engineio_logger=False,
        manage_session=False,
    )
    api = SocketApi(sio, manager, cfg)
    return AuctionApp(flask=app, socketio=sio, manager=manager, api=api, cfg=cfg)


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 요청 로그(werkzeug)와 엔진 로그는 토큰이 없더라도 시끄러우므로 경고 이상만 남긴다.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # 웹소켓 연결이 닫힐 때 개발 서버가 남기는 무해한 "Bad request syntax" 오류 줄은 숨긴다 (콘솔이 빨간 글씨로 가득 차지 않게).
    class _QuietWebsocketClose(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "Bad request" not in record.getMessage()

    logging.getLogger("werkzeug").addFilter(_QuietWebsocketClose())
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)
