"""Socket.IO 실시간 이벤트.

보안 원칙
1. 소켓은 연결만으로는 아무 권한이 없다. 'join' 으로 토큰을 검증해 역할(host/leader/observer)을
   부여받은 뒤에만 다른 이벤트가 동작한다. 15초 안에 join 하지 않으면 연결을 끊는다.
2. 팀장이 어느 팀인지는 서버가 토큰으로 정한다. 클라이언트가 보내는 team_id 는 절대 신뢰하지 않는다.
3. 모든 이벤트는 역할 검사 -> 레이트리밋 -> 입력 검증 순으로 처리하고, 오류는 일반화된 메시지만 돌려준다.
4. 화면 데이터는 views.py 가 역할별로 걸러서 만든다. 주최자 방과 공용 방을 분리해 브로드캐스트한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import request
from flask_socketio import SocketIO, join_room

from . import config, engine, security, views
from .app_config import AppConfig
from .engine import AuctionError
from .manager import RoomHandle, RoomManager
from .models import AuctionState
from .netutil import client_ip
from .validators import (
    ValidationError, clean_int, clean_text, is_valid_room_id, is_valid_token,
    parse_player_entries, parse_settings_patch, parse_team_entries,
)

log = logging.getLogger("auction.socket")

AUTH_FAIL_MESSAGE = "접속 정보가 올바르지 않거나 만료되었습니다."
JOIN_TIMEOUT_SEC = 15
MAX_CONNECTIONS = 300


@dataclass
class Session:
    handle: RoomHandle
    role: str
    team_id: Optional[int]
    nickname: str


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def pub_room(room_id: str) -> str:
    return f"pub:{room_id}"


def host_room(room_id: str) -> str:
    return f"host:{room_id}"


class SocketApi:
    def __init__(self, socketio: SocketIO, manager: RoomManager, cfg: AppConfig):
        self.sio = socketio
        self.manager = manager
        self.cfg = cfg
        self.sessions: dict[str, Session] = {}
        self.connected: set[str] = set()
        self.join_failures = security.FailureLimiter(limit=20, window=60, lockout=120)
        self.buckets = {
            "join": security.RateLimiter(capacity=5, refill_per_sec=0.5),
            "bid": security.RateLimiter(capacity=6, refill_per_sec=3),
            "chat": security.RateLimiter(capacity=3, refill_per_sec=0.5),
            "host": security.RateLimiter(capacity=30, refill_per_sec=10),
            "default": security.RateLimiter(capacity=20, refill_per_sec=10),
        }
        self._register()

    # ------------------------------------------------------------------ 브로드캐스트
    def emit_state(self, handle: RoomHandle) -> None:
        """현재 상태를 역할별 방에 전송한다. 직렬화는 락 안에서, 전송은 락 밖에서 한다."""
        room_id = handle.room.id
        online, host_online = self.manager.presence(handle)
        with handle.lock:
            now = self.manager.clock()
            pub = views.public_view(handle.room, now, online, host_online)
            host = views.host_view(handle.room, now, online, host_online)
        self.sio.emit("state", pub, to=pub_room(room_id))
        self.sio.emit("state", host, to=host_room(room_id))

    def emit_timer(self, handle: RoomHandle) -> None:
        with handle.lock:
            payload = views.timer_payload(handle.room, self.manager.clock())
        room_id = handle.room.id
        self.sio.emit("timer", payload, to=pub_room(room_id))
        self.sio.emit("timer", payload, to=host_room(room_id))

    def snapshot_for(self, sess: Session) -> dict[str, Any]:
        online, host_online = self.manager.presence(sess.handle)
        with sess.handle.lock:
            now = self.manager.clock()
            if sess.role == "host":
                return views.host_view(sess.handle.room, now, online, host_online)
            return views.public_view(sess.handle.room, now, online, host_online)

    def kick(self, sids: list[str]) -> None:
        for sid in sids:
            self.sessions.pop(sid, None)
            try:
                self.sio.server.disconnect(sid)
            except Exception:       # 이미 끊긴 경우 등
                pass

    # ------------------------------------------------------------------ 등록
    def _register(self) -> None:
        sio = self.sio

        @sio.on("connect")
        def on_connect(auth=None):
            sid = request.sid
            if len(self.connected) >= MAX_CONNECTIONS:
                return False
            self.connected.add(sid)

            def reaper():
                sio.sleep(JOIN_TIMEOUT_SEC)
                if sid in self.connected and sid not in self.sessions:
                    try:
                        sio.server.disconnect(sid)
                    except Exception:
                        pass

            sio.start_background_task(reaper)

        @sio.on("disconnect")
        def on_disconnect(*_args):
            sid = request.sid
            self.connected.discard(sid)
            sess = self.sessions.pop(sid, None)
            for bucket in self.buckets.values():
                bucket.forget(sid)
            if sess is not None:
                self.manager.remove_online(sess.handle, sid)
                self.emit_state(sess.handle)

        self._on("join", None, "join", self._join)
        self._on("sync", ("host", "leader", "observer"), "default", self._sync)
        self._on("bid", ("leader",), "bid", self._bid)
        self._on("pick_opponent", ("leader",), "default", self._pick_opponent)
        self._on("chat", ("host", "leader", "observer"), "chat", self._chat)

        self._on("host_setup", ("host",), "host", self._host_setup)
        self._on("host_start", ("host",), "host", self._host_start)
        self._on("host_pause", ("host",), "host", self._host_pause)
        self._on("host_resume", ("host",), "host", self._host_resume)
        self._on("host_force_sold", ("host",), "host", self._host_force_sold)
        self._on("host_force_unsold", ("host",), "host", self._host_force_unsold)
        self._on("host_pick", ("host",), "host", self._host_pick)
        self._on("host_reissue", ("host",), "host", self._host_reissue)
        self._on("host_delete_room", ("host",), "host", self._host_delete_room)

    def _on(self, event: str, roles: Optional[tuple[str, ...]], bucket: str,
            fn: Callable[[Optional[Session], dict[str, Any]], dict[str, Any]]) -> None:
        @self.sio.on(event)
        def wrapper(data=None):
            sid = request.sid
            sess = self.sessions.get(sid)
            if roles is not None and (sess is None or sess.role not in roles):
                return _fail("forbidden", "권한이 없습니다.")
            if not self.buckets[bucket].allow(sid):
                return _fail("rate_limited", "요청이 너무 빠릅니다. 잠시 후 다시 시도해 주세요.")
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return _fail("bad_request", "요청 형식이 올바르지 않습니다.")
            try:
                return fn(sess, data)
            except ValidationError as e:
                return _fail(e.code, e.message)
            except AuctionError as e:
                return _fail(e.code, e.message)
            except Exception:
                log.exception("이벤트 처리 중 오류: %s", event)
                return _fail("server_error", "서버 오류가 발생했습니다.")

        wrapper.__name__ = f"on_{event}"

    # ------------------------------------------------------------------ 접속
    def _join(self, _sess: Optional[Session], data: dict[str, Any]) -> dict[str, Any]:
        sid = request.sid
        ip = client_ip(request.environ, self.cfg.trust_proxy)
        if self.join_failures.blocked(ip):
            return _fail("blocked", "잠시 후 다시 시도해 주세요.")

        room_id, token = data.get("room_id"), data.get("token")
        if not is_valid_room_id(room_id) or not is_valid_token(token):
            self.join_failures.record_failure(ip)
            return _fail("auth", AUTH_FAIL_MESSAGE)

        auth = self.manager.authenticate(room_id, token)
        if auth is None:
            self.join_failures.record_failure(ip)
            return _fail("auth", AUTH_FAIL_MESSAGE)
        self.join_failures.record_success(ip)

        handle = self.manager.get(room_id)
        assert handle is not None

        # 재접속(같은 연결에서 다시 join)이면 이전 세션을 정리한다.
        old = self.sessions.pop(sid, None)
        if old is not None:
            self.manager.remove_online(old.handle, sid)

        if auth.role == "host":
            nickname = "주최자"
            join_room(host_room(room_id))
        else:
            if auth.role == "leader":
                with handle.lock:
                    nickname = handle.room.teams[auth.team_id].leader_name  # type: ignore[index]
            else:
                try:
                    nickname = clean_text(data.get("nickname"), config.NICK_LEN, field="닉네임") or "관전자"
                except ValidationError:
                    nickname = "관전자"
                # 채팅에서 주최자/팀장을 사칭하지 못하도록 예약된 이름에는 표시를 붙인다.
                with handle.lock:
                    reserved = {t.leader_name.casefold() for t in handle.room.teams} | {"주최자"}
                if nickname.casefold() in reserved:
                    nickname = f"{nickname} (관전)"
            join_room(pub_room(room_id))

        sess = Session(handle=handle, role=auth.role, team_id=auth.team_id, nickname=nickname)
        self.sessions[sid] = sess
        self.manager.add_online(handle, sid, auth)

        snapshot = self.snapshot_for(sess)
        with handle.lock:
            chat = list(handle.room.chat)
        self.emit_state(handle)        # 다른 참가자들에게 접속 현황 갱신
        return {
            "ok": True,
            "role": auth.role,
            "team_id": auth.team_id,
            "nickname": nickname,
            "snapshot": snapshot,
            "chat": chat,
        }

    def _sync(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "snapshot": self.snapshot_for(sess)}

    # ------------------------------------------------------------------ 경매 참여
    def _run(self, sess: Session, fn) -> dict[str, Any]:
        changed, error, _result = self.manager.run(sess.handle, fn)
        if changed:
            self.emit_state(sess.handle)
        if error is not None:
            return _fail(error.code, error.message)
        return {"ok": True}

    def _bid(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        team_id = sess.team_id          # 서버가 토큰으로 정한 값만 사용
        assert team_id is not None
        return self._run(
            sess,
            lambda room, now, rng: engine.place_bid(room, team_id, data.get("amount"), data.get("lot_seq"), now),
        )

    def _pick_opponent(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        team_id = sess.team_id
        assert team_id is not None
        return self._run(sess, lambda room, now, rng: engine.pick_opponent(room, team_id, data.get("opponent")))

    def _chat(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        text = clean_text(data.get("text"), config.CHAT_LEN, field="채팅", required=True)
        name = sess.nickname
        if sess.role == "leader":
            with sess.handle.lock:
                name = sess.handle.room.teams[sess.team_id].leader_name   # type: ignore[index]
        entry = {"name": name, "role": sess.role, "team": sess.team_id, "text": text, "ts": int(time.time())}
        self.manager.add_chat(sess.handle, entry)
        room_id = sess.handle.room.id
        self.sio.emit("chat", entry, to=pub_room(room_id))
        self.sio.emit("chat", entry, to=host_room(room_id))
        return {"ok": True}

    # ------------------------------------------------------------------ 주최자 전용
    _SETUP_KEYS = {"settings", "members_per_team", "teams", "players"}

    def _host_setup(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        unknown = set(data) - self._SETUP_KEYS
        if unknown:
            return _fail("bad_request", "알 수 없는 항목이 포함되어 있습니다.")
        room = sess.handle.room
        # 1) 전부 검증한 뒤  2) 한 번에 반영한다 (중간 실패로 일부만 바뀌는 상황 방지)
        parsed_settings = parse_settings_patch(data["settings"]) if "settings" in data else None
        members = (clean_int(data["members_per_team"], config.MIN_TEAM_MEMBERS, config.MAX_TEAM_MEMBERS,
                             field="팀당 인원") if "members_per_team" in data else None)
        teams = parse_team_entries(data["teams"], room.num_teams) if "teams" in data else None
        players = parse_player_entries(data["players"]) if "players" in data else None

        def apply(room, now, rng):
            if room.state != AuctionState.WAITING:
                raise AuctionError("bad_state", "경매 시작 후에는 설정을 바꿀 수 없습니다.")
            if members is not None:
                engine.apply_members_per_team(room, members)
            if parsed_settings is not None:
                engine.apply_settings(room, parsed_settings)
            if teams is not None:
                engine.apply_teams(room, teams)
            if players is not None:
                engine.apply_players(room, players)

        return self._run(sess, apply)

    def _host_start(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return self._run(sess, lambda room, now, rng: engine.start_auction(room, now, rng))

    def _host_pause(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return self._run(sess, lambda room, now, rng: engine.pause(room, now))

    def _host_resume(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return self._run(sess, lambda room, now, rng: engine.resume(room, now))

    def _host_force_sold(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return self._run(sess, lambda room, now, rng: engine.force_sold(room, now, rng))

    def _host_force_unsold(self, sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return self._run(sess, lambda room, now, rng: engine.force_unsold(room, now, rng))

    def _host_pick(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        def act(room, now, rng):
            picker = engine.current_picker(room)
            if picker is None:
                raise AuctionError("not_your_turn", "지금은 대진을 지명할 단계가 아닙니다.")
            engine.pick_opponent(room, picker, data.get("opponent"))
        return self._run(sess, act)

    def _host_reissue(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        target = data.get("target")
        if target not in ("observer", "leader"):
            return _fail("bad_target", "재발급 대상이 올바르지 않습니다.")
        team_id = None
        if target == "leader":
            team_id = clean_int(data.get("team_id"), 0, config.MAX_TEAMS - 1, field="팀 번호")
        handle = sess.handle
        with handle.lock:
            if target == "leader" and team_id >= len(handle.room.teams):    # type: ignore[operator]
                return _fail("bad_target", "재발급 대상이 올바르지 않습니다.")
            token = self.manager.reissue_token(handle, target, team_id)
        # 기존 링크로 접속해 있던 사람은 즉시 내보낸다.
        role = "observer" if target == "observer" else "leader"
        self.kick(self.manager.sids_for(handle, role, team_id))
        self.emit_state(handle)
        return {"ok": True, "token": token}

    def _host_delete_room(self, sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("confirm") != "DELETE":
            return _fail("confirm", "확인 문구가 올바르지 않습니다.")
        handle = sess.handle
        room_id = handle.room.id
        with handle.lock:
            sids = list(handle.online.keys())
        self.sio.emit("room_closed", {}, to=pub_room(room_id))
        self.sio.emit("room_closed", {}, to=host_room(room_id))
        self.manager.delete_room(room_id)

        def later():
            self.sio.sleep(0.5)
            self.kick(sids)

        self.sio.start_background_task(later)
        return {"ok": True}


def start_ticker(sio: SocketIO, manager: RoomManager, api: SocketApi) -> None:
    """백그라운드 루프: 타이머 만료 처리, 1초 하트비트, 오래된 방 정리."""

    def loop():
        last_purge = time.monotonic()
        while True:
            sio.sleep(config.TICK_INTERVAL_SEC)
            now = manager.clock()
            for handle in manager.running_handles():
                try:
                    if manager.tick(handle):
                        handle.last_heartbeat = now
                        api.emit_state(handle)
                    elif now - handle.last_heartbeat >= config.HEARTBEAT_SEC:
                        handle.last_heartbeat = now
                        api.emit_timer(handle)
                except Exception:
                    log.exception("타이머 루프 오류")
            if now - last_purge > 3600:
                last_purge = now
                try:
                    manager.purge_expired()
                except Exception:
                    log.exception("만료 방 정리 오류")

    sio.start_background_task(loop)
