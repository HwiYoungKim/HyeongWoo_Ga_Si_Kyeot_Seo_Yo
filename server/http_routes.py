"""HTTP 라우트: 페이지, 서버 정보, 방 생성 API.

방 생성은 관리자 키가 있어야 한다. 서버를 연 사람(주최자)만 방을 만들 수 있고,
같은 네트워크의 낯선 사람이 방을 마구 만들어 디스크/메모리를 소모하는 것을 막는다.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import config, heroes, security, views
from .app_config import AppConfig
from .engine import AuctionError
from .images import ImageError
from .manager import RoomManager
from .netutil import client_ip
from .validators import (
    ValidationError, clean_int, is_valid_image_id, is_valid_room_id, is_valid_token, parse_room_title,
)


def register_routes(app: Flask, manager: RoomManager, cfg: AppConfig, admin_failures: security.FailureLimiter) -> None:
    def _json_error(message: str, status: int, code: str = "error"):
        resp = jsonify({"ok": False, "code": code, "message": message})
        resp.status_code = status
        return resp

    image_failures = security.FailureLimiter(limit=30, window=60, lockout=120)
    image_reads = security.RateLimiter(capacity=60, refill_per_sec=20)
    image_uploads = security.RateLimiter(capacity=8, refill_per_sec=0.5)

    def _same_origin() -> bool:
        origin = request.headers.get("Origin")
        return not origin or urlparse(origin).netloc == request.host

    def _bearer(room_id: str):
        """Authorization: Bearer <토큰> 으로 역할을 판별한다. 반환: (인증정보, 방 핸들) 또는 (None, 오류응답).

        토큰은 URL 이 아니라 헤더로 보낸다 (서버/프록시 로그와 브라우저 기록에 남지 않게).
        실패 사유(방 없음/토큰 틀림/권한 없음)는 구분해서 알려주지 않는다.
        """
        ip = client_ip(request.environ, cfg.trust_proxy)
        if image_failures.blocked(ip):
            return None, _json_error("잠시 후 다시 시도해 주세요.", 429, "blocked")
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        auth = None
        if is_valid_room_id(room_id) and is_valid_token(token):
            auth = manager.authenticate(room_id, token)
        if auth is None:
            image_failures.record_failure(ip)
            return None, _json_error("권한이 없습니다.", 403, "forbidden")
        image_failures.record_success(ip)
        return auth, manager.get(room_id)

    @app.post("/api/rooms/<room_id>/images")
    def upload_image(room_id: str):
        # 이 라우트만 업로드 크기 상한을 올린다 (전역 상한은 64KB). 본문을 읽기 전에 지정해야 한다.
        request.max_content_length = config.MAX_UPLOAD_BYTES
        if not _same_origin():
            return _json_error("허용되지 않은 요청입니다.", 403, "origin")
        auth, handle = _bearer(room_id)
        if auth is None:
            return handle
        if auth.role != "host":
            return _json_error("권한이 없습니다.", 403, "forbidden")
        if not image_uploads.allow(room_id):
            return _json_error("업로드가 너무 빠릅니다. 잠시 후 다시 시도해 주세요.", 429, "rate_limited")
        raw = request.get_data(cache=False)
        try:
            image_id = manager.images.save_upload(raw)
        except ImageError as e:
            return _json_error(e.message, 413 if e.code == "too_large" else 400, e.code)
        return jsonify({"ok": True, "id": image_id}), 201

    @app.get("/api/rooms/<room_id>/images/<image_id>")
    def get_image(room_id: str, image_id: str):
        auth, handle = _bearer(room_id)
        if auth is None:
            return handle
        ip = client_ip(request.environ, cfg.trust_proxy)
        if not image_reads.allow(ip):
            return _json_error("요청이 너무 빠릅니다.", 429, "rate_limited")
        path = manager.images.path_for(image_id) if is_valid_image_id(image_id) else None
        allowed = False
        if path is not None:
            if auth.role == "host":
                allowed = True          # 주최자: 방금 올려 아직 저장 전인 이미지와 명부의 이미지도 미리 볼 수 있다
            else:
                with handle.lock:
                    allowed = image_id in views.visible_image_ids(handle.room, auth.role)
        if not allowed or path is None:
            return _json_error("찾을 수 없습니다.", 404, "not_found")
        resp = send_file(path, mimetype="image/webp", conditional=False, max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Disposition"] = "inline"
        return resp

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.get("/r/<room_id>")
    def room_page(room_id: str):
        # 방이 실제로 있는지는 알려주지 않는다 (존재 여부 탐색 방지). 형식만 검사한다.
        if not is_valid_room_id(room_id):
            abort(404)
        return render_template("room.html")

    @app.get("/api/server-info")
    def server_info():
        urls = [{"label": "이 주소로 접속 중", "url": request.host_url.rstrip("/")}]
        if cfg.public_url:
            urls.insert(0, {"label": "공유용 주소(설정됨)", "url": cfg.public_url.rstrip("/")})
        scheme = "https" if cfg.secure else "http"
        for ip in cfg.lan_ips:
            urls.append({"label": "같은 네트워크(LAN)", "url": f"{scheme}://{ip}:{cfg.port}"})
        return jsonify({
            "ok": True,
            "urls": urls,
            "lan_enabled": cfg.lan_enabled,
            "secure": cfg.secure,
            "heroes": heroes.hero_list_for_client(),
            "player_limits": {
                "max_heroes": config.MAX_HEROES, "max_images": config.MAX_IMAGES_PER_PLAYER,
                "max_upload_bytes": config.MAX_UPLOAD_BYTES,
            },
            "limits": {
                "min_teams": config.MIN_TEAMS, "max_teams": config.MAX_TEAMS,
                "min_members": config.MIN_TEAM_MEMBERS, "max_members": config.MAX_TEAM_MEMBERS,
                "title_len": config.TITLE_LEN,
            },
        })

    @app.post("/api/rooms")
    def create_room():
        ip = client_ip(request.environ, cfg.trust_proxy)
        if admin_failures.blocked(ip):
            return _json_error("잠시 후 다시 시도해 주세요.", 429, "blocked")

        # CSRF/DNS-rebinding 완화: 브라우저가 Origin 을 보냈다면 이 서버 주소와 같아야 한다.
        origin = request.headers.get("Origin")
        if origin:
            if urlparse(origin).netloc != request.host:
                return _json_error("허용되지 않은 요청입니다.", 403, "origin")

        data = request.get_json(silent=True) if request.is_json else None
        if not isinstance(data, dict):
            return _json_error("요청 형식이 올바르지 않습니다.", 400, "bad_request")

        key = data.get("admin_key")
        if not isinstance(key, str) or not security.safe_equal(key, cfg.admin_key):
            admin_failures.record_failure(ip)
            return _json_error("관리자 키가 올바르지 않습니다.", 403, "admin_key")
        admin_failures.record_success(ip)

        try:
            title = parse_room_title(data.get("title"))
            n = clean_int(data.get("num_teams"), config.MIN_TEAMS, config.MAX_TEAMS, field="팀 수")
            m = clean_int(data.get("members_per_team"), config.MIN_TEAM_MEMBERS,
                          config.MAX_TEAM_MEMBERS, field="팀당 인원")
            created = manager.create_room(title, n, m)
        except ValidationError as e:
            return _json_error(e.message, 400, e.code)
        except AuctionError as e:
            return _json_error(e.message, 409, e.code)

        body: dict[str, Any] = {
            "ok": True,
            "room_id": created.handle.room.id,
            "tokens": {
                "host": created.host_token,
                "observer": created.observer_token,
                "leaders": created.leader_tokens,
            },
        }
        resp = jsonify(body)
        resp.status_code = 201
        return resp

    # --- 오류 응답: 내부 정보(스택 트레이스 등)를 노출하지 않는다 ------------------
    @app.errorhandler(404)
    def _404(_e):
        return _json_error("찾을 수 없습니다.", 404, "not_found")

    @app.errorhandler(405)
    def _405(_e):
        return _json_error("허용되지 않은 요청입니다.", 405, "method")

    @app.errorhandler(413)
    def _413(_e):
        return _json_error("요청이 너무 큽니다.", 413, "too_large")

    @app.errorhandler(500)
    def _500(_e):
        return _json_error("서버 오류가 발생했습니다.", 500, "server_error")
