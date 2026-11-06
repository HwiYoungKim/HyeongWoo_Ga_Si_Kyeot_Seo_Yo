"""경매 코어 엔진 (상태 머신).

작업지시서 TB-ARCH-C-2026 의 C 의사코드를 파이썬으로 옮기되, 의사코드를 그대로 옮기면
실제 서비스에서 문제가 되는 부분을 아래와 같이 고쳤다.

[의사코드 대비 수정 사항]
1. 대진 우선순위 산출에 qsort(room->teams) 로 팀 배열 자체를 정렬하면, 이후 teams[team_id]
   인덱싱이 깨진다. -> 팀 배열은 그대로 두고 별도 순서 목록(bracket.order)만 만든다.
2. 유찰 재경매 라운드에서 또 유찰된 선수를 다시 unsold_queue 에 넣으면 영원히 반복된다.
   -> 재경매에서 다시 유찰된 선수는 leftover 로 보내고, 라운드가 끝나면 빈자리에 0P 자동 배정한다.
3. 입찰 직전에 매물이 바뀌면 이전 매물을 겨냥한 입찰이 새 매물에 적용될 수 있다.
   -> 입찰은 lot_seq(매물 일련번호)를 함께 보내야 하고, 다르면 거부한다.
4. 타이머 만료 직후/틱 직전에 도착한 입찰이 마감 후에 통과할 수 있다.
   -> 입찰 처리 전에 항상 tick() 을 먼저 수행하고, 입찰 시점이 deadline 이후면 거부한다.
5. 비율 입찰은 int() 버림이라 "n% 이상" 조건을 만족하지 못할 수 있다. -> 올림(ceil) 사용.
6. 최종 대진: 점수 동률은 무작위 순서로 정한다 (stable sort 로 셔플 순서를 보존).
7. 서버 재시작 시 deadline(monotonic)이 의미를 잃는다. -> remaining 만 저장하고 재시작 시 일시정지로 복구.

이 모듈은 입출력(네트워크/파일)을 전혀 모른다. 시간(now)과 난수(rng)를 인자로 받으므로
테스트에서 결정적으로 재현할 수 있다.
"""
from __future__ import annotations

import math
import secrets
import time
from typing import Any, Protocol

from . import config
from .models import AuctionState, BidMode, Player, Room, Team, RUNNING_STATES


class Rng(Protocol):
    def shuffle(self, x: list) -> None: ...


class AuctionError(Exception):
    """규칙 위반. message 는 사용자에게 그대로 보여줘도 되는 문장이다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# =============================================================================
# 조회용 보조 함수
# =============================================================================

def open_slots(room: Room, team: Team) -> int:
    """팀의 남은 빈자리 수 (팀장 본인 1칸 제외)."""
    return room.members_per_team - 1 - len(team.roster)


def teams_with_open_slots(room: Room) -> list[Team]:
    return [t for t in room.teams if open_slots(room, t) > 0]


def max_bid_for(room: Room, team: Team) -> int:
    """팀이 지금 낼 수 있는 최대 입찰액 (보유 포인트 - 남은 빈자리 유보금)."""
    slots = open_slots(room, team)
    if slots <= 0:
        return 0
    reserve = room.settings.reserve_per_slot * (slots - 1)
    return max(0, team.points - reserve)


def _bid_floor(room: Room) -> int:
    """새 매물에 낼 수 있는 가장 낮은 의미 있는 입찰액 (0P 입찰은 "입찰 불가"로 본다)."""
    return max(1, room.settings.min_first_bid)


def points_exhausted(room: Room) -> bool:
    """빈자리가 남은 팀이 있는데, 그 팀들이 모두 더 이상 입찰할 포인트가 없는가.

    이 상태에서는 경매를 계속해도 낙찰이 나올 수 없으므로 강제로 멈추고 남은 선수를 자동 배정한다.
    """
    open_teams = teams_with_open_slots(room)
    if not open_teams:
        return False
    floor = _bid_floor(room)
    return all(max_bid_for(room, t) < floor for t in open_teams)


def nobody_can_outbid(room: Room) -> bool:
    """현재 최고 입찰을 넘길 수 있는 팀이 하나도 없는가 (최고 입찰자 본인은 제외)."""
    if room.highest_team == -1:
        return False
    need = min_next_bid(room)
    for t in teams_with_open_slots(room):
        if t.id != room.highest_team and max_bid_for(room, t) >= need:
            return False
    return True


def min_next_bid(room: Room) -> int:
    """현재 매물에 대해 다음 입찰이 최소한 넘어야 하는 금액."""
    if room.highest_team == -1:
        return max(1, room.settings.min_first_bid)
    s = room.settings
    if s.bid_mode == BidMode.RATIO.value:
        delta = max(1, math.ceil(room.highest_bid * s.ratio_min_increment))
    else:
        delta = s.fixed_min_increment
    return room.highest_bid + delta


def remaining_sec(room: Room, now: float) -> float:
    if room.state in RUNNING_STATES and not room.paused and room.deadline is not None:
        return max(0.0, room.deadline - now)
    return room.remaining


def is_running(room: Room) -> bool:
    return room.state in RUNNING_STATES


def current_picker(room: Room) -> int | None:
    """대진 선택 단계에서 지금 상대를 지명할 팀 ID."""
    if room.state != AuctionState.BRACKET_SELECT:
        return None
    unpaired = _unpaired(room)
    return unpaired[0] if len(unpaired) >= 3 else None


def sync_remaining(room: Room, now: float) -> None:
    """저장 직전에 호출: 진행 중인 타이머의 남은 시간을 remaining 에 반영한다."""
    if is_running(room) and not room.paused and room.deadline is not None:
        room.remaining = max(0.0, room.deadline - now)


def restore_after_restart(room: Room) -> None:
    """서버 재시작 후 호출: 진행 중이던 경매는 일시정지 상태로 복구한다."""
    room.deadline = None
    if is_running(room) and not room.paused:
        room.paused = True
        _log(room, "paused", reason="restart")


# =============================================================================
# 내부 유틸
# =============================================================================

def _touch(room: Room) -> None:
    room.rev += 1
    room.updated_at = time.time()


def _log(room: Room, type_: str, **fields: Any) -> None:
    entry = {"type": type_, "ts": int(time.time()), **fields}
    room.activity.append(entry)
    if len(room.activity) > config.ACTIVITY_HISTORY:
        del room.activity[: len(room.activity) - config.ACTIVITY_HISTORY]


def _require_state(room: Room, *states: AuctionState) -> None:
    if room.state not in states:
        raise AuctionError("bad_state", "지금은 사용할 수 없는 기능입니다.")


def _require_running(room: Room) -> None:
    if not is_running(room) or room.current_lot is None:
        raise AuctionError("not_running", "진행 중인 경매가 없습니다.")


def _recompute_points(room: Room) -> None:
    for team in room.teams:
        team.initial_points = max(0, room.settings.base_points + team.handicap)
        team.points = team.initial_points


# =============================================================================
# 방 설정 (WAITING 상태에서만)
# =============================================================================

def init_teams(room: Room, token_hashes: list[str]) -> None:
    """방 생성 직후 팀 슬롯을 만든다. 팀장 이름은 임시값."""
    room.teams = [
        Team(id=i, leader_name=f"팀장 {i + 1}", token_hash=token_hashes[i])
        for i in range(room.num_teams)
    ]
    _recompute_points(room)


def apply_settings(room: Room, patch: dict[str, Any]) -> None:
    _require_state(room, AuctionState.WAITING)
    for key, value in patch.items():
        setattr(room.settings, key, value)
    _recompute_points(room)
    _touch(room)


def apply_members_per_team(room: Room, members: int) -> None:
    _require_state(room, AuctionState.WAITING)
    if not (config.MIN_TEAM_MEMBERS <= members <= config.MAX_TEAM_MEMBERS):
        raise AuctionError("bad_value", "팀당 인원 범위가 올바르지 않습니다.")
    room.members_per_team = members
    _touch(room)


def apply_teams(room: Room, entries: list[dict[str, Any]]) -> None:
    _require_state(room, AuctionState.WAITING)
    for team, entry in zip(room.teams, entries):
        team.leader_name = entry["leader_name"]
        team.leader_position = entry["leader_position"]
        team.handicap = entry["handicap"]
    _recompute_points(room)
    _touch(room)


def _entry_roles(e: dict[str, Any]) -> list[str]:
    roles = e.get("roles")
    if roles is None and e.get("role"):               # 예전 형식 호환
        roles = [e["role"]]
    return list(roles or [])


def apply_players(room: Room, entries: list[dict[str, Any]]) -> None:
    _require_state(room, AuctionState.WAITING)
    players = []
    for i, e in enumerate(entries):
        players.append(Player(
            id=i, name=e["name"], uid=e.get("uid") or secrets.token_hex(8),
            battletag=e.get("battletag", ""), roles=_entry_roles(e),
            heroes=list(e.get("heroes", [])), images=list(e.get("images", [])),
            elo=e.get("elo"), intro=e.get("intro", ""),
        ))
    room.players = players
    _touch(room)


def required_player_count(room: Room) -> int:
    return room.num_teams * (room.members_per_team - 1)


# =============================================================================
# 경매 시작 / 다음 매물
# =============================================================================

def start_auction(room: Room, now: float, rng: Rng) -> None:
    _require_state(room, AuctionState.WAITING)
    need = required_player_count(room)
    if len(room.players) != need:
        raise AuctionError(
            "player_count",
            f"매물이 정확히 {need}명 등록되어야 합니다. (현재 {len(room.players)}명)",
        )
    _recompute_points(room)
    for team in room.teams:
        team.roster = []
    for p in room.players:
        p.winning_bid = 0
        p.assigned_team_id = -1

    order = list(range(len(room.players)))
    rng.shuffle(order)                       # Fisher-Yates (random.shuffle)
    room.main_queue = order
    room.unsold_queue = []
    room.leftover = []
    room.bracket = {"order": [], "pairs": [], "byes": []}
    room.state = AuctionState.MAIN_AUCTION
    room.paused = False
    _log(room, "start")
    _start_next_lot(room, now, rng)
    _touch(room)


def _start_next_lot(room: Room, now: float, rng: Rng) -> None:
    """다음 매물을 꺼내 타이머를 초기화한다. 꺼낼 매물이 없으면 자동 배정/대진 단계로 넘어간다."""
    room.current_lot = None
    room.highest_bid = 0
    room.highest_team = -1
    room.bid_history = []

    while True:
        open_teams = teams_with_open_slots(room)
        if not open_teams:
            _enter_bracket(room, rng)
            return
        if points_exhausted(room):
            # 빈자리가 있는 모든 팀이 포인트를 소진했다 -> 경매 강제 종료, 남은 선수 자동 배정
            _log(room, "forced_stop", reason="points_exhausted")
            _auto_assign(room, rng, reason="points_exhausted")
            return
        if len(open_teams) == 1:
            # 빈자리가 남은 팀이 하나뿐이면 경쟁이 불가능하므로 남은 매물을 즉시 0P 배정
            _auto_assign(room, rng, reason="last_team")
            return
        if room.main_queue:
            lot = room.main_queue.pop(0)
            break
        if room.state == AuctionState.MAIN_AUCTION and room.unsold_queue:
            # 정규 큐 소진 -> 유찰 큐를 메인 큐로 전환해 재경매 라운드 시작
            room.main_queue = room.unsold_queue
            room.unsold_queue = []
            room.state = AuctionState.UNSOLD_AUCTION
            _log(room, "unsold_round", count=len(room.main_queue))
            continue
        # 재경매까지 끝났는데 남은 선수(leftover)가 있다 -> 빈자리에 0P 자동 배정
        _auto_assign(room, rng, reason="leftover")
        return

    room.current_lot = lot
    room.lot_seq += 1
    room.remaining = float(room.settings.base_bid_time_sec)
    room.deadline = None if room.paused else now + room.remaining


# =============================================================================
# 입찰
# =============================================================================

def place_bid(room: Room, team_id: int, amount: Any, lot_seq: Any, now: float,
              rng: "Rng | None" = None) -> None:
    """팀장의 입찰을 검증하고 반영한다. 규칙 위반이면 AuctionError.

    호출 측은 먼저 tick() 으로 만료된 매물을 정리해 두어야 한다 (manager 가 보장).
    """
    # 상태 검증
    if not is_running(room) or room.current_lot is None:
        raise AuctionError("not_running", "진행 중인 경매가 없습니다.")
    if room.paused:
        raise AuctionError("paused", "경매가 일시정지 상태입니다.")
    if room.deadline is not None and now >= room.deadline:
        raise AuctionError("time_over", "입찰 시간이 종료되었습니다.")
    if isinstance(lot_seq, bool) or not isinstance(lot_seq, int) or lot_seq != room.lot_seq:
        raise AuctionError("stale_lot", "이미 다음 매물로 넘어갔습니다. 화면을 확인해 주세요.")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise AuctionError("bad_amount", "입찰 금액은 정수여야 합니다.")
    if not (0 <= team_id < len(room.teams)):
        raise AuctionError("no_team", "팀을 찾을 수 없습니다.")

    team = room.teams[team_id]

    # [검증 2] 로스터 빈자리
    if open_slots(room, team) <= 0:
        raise AuctionError("roster_full", "이미 팀 정원이 가득 찼습니다.")
    # [검증 3] 자가 연속 입찰 방지
    if room.highest_team == team.id:
        raise AuctionError("already_highest", "이미 최고 입찰자입니다.")
    # [검증 4] 최소 입찰 단위
    need = min_next_bid(room)
    if amount < need:
        raise AuctionError("bid_too_low", f"최소 {need}P 이상으로 입찰해야 합니다.")
    # [검증 5] 보유 포인트 한도 (유보금 포함)
    limit = max_bid_for(room, team)
    if amount > limit:
        raise AuctionError("insufficient_points", f"최대 {limit}P까지 입찰할 수 있습니다.")

    # 승인
    room.highest_bid = amount
    room.highest_team = team.id
    room.bid_history.append({"team": team.id, "amount": amount})
    if len(room.bid_history) > config.BID_HISTORY:
        del room.bid_history[: len(room.bid_history) - config.BID_HISTORY]

    if room.deadline is not None:
        if room.settings.timer_mode == "RESET":
            # 더 높은 입찰이 승인될 때마다 남은 시간을 기본 입찰 시간으로 되돌린다 (줄어들 일은 없다:
            # 기본 시간은 매물 시작 때의 최대치이므로 항상 그 이하에서 다시 채워진다).
            base = float(room.settings.base_bid_time_sec)
            room.deadline = now + base
            room.remaining = base
        else:
            ext = room.settings.extension_time_sec
            if ext > 0:
                left = max(0.0, room.deadline - now)
                if left < ext:                        # 남은 시간이 연장 시간보다 짧을 때만 회복
                    room.deadline = now + ext
                    room.remaining = float(ext)
    _touch(room)
    # 이 입찰을 넘길 수 있는 팀이 없으면 남은 시간을 기다릴 이유가 없다 -> 즉시 낙찰 (강제 낙찰)
    if rng is not None and nobody_can_outbid(room):
        _log(room, "forced_sold", reason="no_challenger", team=room.highest_team)
        _resolve_current_lot(room, now, rng)


# =============================================================================
# 시간 경과 / 낙찰·유찰
# =============================================================================

def tick(room: Room, now: float, rng: Rng) -> bool:
    """타이머를 검사해 만료됐다면 낙찰/유찰 처리 후 다음 매물로 넘어간다. 바뀌었으면 True."""
    if not is_running(room) or room.paused or room.deadline is None or room.current_lot is None:
        return False
    if now < room.deadline:
        return False
    _resolve_current_lot(room, now, rng)
    return True


def _resolve_current_lot(room: Room, now: float, rng: Rng) -> None:
    pid = room.current_lot
    assert pid is not None
    player = room.players[pid]
    if room.highest_team != -1:
        team = room.teams[room.highest_team]
        team.points -= room.highest_bid
        team.roster.append(pid)
        player.winning_bid = room.highest_bid
        player.assigned_team_id = team.id
        _log(room, "sold", player=pid, team=team.id, amount=room.highest_bid)
    else:
        if room.state == AuctionState.UNSOLD_AUCTION:
            room.leftover.append(pid)
        else:
            room.unsold_queue.append(pid)
        _log(room, "unsold", player=pid)
    _start_next_lot(room, now, rng)
    _touch(room)


def pause(room: Room, now: float) -> None:
    _require_running(room)
    if room.paused:
        return
    room.remaining = max(0.0, (room.deadline - now) if room.deadline is not None else room.remaining)
    room.deadline = None
    room.paused = True
    _log(room, "paused", reason="host")
    _touch(room)


def resume(room: Room, now: float) -> None:
    _require_running(room)
    if not room.paused:
        return
    room.paused = False
    room.deadline = now + room.remaining
    _log(room, "resumed")
    _touch(room)


def force_sold(room: Room, now: float, rng: Rng) -> None:
    """주최자 강제 낙찰: 현재 최고 입찰자에게 즉시 낙찰."""
    _require_running(room)
    if room.highest_team == -1:
        raise AuctionError("no_bid", "입찰자가 없어 낙찰할 수 없습니다.")
    _resolve_current_lot(room, now, rng)


def force_unsold(room: Room, now: float, rng: Rng) -> None:
    """주최자 유찰 처리: 입찰이 있어도 현재 매물을 유찰시킨다."""
    _require_running(room)
    room.highest_team = -1
    room.highest_bid = 0
    room.bid_history = []
    _resolve_current_lot(room, now, rng)


# =============================================================================
# 자동 배정 / 대진 선택
# =============================================================================

def _auto_assign(room: Room, rng: Rng, reason: str) -> None:
    """남은 모든 매물을 빈자리에 0P 로 무작위 배정한다 (교착 상태 방지)."""
    room.state = AuctionState.AUTO_ASSIGN
    pool = room.main_queue + room.unsold_queue + room.leftover
    room.main_queue, room.unsold_queue, room.leftover = [], [], []
    slots: list[int] = []
    for team in room.teams:
        slots.extend([team.id] * max(0, open_slots(room, team)))
    rng.shuffle(pool)
    rng.shuffle(slots)
    for pid, tid in zip(pool, slots):
        player = room.players[pid]
        player.winning_bid = 0
        player.assigned_team_id = tid
        room.teams[tid].roster.append(pid)
        _log(room, "auto_assign", player=pid, team=tid, reason=reason)
    _enter_bracket(room, rng)


def _unpaired(room: Room) -> list[int]:
    used = {t for pair in room.bracket["pairs"] for t in pair} | set(room.bracket["byes"])
    return [t for t in room.bracket["order"] if t not in used]


def _enter_bracket(room: Room, rng: Rng) -> None:
    room.state = AuctionState.BRACKET_SELECT
    room.paused = False
    room.deadline = None
    room.current_lot = None
    order = [t.id for t in room.teams]
    rng.shuffle(order)                                       # 동률은 무작위
    order.sort(key=lambda tid: -room.teams[tid].points)      # 잔여 포인트 내림차순 (stable)
    room.bracket = {"order": order, "pairs": [], "byes": []}
    _log(room, "bracket_start")
    _advance_bracket(room)


def _advance_bracket(room: Room) -> None:
    """선택지가 하나뿐인 경우 자동으로 확정하고, 다 끝나면 COMPLETED 로 전이."""
    unpaired = _unpaired(room)
    if len(unpaired) == 0:
        room.state = AuctionState.COMPLETED
        _log(room, "completed")
    elif len(unpaired) == 1:
        room.bracket["byes"].append(unpaired[0])
        _log(room, "bye", team=unpaired[0])
        room.state = AuctionState.COMPLETED
        _log(room, "completed")
    elif len(unpaired) == 2:
        room.bracket["pairs"].append([unpaired[0], unpaired[1]])
        _log(room, "pair", a=unpaired[0], b=unpaired[1], auto=True)
        room.state = AuctionState.COMPLETED
        _log(room, "completed")
    # 3팀 이상 남아 있으면 current_picker 의 선택을 기다린다.


def pick_opponent(room: Room, picker: int, opponent: Any) -> None:
    _require_state(room, AuctionState.BRACKET_SELECT)
    if isinstance(opponent, bool) or not isinstance(opponent, int):
        raise AuctionError("bad_value", "상대 팀 값이 올바르지 않습니다.")
    expected = current_picker(room)
    if expected is None or picker != expected:
        raise AuctionError("not_your_turn", "지금은 상대를 지명할 차례가 아닙니다.")
    if opponent == picker or opponent not in _unpaired(room):
        raise AuctionError("bad_opponent", "선택할 수 없는 상대입니다.")
    room.bracket["pairs"].append([picker, opponent])
    _log(room, "pair", a=picker, b=opponent, auto=False)
    _advance_bracket(room)
    _touch(room)
