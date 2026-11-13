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
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import request
from flask_socketio import SocketIO, join_room

from . import config, engine, security, statediff, views
from .app_config import AppConfig
from .engine import AuctionError
from .manager import RoomHandle, RoomManager
from .models import AuctionState
from .netutil import client_ip
from .validators import (
    ValidationError, clean_int, clean_text, is_valid_room_id, is_valid_token, is_valid_uid,
    parse_player_entries, parse_settings_patch, parse_team_entries,
)

log = logging.getLogger("auction.socket")

AUTH_FAIL_MESSAGE = "접속 정보가 올바르지 않거나 만료되었습니다."
JOIN_TIMEOUT_SEC = 15
PRESENCE_DELAY_SEC = 0.2
MAX_CONNECTIONS = 300


@dataclass
class Session:
    handle: RoomHandle
    role: str
    team_id: Optional[int]
    nickname: str


# 패치가 전체 상태의 이 비율보다 크면 그냥 전체를 보낸다 (경매 시작처럼 거의 다 바뀌는 경우).
PATCH_MAX_RATIO = 0.8


class _Sent:
    """한 대상(공용/주최자)에게 마지막으로 보낸 화면 데이터와 그 순번."""
    __slots__ = ("seq", "view")

    def __init__(self) -> None:
        self.seq = 0
        self.view: Optional[dict[str, Any]] = None      # remaining_ms 를 뺀 화면 데이터


class _RoomBroadcast:
    """방 하나의 전송 상태. lock 을 잡은 채로 계산·전송해서 순번과 실제 전송 순서를 일치시킨다."""
    __slots__ = ("lock", "sent")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent = {"pub": _Sent(), "host": _Sent()}


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
        self._pending_presence: set[str] = set()
        self._pending_lock = threading.Lock()
        self._broadcasts: dict[str, _RoomBroadcast] = {}
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
    def _broadcast(self, room_id: str) -> _RoomBroadcast:
        with self._pending_lock:
            bc = self._broadcasts.get(room_id)
            if bc is None:
                bc = self._broadcasts[room_id] = _RoomBroadcast()
            return bc

    def forget_room(self, room_id: str) -> None:
        with self._pending_lock:
            self._broadcasts.pop(room_id, None)

    def emit_state(self, handle: RoomHandle, want: Optional[str] = None) -> Optional[dict[str, Any]]:
        """현재 상태를 역할별 방에 전송한다. 마지막으로 보낸 것과 달라진 부분(패치)만 보낸다.

        - 대상(공용/주최자)마다 순번(seq)을 매긴다. 패치는 "base 번 상태를 seq 번으로 바꾸는 변경분"이고,
          클라이언트는 자기가 가진 순번이 base 와 다르면 전체 상태를 다시 요청한다.
        - 바뀐 것이 없으면 아무것도 보내지 않는다 (예: 관전자 입장).
        - want 가 "pub"/"host" 이면 그 대상의 현재 전체 상태(순번 포함)를 돌려준다. 접속·재동기화 응답에 쓴다.
          이렇게 돌려준 상태는 항상 "seq 번으로 방송된 내용"과 같으므로 이후 패치를 그대로 이어 적용할 수 있다.

        handle.lock 을 잡은 채로 호출하면 안 된다 (잠금 순서: 전송 lock -> handle.lock).
        """
        room_id = handle.room.id
        bc = self._broadcast(room_id)
        result: Optional[dict[str, Any]] = None
        with bc.lock:
            online, host_online = self.manager.presence(handle)
            with handle.lock:
                now = self.manager.clock()
                fresh = {
                    "pub": views.public_view(handle.room, now, online, host_online),
                    "host": views.host_view(handle.room, now, online, host_online),
                }
            for audience, target in (("pub", pub_room(room_id)), ("host", host_room(room_id))):
                view = fresh[audience]
                remaining_ms = view.pop("remaining_ms")     # 시간에 따라 변하는 값은 비교에서 빼고 항상 따로 보낸다
                sent = bc.sent[audience]
                if sent.view is None:
                    # 아직 아무에게도 준 적이 없다 → 받을 사람도 없다. 기준만 잡는다.
                    sent.seq, sent.view = 1, view
                else:
                    try:
                        patch = statediff.diff(sent.view, view)
                        too_big = (patch is not statediff.SAME and
                                   statediff.encoded_size(patch) > PATCH_MAX_RATIO * statediff.encoded_size(view))
                    except Exception:               # 변경분 계산이 실패해도 방송은 반드시 나가야 한다
                        log.exception("변경분 계산 오류 - 전체 상태를 보냅니다")
                        patch, too_big = None, True
                    if patch is not statediff.SAME:
                        base = sent.seq
                        sent.seq, sent.view = base + 1, view
                        if too_big:
                            self.sio.emit("state", {**view, "remaining_ms": remaining_ms, "seq": sent.seq}, to=target)
                        else:
                            self.sio.emit("patch", {"base": base, "seq": sent.seq, "remaining_ms": remaining_ms,
                                                    "p": patch}, to=target)
                if audience == want:
                    result = {**sent.view, "remaining_ms": remaining_ms, "seq": sent.seq}
        return result

    def emit_state_soon(self, handle: RoomHandle) -> None:
        """입장/퇴장처럼 급하지 않은 변화는 PRESENCE_DELAY 동안 모아 한 번만 보낸다.

        30명이 동시에 접속하면 입장마다 전원에게 전체 상태를 보내는 대신 한두 번으로 줄어든다.
        (입찰/낙찰 같은 경매 이벤트는 emit_state 로 즉시 보낸다.)
        """
        room_id = handle.room.id
        with self._pending_lock:
            if room_id in self._pending_presence:
                return
            self._pending_presence.add(room_id)

        def run():
            self.sio.sleep(PRESENCE_DELAY_SEC)
            with self._pending_lock:
                self._pending_presence.discard(room_id)
            try:
                self.emit_state(handle)
            except Exception:
                log.exception("접속 현황 전송 오류")

        self.sio.start_background_task(run)

    def emit_timer(self, handle: RoomHandle) -> None:
        # 타이머의 rev 가 클라이언트 상태보다 앞서면 불필요한 재동기화가 일어난다.
        # 아직 방송되지 않은 변화가 있으면 먼저 내보낸다 (없으면 아무것도 보내지 않는다).
        self.emit_state(handle)
        with handle.lock:
            payload = views.timer_payload(handle.room, self.manager.clock())
        room_id = handle.room.id
        self.sio.emit("timer", payload, to=pub_room(room_id))
        self.sio.emit("timer", payload, to=host_room(room_id))

    def snapshot_for(self, sess: Session) -> dict[str, Any]:
        """이 세션이 받을 전체 상태. (아직 방송되지 않은 변화가 있으면 먼저 방송해 순번을 맞춘다.)"""
        snapshot = self.emit_state(sess.handle, want="host" if sess.role == "host" else "pub")
        assert snapshot is not None
        return snapshot

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
                self.emit_state_soon(sess.handle)

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
        self._on("host_roster_list", ("host",), "host", self._host_roster_list)
        self._on("host_roster_delete", ("host",), "host", self._host_roster_delete)
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
        self.emit_state_soon(handle)   # 다른 참가자들에게 접속 현황 갱신 (모아서 전송)
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
            lambda room, now, rng: engine.place_bid(room, team_id, data.get("amount"), data.get("lot_seq"), now, rng),
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
        if teams is not None:
            for i, t in enumerate(teams, start=1):
                if t["leader_image"] and not self.manager.images.exists(t["leader_image"]):
                    raise ValidationError(f"{i}번 팀장 아이콘: 업로드되지 않았거나 삭제된 이미지입니다. 다시 올려 주세요.")
        if players is not None:
            for i, p in enumerate(players, start=1):
                for image_id in p["images"]:
                    if not self.manager.images.exists(image_id):
                        raise ValidationError(f"{i}번 매물: 업로드되지 않았거나 삭제된 이미지가 있습니다. 다시 올려 주세요.")
                if not p["uid"]:
                    p["uid"] = secrets.token_hex(8)       # 영구 명부의 키 (새 선수에게 부여)

        def apply(room, now, rng):
            if room.state != AuctionState.WAITING:
                raise AuctionError("bad_state", "경매 시작 후에는 설정을 바꿀 수 없습니다.")
            if members is not None:
                engine.apply_members_per_team(room, members)
            if parsed_settings is not None:
                engine.apply_settings(room, parsed_settings)
            if teams is not None:
                previously_saved.update(t.leader_image for t in room.teams if t.leader_image)
                engine.apply_teams(room, teams)
            if players is not None:
                # 이번 저장으로 "빠지는" 이미지를 기억해 둔다 (이전에 저장돼 있었는데 이번에는 없는 것).
                for p in room.players:
                    previously_saved.update(p.images)
                for p in players:
                    old = self.manager.roster.get(p["uid"]) if p["uid"] else None
                    if old:
                        previously_saved.update(old.get("images", []))
                # 영구 명부를 먼저 저장한다 (명부가 가득 차면 방을 건드리기 전에 실패한다).
                self.manager.roster.upsert_many(players)
                engine.apply_players(room, players)

        previously_saved: set[str] = set()
        result = self._run(sess, apply)
        if result.get("ok") and (players is not None or teams is not None):
            # 주최자가 제거한 이미지는 어디에서도 쓰이지 않으면 바로 삭제한다 (개인정보 최소 보관).
            # gc_images 는 방·명부에 아직 연결된 이미지는 건드리지 않으므로 후보만 넘기면 된다.
            self.manager.gc_images(grace_sec=0, only=previously_saved)
            self.manager.gc_images()        # 그 밖의 연결 안 된 이미지는 유예 시간 후 정리
        return result

    def _host_roster_list(self, _sess: Session, _data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "players": self.manager.roster.list()}

    def _host_roster_delete(self, _sess: Session, data: dict[str, Any]) -> dict[str, Any]:
        uid = data.get("uid")
        if not is_valid_uid(uid):
            return _fail("bad_request", "대상이 올바르지 않습니다.")
        entry = self.manager.roster.get(uid)
        if entry is None:
            return _fail("not_found", "명부에 없는 선수입니다.")
        self.manager.roster.delete(uid)
        # 개인정보 최소 보관: 다른 곳에서 쓰지 않는 이 선수의 이미지는 즉시 삭제한다.
        self.manager.gc_images(grace_sec=0, only=set(entry.get("images", [])))
        return {"ok": True, "players": self.manager.roster.list()}

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
        self.forget_room(room_id)

        def later():
            self.sio.sleep(0.5)
            self.kick(sids)

        self.sio.start_background_task(later)
        return {"ok": True}


def start_ticker(sio: SocketIO, manager: RoomManager, api: SocketApi) -> None:
    """백그라운드 루프: 타이머 만료 처리, 타이머 재동기화(HEARTBEAT_SEC), 오래된 방 정리."""

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
                    for room_id in manager.purge_expired():
                        api.forget_room(room_id)
                except Exception:
                    log.exception("만료 방 정리 오류")

    sio.start_background_task(loop)
