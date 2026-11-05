"""포인트 소진 강제 정지/강제 낙찰, 유찰자 목록 테스트."""
import random

from server import engine
from server.models import AuctionState, Settings
from tests.helpers import make_room
from tests.test_server_flow import env, setup_room, snap  # noqa: F401


def rng():
    return random.Random(3)


def test_all_teams_out_of_points_at_start_forces_auto_assign():
    s = Settings(base_points=0)
    room, r = make_room(3, 3, settings=s, start=True)
    # 시작하자마자 아무도 입찰할 수 없다 -> 경매 없이 자동 배정되고 대진 단계로
    assert room.state in (AuctionState.BRACKET_SELECT, AuctionState.COMPLETED)
    assert room.current_lot is None
    assert all(len(t.roster) == 2 for t in room.teams)
    assert any(e["type"] == "forced_stop" for e in room.activity)
    assert all(p.winning_bid == 0 and p.assigned_team_id >= 0 for p in room.players)


def test_stops_when_last_points_are_spent_in_main_auction():
    s = Settings(base_points=100, timer_mode="RESET", fixed_min_increment=10, min_first_bid=10)
    room, r = make_room(3, 3, settings=s, start=True)      # 3팀, 팀당 빈자리 2, 매물 6
    t = 0.0
    # 세 팀이 각각 100P 를 한 번에 써서 포인트를 전부 소진한다
    for team in range(3):
        seq = room.lot_seq
        engine.place_bid(room, team, 100, seq, t, r)       # 남은 팀이 넘길 수 없다면 즉시 낙찰되기도 한다
        if room.current_lot is not None and room.lot_seq == seq:
            engine.force_sold(room, t, r)
    assert room.state in (AuctionState.BRACKET_SELECT, AuctionState.COMPLETED)
    assert any(e["type"] == "forced_stop" for e in room.activity)
    assert all(len(t.roster) == 2 for t in room.teams)     # 남은 선수는 모두 자동 배정됨
    assert [p for p in room.players if p.assigned_team_id == -1] == []


def test_stops_in_retry_round_too():
    s = Settings(base_points=50, min_first_bid=10, timer_mode="RESET")
    room, r = make_room(3, 3, settings=s, start=True)
    # 정규 경매에서 5명을 유찰시켜 재경매 라운드로 보낸다 (6명 중 1명만 낙찰)
    engine.place_bid(room, 0, 50, room.lot_seq, 0.0, r)     # 넘길 팀이 없어 즉시 낙찰된다
    assert room.teams[0].roster
    guard = 0
    while room.state == AuctionState.MAIN_AUCTION and guard < 20:
        engine.force_unsold(room, 0.0, r)
        guard += 1
    assert room.state == AuctionState.UNSOLD_AUCTION
    # 재경매에서 나머지 두 팀이 가진 포인트를 모두 쓴다
    for team in (1, 2):
        if room.state != AuctionState.UNSOLD_AUCTION:
            break
        engine.place_bid(room, team, 50, room.lot_seq, 1.0, r)
    assert room.state in (AuctionState.BRACKET_SELECT, AuctionState.COMPLETED)
    assert [p for p in room.players if p.assigned_team_id == -1] == []


def test_instant_sale_when_nobody_can_outbid():
    s = Settings(base_points=100, min_first_bid=10, fixed_min_increment=10, timer_mode="RESET")
    room, r = make_room(3, 3, settings=s, start=True)
    room.teams[1].points = 40                              # 다른 팀들은 최소 인상액을 못 낸다
    room.teams[2].points = 40
    lot = room.current_lot
    engine.place_bid(room, 0, 100, room.lot_seq, 1.0, r)   # 100P 를 넘길 수 있는 팀이 없다
    assert room.players[lot].assigned_team_id == 0          # 15초를 기다리지 않고 즉시 낙찰
    assert any(e["type"] == "forced_sold" for e in room.activity)


def test_no_instant_sale_when_someone_can_still_outbid():
    s = Settings(base_points=1000, min_first_bid=10, fixed_min_increment=10)
    room, r = make_room(3, 3, settings=s, start=True)
    lot = room.current_lot
    engine.place_bid(room, 0, 100, room.lot_seq, 1.0, r)
    assert room.current_lot == lot and room.players[lot].assigned_team_id == -1


def test_place_bid_without_rng_never_forces():
    s = Settings(base_points=100, min_first_bid=10)
    room, r = make_room(3, 3, settings=s, start=True)
    room.teams[1].points = room.teams[2].points = 0
    lot = room.current_lot
    engine.place_bid(room, 0, 100, room.lot_seq, 1.0)       # rng 를 안 주면 기존처럼 타이머에 맡긴다
    assert room.current_lot == lot


def test_points_exhausted_requires_open_slots():
    room, r = make_room(3, 2, settings=Settings(base_points=100), start=True)
    assert engine.points_exhausted(room) is False


# --------------------------------------------------------------------------- 유찰자 목록

def test_unsold_list_is_public_even_in_blind_mode(env):
    created, host, leaders, obs = setup_room(env, settings={"order_public": False})
    assert host.emit("host_start", {}, callback=True)["ok"]
    assert snap(obs)["queue"]["unsold_players"] == []
    first = snap(obs)["lot"]["player"]
    assert host.emit("host_force_unsold", {}, callback=True)["ok"]
    for who in (obs, leaders[0], host):
        lst = snap(who)["queue"]["unsold_players"]
        assert [p["name"] for p in lst] == [first["name"]]
        assert set(lst[0]) == {"id", "name", "role", "round"} and lst[0]["round"] == "retry"
    # 대기 중인 다른 선수의 이름은 여전히 공개 화면에 없다 (BLIND)
    import json
    text = json.dumps(snap(obs))
    others = [p["name"] for p in snap(host)["players"] if p["name"] != first["name"] and p["name"] != snap(obs)["lot"]["player"]["name"]]
    assert all(n not in text for n in others)


def test_unsold_list_tracks_retry_round_and_leftover(env):
    created, host, leaders, obs = setup_room(env, n=3, m=3, settings={"base_points": 1000})
    assert host.emit("host_start", {}, callback=True)["ok"]
    for _ in range(6):                                      # 정규 경매 6명 전원 유찰
        assert host.emit("host_force_unsold", {}, callback=True)["ok"]
    st = snap(obs)
    assert st["state"] == "UNSOLD_AUCTION"
    names_unsold = {p["name"] for p in st["queue"]["unsold_players"]}
    assert len(names_unsold) == 5                           # 지금 경매 중인 1명은 목록에서 빠진다
    cur = st["lot"]["player"]["name"]
    assert host.emit("host_force_unsold", {}, callback=True)["ok"]   # 재경매에서도 유찰
    lst = snap(obs)["queue"]["unsold_players"]
    assert any(p["name"] == cur and p["round"] == "leftover" for p in lst)
