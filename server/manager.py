"""방 레지스트리와 동시성 제어.

- 방마다 RLock 을 하나 둔다. 한 방의 모든 변경(입찰, 타이머 만료, 주최자 조작)은 이 락 안에서
  순서대로 처리되므로 "동시에 도착한 입찰"도 도착 순서대로 한 건씩 판정된다.
- 변경 작업은 run() 으로 감싼다: (1) 만료된 매물 먼저 정리 -> (2) 작업 수행 -> (3) 변경이 있으면 저장.
  이 순서 덕분에 마감 직후 도착한 입찰은 항상 "시간 종료"로 거부된다.
- 소켓/HTTP 와 무관하다. 브로드캐스트는 호출한 쪽(socket_events)이 결과를 보고 수행한다.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config, engine, security
from .engine import AuctionError
from .models import AuctionState, Room
from .images import ImageStore
from .roster import RosterStore
from .storage import RoomStore, WriteBehind

log = logging.getLogger("auction.manager")


@dataclass
class RoomHandle:
    room: Room
    lock: threading.RLock = field(default_factory=threading.RLock)
    online: dict[str, tuple[str, Optional[int]]] = field(default_factory=dict)   # sid -> (role, team_id)
    last_heartbeat: float = 0.0


@dataclass
class Auth:
    role: str                 # "host" | "leader" | "observer"
    team_id: Optional[int] = None


@dataclass
class CreatedRoom:
    handle: RoomHandle
    host_token: str
    observer_token: str
    leader_tokens: list[str]


class RoomManager:
    def __init__(
        self,
        store: RoomStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        retention_days: float = config.DEFAULT_RETENTION_DAYS,
        max_rooms: int = config.MAX_ROOMS,
        images: Optional[ImageStore] = None,
        roster: Optional[RosterStore] = None,
        async_save: bool = False,
    ):
        self.store = store
        self.images = images or ImageStore(store.root)       # 선수 이미지 (방과 무관하게 영구 보관)
        self.roster = roster or RosterStore(store.root)      # 영구 선수 명부
        self.clock = clock
        self.rng: random.Random = rng or random.SystemRandom()
        self.retention_days = retention_days
        self.max_rooms = max_rooms
        self._rooms: dict[str, RoomHandle] = {}
        self._lock = threading.Lock()
        self._writer = WriteBehind(store) if async_save else None

    # ------------------------------------------------------------------ 로드/생성/삭제
    def load_from_disk(self) -> int:
        count = 0
        for data in self.store.load_all():
            try:
                room = Room.from_dict(data)
            except (KeyError, TypeError, ValueError):
                log.warning("복구할 수 없는 방 데이터를 건너뜁니다.")
                continue
            engine.restore_after_restart(room)
            self._rooms[room.id] = RoomHandle(room=room)
            count += 1
        return count

    def create_room(self, title: str, num_teams: int, members_per_team: int) -> CreatedRoom:
        with self._lock:
            if len(self._rooms) >= self.max_rooms:
                raise AuctionError("too_many_rooms", "생성할 수 있는 방 수 한도에 도달했습니다. 오래된 방을 삭제해 주세요.")
            room_id = security.new_room_id()
            while room_id in self._rooms:
                room_id = security.new_room_id()
            host_token = security.new_token()
            observer_token = security.new_token()
            leader_tokens = [security.new_token() for _ in range(num_teams)]
            room = Room(
                id=room_id, title=title, num_teams=num_teams, members_per_team=members_per_team,
                host_hash=security.hash_token(host_token),
                observer_hash=security.hash_token(observer_token),
            )
            engine.init_teams(room, [security.hash_token(t) for t in leader_tokens])
            handle = RoomHandle(room=room)
            self._rooms[room_id] = handle
        with handle.lock:
            self.save(handle)
        log.info("방 생성: %s (팀 %d, 팀당 %d)", room_id, num_teams, members_per_team)
        return CreatedRoom(handle, host_token, observer_token, leader_tokens)

    def get(self, room_id: str) -> Optional[RoomHandle]:
        return self._rooms.get(room_id)

    def all_handles(self) -> list[RoomHandle]:
        with self._lock:
            return list(self._rooms.values())

    def delete_room(self, room_id: str) -> None:
        with self._lock:
            self._rooms.pop(room_id, None)
        if self._writer is not None:
            self._writer.discard(room_id)
        self.store.delete(room_id)
        log.info("방 삭제: %s", room_id)
        # 방 데이터만 지운다. 선수 명부와 거기에 연결된 이미지는 영구 보관이므로 남는다.
        self.gc_images()

    def gc_images(self, grace_sec: float = config.IMAGE_ORPHAN_GRACE_SEC,
                  only: Optional[set[str]] = None) -> int:
        """어느 방의 선수에게도, 영구 명부에도 연결되지 않은 이미지를 지운다.

        방금 올려서 아직 저장 버튼을 누르지 않은 이미지를 지우지 않도록 grace_sec 동안은 둔다.
        """
        referenced = set(self.roster.image_ids())
        for handle in self.all_handles():
            with handle.lock:
                for p in handle.room.players:
                    referenced.update(p.images)
        removed = 0
        for image_id, age in self.images.ages().items():
            if only is not None and image_id not in only:
                continue
            if image_id not in referenced and age >= grace_sec and self.images.delete(image_id):
                removed += 1
        if removed:
            log.info("연결되지 않은 이미지 %d개를 정리했습니다.", removed)
        return removed

    def purge_expired(self) -> list[str]:
        removed = self.store.purge_older_than(self.retention_days)
        with self._lock:
            for rid in removed:
                self._rooms.pop(rid, None)
        for rid in removed:
            log.info("보존 기간 만료로 방 삭제: %s", rid)
        if removed:
            self.gc_images()
        return removed

    # ------------------------------------------------------------------ 인증
    def authenticate(self, room_id: str, token: str) -> Optional[Auth]:
        """토큰으로 역할을 판별한다. 실패 시 None (실패 이유는 구분해서 알려주지 않는다)."""
        handle = self.get(room_id)
        if handle is None:
            # 방이 없는 경우에도 해시 연산을 한 번 수행해 응답 시간 차이를 줄인다.
            security.verify_token(token, "0" * 64)
            return None
        room = handle.room
        found: Optional[Auth] = None
        # 일부러 모두 비교한다 (조기 종료 시간차 최소화)
        if security.verify_token(token, room.host_hash):
            found = Auth("host")
        if security.verify_token(token, room.observer_hash) and found is None:
            found = Auth("observer")
        for team in room.teams:
            if security.verify_token(token, team.token_hash) and found is None:
                found = Auth("leader", team.id)
        return found

    def reissue_token(self, handle: RoomHandle, target: str, team_id: Optional[int]) -> str:
        """팀장/관전자 링크 재발급. 기존 토큰은 즉시 무효가 된다. (handle.lock 보유 상태에서 호출)"""
        token = security.new_token()
        digest = security.hash_token(token)
        if target == "observer":
            handle.room.observer_hash = digest
        elif target == "leader" and team_id is not None and 0 <= team_id < len(handle.room.teams):
            handle.room.teams[team_id].token_hash = digest
        else:
            raise AuctionError("bad_target", "재발급 대상이 올바르지 않습니다.")
        engine._touch(handle.room)
        self.save(handle)
        return token

    # ------------------------------------------------------------------ 접속 현황
    def add_online(self, handle: RoomHandle, sid: str, auth: Auth) -> None:
        with handle.lock:
            handle.online[sid] = (auth.role, auth.team_id)

    def remove_online(self, handle: RoomHandle, sid: str) -> None:
        with handle.lock:
            handle.online.pop(sid, None)

    def presence(self, handle: RoomHandle) -> tuple[list[int], bool]:
        with handle.lock:
            teams = sorted({tid for role, tid in handle.online.values() if role == "leader" and tid is not None})
            host = any(role == "host" for role, _ in handle.online.values())
            return teams, host

    def sids_for(self, handle: RoomHandle, role: str, team_id: Optional[int] = None) -> list[str]:
        with handle.lock:
            return [
                sid for sid, (r, t) in handle.online.items()
                if r == role and (team_id is None or t == team_id)
            ]

    # ------------------------------------------------------------------ 변경 실행
    def run(
        self,
        handle: RoomHandle,
        fn: Callable[[Room, float, random.Random], Any],
    ) -> tuple[bool, Optional[AuctionError], Any]:
        """(만료 정리 -> fn 실행 -> 저장). 반환: (상태가 바뀌었는가, 규칙 위반 오류, fn 결과)."""
        with handle.lock:
            room = handle.room
            before = room.rev
            now = self.clock()
            engine.tick(room, now, self.rng)
            error: Optional[AuctionError] = None
            result = None
            try:
                result = fn(room, now, self.rng)
            except AuctionError as e:
                error = e
            changed = room.rev != before
            if changed:
                self.save(handle)
            return changed, error, result

    def tick(self, handle: RoomHandle) -> bool:
        """주기 호출: 타이머 만료를 처리한다. 바뀌었으면 True."""
        with handle.lock:
            room = handle.room
            before = room.rev
            engine.tick(room, self.clock(), self.rng)
            changed = room.rev != before
            if changed:
                self.save(handle)
            return changed

    def save(self, handle: RoomHandle) -> None:
        """(handle.lock 보유 상태에서 호출)"""
        room = handle.room
        engine.sync_remaining(room, self.clock())
        try:
            if self._writer is not None:
                # 락 안에서는 직렬화만 하고, 디스크 쓰기(fsync)는 별도 스레드가 묶어서 처리한다.
                self._writer.submit(room.id, json.dumps(room.to_dict(), ensure_ascii=False, separators=(",", ":")))
            else:
                self.store.save(room.id, room.to_dict())
        except OSError:
            log.exception("방 저장 실패: %s", room.id)

    def flush(self) -> None:
        """대기 중인 저장을 모두 디스크에 쓴다 (종료 직전에 호출)."""
        if self._writer is not None:
            self._writer.flush()

    def add_chat(self, handle: RoomHandle, entry: dict[str, Any]) -> None:
        with handle.lock:
            handle.room.chat.append(entry)
            if len(handle.room.chat) > config.CHAT_HISTORY:
                del handle.room.chat[: len(handle.room.chat) - config.CHAT_HISTORY]
            self.save(handle)

    def running_handles(self) -> list[RoomHandle]:
        return [h for h in self.all_handles() if engine.is_running(h.room)]
