"""역할별 화면 데이터(스냅샷) 생성.

선수의 신상 정보(이름/포지션/Elo/소개)를 누구에게 언제 보여줄지를 이 파일 한 곳에서 통제한다.
네트워크로 나가는 모든 상태 데이터는 반드시 여기서 만든 값만 사용한다 (Room 객체를 직접 직렬화 금지).

공개 범위
- 주최자(host): 전체. 등록한 선수 전원의 정보와 큐 순서까지.
- 팀장/관전자(public): "지금 경매 중인 매물"은 전체 정보(이름/배틀태그/포지션/영웅/Elo/소개/이미지),
  "이미 팀에 영입된 선수"는 이름/포지션/Elo/낙찰가, "대기 중 매물"은 순서 공개(PUBLIC) 모드일 때만
  이름/포지션만, 블라인드(BLIND) 모드에서는 인원수만. 경매 시작 전에는 등록 인원수만 보인다.
- 이미지: 스냅샷에는 이미지 id 만 들어간다. 실제 파일은 http_routes 의 이미지 API 가 내려주고,
  그곳에서도 visible_image_ids() 로 같은 규칙을 다시 검사한다. (id 를 알아도 권한이 없으면 못 받는다)
- 토큰 해시, 다른 사람의 접속 정보 등은 어떤 역할에도 내려가지 않는다.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import engine
from .models import AuctionState, Player, Room


def _player_card(p: Player) -> dict[str, Any]:
    """현재 경매 중인 매물: 전체 정보."""
    return {
        "id": p.id, "name": p.name, "battletag": p.battletag, "role": p.role,
        "heroes": list(p.heroes), "images": list(p.images), "elo": p.elo, "intro": p.intro,
    }


def _player_brief(p: Player) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "role": p.role}


def _roster_entry(p: Player) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "role": p.role, "elo": p.elo,
            "winning_bid": p.winning_bid}


def visible_image_ids(room: Room, role: str) -> set[str]:
    """이 역할이 받아갈 수 있는 이미지 id 집합. 이미지 API 의 권한 검사 기준이다.

    - host: 이 방에 등록된 모든 선수의 이미지.
    - 그 외(팀장/관전자): 지금 경매 중인 매물의 이미지뿐. 대기 중인 선수의 이미지는 받을 수 없다.
    """
    if role == "host":
        return {iid for p in room.players for iid in p.images}
    if room.current_lot is not None and engine.is_running(room) and 0 <= room.current_lot < len(room.players):
        return set(room.players[room.current_lot].images)
    return set()


def _activity(room: Room) -> list[dict[str, Any]]:
    """활동 로그: 선수 ID 를 이름으로 풀어서 내려준다 (이미 공개된 선수만 등장한다)."""
    out = []
    for e in room.activity:
        item = dict(e)
        if "player" in item:
            pid = item.pop("player")
            if 0 <= pid < len(room.players):
                item["player_name"] = room.players[pid].name
        out.append(item)
    return out


def _settings_view(room: Room) -> dict[str, Any]:
    return room.settings.to_dict()


def _base(room: Room, now: float, online_teams: Iterable[int], host_online: bool) -> dict[str, Any]:
    open_total = sum(max(0, engine.open_slots(room, t)) for t in room.teams)
    teams = []
    for t in room.teams:
        teams.append({
            "id": t.id,
            "leader_name": t.leader_name,
            "leader_position": t.leader_position,
            "points": t.points,
            "initial_points": t.initial_points,
            "open_slots": max(0, engine.open_slots(room, t)),
            "max_bid": engine.max_bid_for(room, t) if engine.is_running(room) else 0,
            "roster": [_roster_entry(room.players[pid]) for pid in t.roster],
        })

    lot = None
    if room.current_lot is not None and engine.is_running(room):
        lot = {
            "seq": room.lot_seq,
            "player": _player_card(room.players[room.current_lot]),
            "highest_bid": room.highest_bid,
            "highest_team": room.highest_team,
            "min_next_bid": engine.min_next_bid(room),
            "bids": list(room.bid_history),
        }

    in_auction = room.state != AuctionState.WAITING
    queue: dict[str, Any] = {
        "registered": len(room.players),
        "required": engine.required_player_count(room),
        "main_count": len(room.main_queue),
        "unsold_count": len(room.unsold_queue),
        "leftover_count": len(room.leftover),
        "upcoming": None,
    }
    if in_auction and room.settings.order_public:
        queue["upcoming"] = [_player_brief(room.players[pid]) for pid in room.main_queue]
        queue["unsold"] = [_player_brief(room.players[pid]) for pid in room.unsold_queue]

    return {
        "rev": room.rev,
        "state": room.state.value,
        "paused": room.paused,
        "room": {
            "id": room.id,
            "title": room.title,
            "num_teams": room.num_teams,
            "members_per_team": room.members_per_team,
        },
        "settings": _settings_view(room),
        "teams": teams,
        "lot": lot,
        "remaining_ms": int(engine.remaining_sec(room, now) * 1000) if lot else 0,
        "queue": queue,
        "open_slots_total": open_total,
        "bracket": {
            "order": list(room.bracket.get("order", [])),
            "pairs": [list(p) for p in room.bracket.get("pairs", [])],
            "byes": list(room.bracket.get("byes", [])),
            "picker": engine.current_picker(room),
        },
        "activity": _activity(room),
        "online_teams": sorted(set(online_teams)),
        "host_online": host_online,
    }


def public_view(room: Room, now: float, online_teams: Iterable[int], host_online: bool) -> dict[str, Any]:
    """팀장·관전자에게 내려가는 공통 화면 데이터."""
    view = _base(room, now, online_teams, host_online)
    view["role_view"] = "public"
    return view


def host_view(room: Room, now: float, online_teams: Iterable[int], host_online: bool) -> dict[str, Any]:
    """주최자 화면 데이터: 공통 데이터 + 전체 선수 정보/큐 순서/핸디캡."""
    view = _base(room, now, online_teams, host_online)
    view["role_view"] = "host"
    view["players"] = [
        {**_player_card(p), "uid": p.uid, "winning_bid": p.winning_bid, "team": p.assigned_team_id}
        for p in room.players
    ]
    view["queue"]["main_ids"] = list(room.main_queue)
    view["queue"]["unsold_ids"] = list(room.unsold_queue)
    view["queue"]["leftover_ids"] = list(room.leftover)
    for tv, t in zip(view["teams"], room.teams):
        tv["handicap"] = t.handicap
    view["required_players"] = engine.required_player_count(room)
    return view


def timer_payload(room: Room, now: float) -> dict[str, Any]:
    """1초마다 보내는 가벼운 타이머 재동기화 메시지."""
    return {
        "rev": room.rev,
        "lot_seq": room.lot_seq,
        "remaining_ms": int(engine.remaining_sec(room, now) * 1000),
        "paused": room.paused,
    }
