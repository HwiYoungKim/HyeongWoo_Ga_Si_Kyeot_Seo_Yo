"""경매 방의 데이터 모델.

의사코드(C 구조체)의 Player / Team / AuctionRoom 을 파이썬 dataclass 로 옮겼다.
- 선수/팀은 리스트 인덱스가 곧 ID 이다 (player_id == players[i].id).
- 큐(main_queue, unsold_queue)는 선수 ID 리스트다. 규모가 최대 90명이라 list 로 충분하다.
- deadline(monotonic 시계)은 런타임 전용이라 저장하지 않는다. 대신 remaining 을 저장하고,
  서버 재시작 시 진행 중이던 경매는 "일시정지" 상태로 복구된다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AuctionState(str, Enum):
    WAITING = "WAITING"                  # 방 생성 후 설정/매물 등록 중
    MAIN_AUCTION = "MAIN_AUCTION"        # 1차 정규 경매
    UNSOLD_AUCTION = "UNSOLD_AUCTION"    # 유찰자 재경매 라운드
    AUTO_ASSIGN = "AUTO_ASSIGN"          # 잔여 매물 자동 배정 (순간 상태)
    BRACKET_SELECT = "BRACKET_SELECT"    # 잔여 포인트 기반 대진 선택
    COMPLETED = "COMPLETED"              # 종료


RUNNING_STATES = (AuctionState.MAIN_AUCTION, AuctionState.UNSOLD_AUCTION)


class BidMode(str, Enum):
    FIXED = "FIXED"      # 고정 금액 단위 (예: +10P 이상)
    RATIO = "RATIO"      # 현재가 대비 비율 (예: +10% 이상)


@dataclass
class Settings:
    base_points: int = 1000
    bid_mode: str = BidMode.FIXED.value
    fixed_min_increment: int = 10
    ratio_min_increment: float = 0.10     # 0.10 = 10%
    min_first_bid: int = 0                # 첫 입찰 최소 금액 (의사코드 기본값은 0P)
    reserve_per_slot: int = 0             # 남은 빈자리 1칸당 남겨둬야 하는 최소 포인트 (0이면 비활성)
    base_bid_time_sec: int = 30
    extension_time_sec: int = 10          # 0이면 순발력 모드 (연장 없음)
    order_public: bool = False            # True=PUBLIC(순서 공개) / False=BLIND

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        base = cls()
        for key in base.__dict__:
            if key in data:
                setattr(base, key, data[key])
        return base


@dataclass
class Player:
    id: int
    name: str
    uid: str = ""                         # 방과 무관한 영구 식별자 (선수 명부의 키)
    battletag: str = ""
    role: str = ""                        # tank | damage | support
    heroes: list[str] = field(default_factory=list)   # 영웅 id (최대 5)
    images: list[str] = field(default_factory=list)   # 이미지 id (0~2)
    elo: Optional[int] = None
    intro: str = ""
    winning_bid: int = 0
    assigned_team_id: int = -1            # -1 이면 미배정

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Player":
        # 예전 버전 파일(필드 없음/폐기된 position)과 알 수 없는 키를 모두 견딘다.
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Team:
    id: int
    leader_name: str
    leader_position: str = ""
    token_hash: str = ""                  # 팀장 접속 토큰의 SHA-256 (원문은 저장하지 않음)
    handicap: int = 0
    initial_points: int = 0
    points: int = 0
    roster: list[int] = field(default_factory=list)   # 영입한 선수 ID

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Team":
        return cls(**data)


@dataclass
class Room:
    id: str
    title: str
    num_teams: int
    members_per_team: int
    host_hash: str
    observer_hash: str
    settings: Settings = field(default_factory=Settings)
    teams: list[Team] = field(default_factory=list)
    players: list[Player] = field(default_factory=list)

    state: AuctionState = AuctionState.WAITING
    paused: bool = False
    main_queue: list[int] = field(default_factory=list)
    unsold_queue: list[int] = field(default_factory=list)
    leftover: list[int] = field(default_factory=list)   # 재경매에서도 유찰된 선수

    current_lot: Optional[int] = None
    lot_seq: int = 0                      # 매물이 바뀔 때마다 +1 (이전 매물에 대한 입찰 혼입 방지)
    highest_bid: int = 0
    highest_team: int = -1                # -1 이면 입찰자 없음
    bid_history: list[dict[str, int]] = field(default_factory=list)
    remaining: float = 0.0                # 남은 시간(초). 진행 중에는 deadline 이 우선한다.
    deadline: Optional[float] = None      # monotonic 기준 만료 시각 (저장하지 않음)

    bracket: dict[str, Any] = field(default_factory=lambda: {"order": [], "pairs": [], "byes": []})
    activity: list[dict[str, Any]] = field(default_factory=list)
    chat: list[dict[str, Any]] = field(default_factory=list)

    rev: int = 0                          # 상태가 바뀔 때마다 +1 (클라이언트 동기화 검사용)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- 직렬화 ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "num_teams": self.num_teams,
            "members_per_team": self.members_per_team,
            "host_hash": self.host_hash,
            "observer_hash": self.observer_hash,
            "settings": self.settings.to_dict(),
            "teams": [t.to_dict() for t in self.teams],
            "players": [p.to_dict() for p in self.players],
            "state": self.state.value,
            "paused": self.paused,
            "main_queue": list(self.main_queue),
            "unsold_queue": list(self.unsold_queue),
            "leftover": list(self.leftover),
            "current_lot": self.current_lot,
            "lot_seq": self.lot_seq,
            "highest_bid": self.highest_bid,
            "highest_team": self.highest_team,
            "bid_history": list(self.bid_history),
            "remaining": self.remaining,
            "bracket": self.bracket,
            "activity": list(self.activity),
            "chat": list(self.chat),
            "rev": self.rev,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Room":
        room = cls(
            id=data["id"],
            title=data["title"],
            num_teams=data["num_teams"],
            members_per_team=data["members_per_team"],
            host_hash=data["host_hash"],
            observer_hash=data["observer_hash"],
            settings=Settings.from_dict(data.get("settings", {})),
            teams=[Team.from_dict(t) for t in data.get("teams", [])],
            players=[Player.from_dict(p) for p in data.get("players", [])],
            state=AuctionState(data.get("state", "WAITING")),
            paused=bool(data.get("paused", False)),
            main_queue=list(data.get("main_queue", [])),
            unsold_queue=list(data.get("unsold_queue", [])),
            leftover=list(data.get("leftover", [])),
            current_lot=data.get("current_lot"),
            lot_seq=int(data.get("lot_seq", 0)),
            highest_bid=int(data.get("highest_bid", 0)),
            highest_team=int(data.get("highest_team", -1)),
            bid_history=list(data.get("bid_history", [])),
            remaining=float(data.get("remaining", 0.0)),
            bracket=data.get("bracket") or {"order": [], "pairs": [], "byes": []},
            activity=list(data.get("activity", [])),
            chat=list(data.get("chat", [])),
            rev=int(data.get("rev", 0)),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )
        return room
