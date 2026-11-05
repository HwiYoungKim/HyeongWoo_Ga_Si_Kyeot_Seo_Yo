"""경매 엔진 단위 테스트."""
import random

import pytest

from server import engine
from server.engine import AuctionError
from server.models import AuctionState, BidMode, Room, Settings

from tests.helpers import make_room


def err_code(fn, *args, **kwargs):
    with pytest.raises(AuctionError) as ei:
        fn(*args, **kwargs)
    return ei.value.code


# --------------------------------------------------------------------------- 시작

def test_start_requires_exact_player_count():
    room, rng = make_room(3, 3)
    engine.apply_players(room, [{"name": "a", "role": "tank", "elo": None, "intro": ""}])
    assert err_code(engine.start_auction, room, 0.0, rng) == "player_count"


def test_required_player_count_formula():
    room, _ = make_room(4, 5)
    assert engine.required_player_count(room) == 4 * (5 - 1)
    assert len(room.players) == 16


def test_start_shuffles_all_players_into_queue_and_opens_first_lot():
    room, rng = make_room(3, 3, start=True)
    assert room.state == AuctionState.MAIN_AUCTION
    assert room.current_lot is not None
    ids = sorted(room.main_queue + [room.current_lot])
    assert ids == list(range(6))
    assert room.remaining == room.settings.base_bid_time_sec
    assert room.deadline == room.settings.base_bid_time_sec  # now=0


def test_handicap_adjusts_initial_points():
    room, _ = make_room(3, 3, handicaps=[-300, 0, 500])
    assert [t.initial_points for t in room.teams] == [700, 1000, 1500]
    assert [t.points for t in room.teams] == [700, 1000, 1500]


def test_handicap_cannot_make_negative_points():
    room, _ = make_room(2, 3, handicaps=[-5000, 0])
    assert room.teams[0].initial_points == 0


# --------------------------------------------------------------------------- 입찰 검증

def test_bid_rejected_before_start():
    room, rng = make_room(3, 3)
    assert err_code(engine.place_bid, room, 0, 10, 0, 0.0) == "not_running"


def test_five_step_validation():
    s = Settings(base_points=100, fixed_min_increment=10, min_first_bid=0)
    room, rng = make_room(3, 3, settings=s, start=True)
    seq = room.lot_seq

    # 정상 첫 입찰
    engine.place_bid(room, 0, 20, seq, 1.0)
    assert (room.highest_bid, room.highest_team) == (20, 0)

    # 자가 연속 입찰
    assert err_code(engine.place_bid, room, 0, 50, seq, 1.5) == "already_highest"
    # 최소 단위 미달 (20 + 10 = 30 필요)
    assert err_code(engine.place_bid, room, 1, 29, seq, 2.0) == "bid_too_low"
    # 보유 포인트 초과
    assert err_code(engine.place_bid, room, 1, 101, seq, 2.0) == "insufficient_points"
    # 정상 재입찰
    engine.place_bid(room, 1, 30, seq, 2.0)
    assert room.highest_team == 1


def test_roster_full_team_cannot_bid():
    s = Settings(base_points=1000, base_bid_time_sec=10, extension_time_sec=0)
    room, rng = make_room(3, 2, settings=s, start=True)   # 팀당 매물 1명
    seq = room.lot_seq
    engine.place_bid(room, 0, 10, seq, 1.0)
    engine.tick(room, 11.0, rng)                          # 낙찰
    assert len(room.teams[0].roster) == 1
    assert err_code(engine.place_bid, room, 0, 20, room.lot_seq, 12.0) == "roster_full"


def test_stale_lot_seq_rejected():
    room, rng = make_room(3, 3, start=True)
    old = room.lot_seq
    engine.tick(room, 1000.0, rng)         # 입찰 없이 만료 -> 유찰 -> 다음 매물
    assert room.lot_seq == old + 1
    assert err_code(engine.place_bid, room, 0, 10, old, 1000.5) == "stale_lot"


@pytest.mark.parametrize("bad", [True, 1.5, "10", None, [10]])
def test_non_integer_amount_rejected(bad):
    room, _ = make_room(3, 3, start=True)
    assert err_code(engine.place_bid, room, 0, bad, room.lot_seq, 1.0) == "bad_amount"


@pytest.mark.parametrize("bad", [True, "1", None, 1.0])
def test_non_integer_lot_seq_rejected(bad):
    room, _ = make_room(3, 3, start=True)
    assert err_code(engine.place_bid, room, 0, 10, bad, 1.0) == "stale_lot"


def test_negative_amount_rejected_even_when_min_first_bid_zero():
    room, _ = make_room(3, 3, start=True)
    assert err_code(engine.place_bid, room, 0, -5, room.lot_seq, 1.0) == "bid_too_low"


def test_bid_after_deadline_rejected():
    s = Settings(base_bid_time_sec=10)
    room, _ = make_room(3, 3, settings=s, start=True)
    assert err_code(engine.place_bid, room, 0, 10, room.lot_seq, 10.0) == "time_over"


def test_unknown_team_rejected():
    room, _ = make_room(3, 3, start=True)
    assert err_code(engine.place_bid, room, 99, 10, room.lot_seq, 1.0) == "no_team"
    assert err_code(engine.place_bid, room, -1, 10, room.lot_seq, 1.0) == "no_team"


def test_ratio_increment_uses_ceil_and_minimum_one():
    s = Settings(bid_mode=BidMode.RATIO.value, ratio_min_increment=0.10, min_first_bid=0)
    room, _ = make_room(3, 3, settings=s, start=True)
    seq = room.lot_seq
    engine.place_bid(room, 0, 0, seq, 1.0)
    assert engine.min_next_bid(room) == 1             # 0의 10%는 0 -> 최소 1
    engine.place_bid(room, 1, 15, seq, 1.0)
    assert engine.min_next_bid(room) == 17            # 15 * 10% = 1.5 -> 올림 2
    engine.place_bid(room, 2, 100, seq, 1.0)
    assert engine.min_next_bid(room) == 110


def test_fixed_increment_min_next_bid():
    s = Settings(fixed_min_increment=25, min_first_bid=5)
    room, _ = make_room(3, 3, settings=s, start=True)
    assert engine.min_next_bid(room) == 5
    engine.place_bid(room, 0, 5, room.lot_seq, 1.0)
    assert engine.min_next_bid(room) == 30


def test_reserve_per_slot_limits_max_bid():
    s = Settings(base_points=100, reserve_per_slot=20)
    room, _ = make_room(3, 4, settings=s, start=True)     # 팀당 매물 3칸
    team = room.teams[0]
    assert engine.max_bid_for(room, team) == 100 - 20 * 2
    assert err_code(engine.place_bid, room, 0, 61, room.lot_seq, 1.0) == "insufficient_points"
    engine.place_bid(room, 0, 60, room.lot_seq, 1.0)


# --------------------------------------------------------------------------- 타이머

def test_extension_restores_time_only_when_below_extension():
    s = Settings(base_bid_time_sec=30, extension_time_sec=10)
    room, _ = make_room(3, 3, settings=s, start=True)
    seq = room.lot_seq
    engine.place_bid(room, 0, 10, seq, 5.0)               # 남은 25초 > 10 -> 그대로
    assert room.deadline == 30.0
    engine.place_bid(room, 1, 20, seq, 25.0)              # 남은 5초 < 10 -> 10초로 회복
    assert room.deadline == 35.0
    engine.place_bid(room, 0, 30, seq, 34.0)              # 남은 1초 -> 10초
    assert room.deadline == 44.0


def test_zero_extension_is_reflex_mode():
    s = Settings(base_bid_time_sec=10, extension_time_sec=0)
    room, _ = make_room(3, 3, settings=s, start=True)
    engine.place_bid(room, 0, 10, room.lot_seq, 9.0)
    assert room.deadline == 10.0


def test_tick_before_deadline_changes_nothing():
    room, rng = make_room(3, 3, start=True)
    rev = room.rev
    assert engine.tick(room, 1.0, rng) is False
    assert room.rev == rev


def test_sold_deducts_points_and_fills_roster():
    s = Settings(base_points=500, base_bid_time_sec=10, extension_time_sec=0)
    room, rng = make_room(3, 3, settings=s, start=True)
    lot = room.current_lot
    engine.place_bid(room, 2, 120, room.lot_seq, 1.0)
    assert engine.tick(room, 10.0, rng) is True
    team = room.teams[2]
    assert team.points == 380
    assert team.roster == [lot]
    assert room.players[lot].winning_bid == 120
    assert room.players[lot].assigned_team_id == 2
    assert room.current_lot != lot


def test_pause_freezes_timer_and_blocks_bids():
    s = Settings(base_bid_time_sec=30, extension_time_sec=0)
    room, rng = make_room(3, 3, settings=s, start=True)
    engine.pause(room, 12.0)
    assert room.paused and room.remaining == pytest.approx(18.0)
    assert engine.tick(room, 9999.0, rng) is False        # 멈춘 동안은 만료되지 않는다
    assert err_code(engine.place_bid, room, 0, 10, room.lot_seq, 100.0) == "paused"
    engine.resume(room, 100.0)
    assert room.deadline == pytest.approx(118.0)
    engine.place_bid(room, 0, 10, room.lot_seq, 101.0)


# --------------------------------------------------------------------------- 유찰/재경매/자동배정

def run_all_unsold(room, rng, start_time=0.0, step=1000.0):
    t = start_time
    guard = 0
    while engine.is_running(room):
        t += step
        engine.tick(room, t, rng)
        guard += 1
        assert guard < 1000, "무한 루프"
    return t


def test_unsold_goes_to_queue_then_reauction_then_auto_assign():
    room, rng = make_room(3, 3, start=True)               # 매물 6
    engine.tick(room, 1000.0, rng)                        # 1번 유찰
    assert len(room.unsold_queue) == 1
    t = 1000.0
    # 정규 큐(남은 5명)를 모두 유찰
    for _ in range(5):
        t += 1000.0
        engine.tick(room, t, rng)
    assert room.state == AuctionState.UNSOLD_AUCTION      # 6명 재경매 라운드 시작
    assert len(room.main_queue) + 1 == 6
    run_all_unsold(room, rng, t)
    assert room.state == AuctionState.BRACKET_SELECT      # 3팀 -> 대진 지명 대기
    assert all(len(tm.roster) == 2 for tm in room.teams)
    assert all(p.assigned_team_id >= 0 and p.winning_bid == 0 for p in room.players)


def test_reauction_allows_selling_unsold_players():
    s = Settings(base_bid_time_sec=10, extension_time_sec=0)
    room, rng = make_room(2, 2, settings=s, start=True)   # 매물 2, 팀당 1
    t = 100.0
    engine.tick(room, t, rng)                             # 첫 매물 유찰
    t += 100.0
    engine.tick(room, t, rng)                             # 둘째도 유찰 -> 재경매 시작
    assert room.state == AuctionState.UNSOLD_AUCTION
    engine.place_bid(room, 1, 7, room.lot_seq, t + 1)
    engine.tick(room, t + 20, rng)                        # 낙찰
    # 팀1 정원 충족 -> 빈자리 있는 팀은 팀0 하나뿐 -> 마지막 매물 자동 배정 후 종료
    assert room.state == AuctionState.COMPLETED
    sold = [p for p in room.players if p.winning_bid == 7]
    assert len(sold) == 1 and sold[0].assigned_team_id == 1
    other = [p for p in room.players if p.winning_bid == 0]
    assert len(other) == 1 and other[0].assigned_team_id == 0


def test_single_open_team_gets_remaining_players_for_zero():
    s = Settings(base_bid_time_sec=10, extension_time_sec=0)
    room, rng = make_room(3, 3, settings=s, start=True)   # 팀당 2칸
    t = 0.0
    # 팀1이 두 명을, 팀2가 두 명을 낙찰 -> 빈자리 있는 팀은 팀0 하나
    winners = [1, 2, 1, 2]
    for w in winners:
        engine.place_bid(room, w, 10, room.lot_seq, t + 1)
        t += 20
        engine.tick(room, t, rng)
    assert room.state == AuctionState.BRACKET_SELECT      # 3팀 -> 대진 지명 대기
    assert len(room.teams[0].roster) == 2
    assert all(room.players[pid].winning_bid == 0 for pid in room.teams[0].roster)


def test_force_sold_and_force_unsold():
    room, rng = make_room(3, 3, start=True)
    assert err_code(engine.force_sold, room, 1.0, rng) == "no_bid"
    lot = room.current_lot
    engine.place_bid(room, 1, 50, room.lot_seq, 1.0)
    engine.force_sold(room, 2.0, rng)
    assert room.players[lot].assigned_team_id == 1

    lot2 = room.current_lot
    engine.place_bid(room, 0, 40, room.lot_seq, 3.0)
    engine.force_unsold(room, 4.0, rng)
    assert room.players[lot2].assigned_team_id == -1
    assert lot2 in room.unsold_queue
    assert room.teams[0].points == room.teams[0].initial_points   # 입찰은 무효


# --------------------------------------------------------------------------- 대진 선택

def finish_with_points(points_by_team, seed=3):
    """팀별 최종 잔여 포인트가 정해진 상태로 경매를 끝낸다 (자동 배정만 사용)."""
    n = len(points_by_team)
    s = Settings(base_points=1000)
    room, rng = make_room(n, 2, settings=s, seed=seed, start=True)
    for t, p in zip(room.teams, points_by_team):
        t.points = p
    run_all_unsold(room, rng)
    return room


def test_bracket_priority_is_by_remaining_points_desc_without_reordering_teams():
    room = finish_with_points([100, 900, 500, 300])
    assert [t.id for t in room.teams] == [0, 1, 2, 3]             # 팀 배열은 그대로 (의사코드 qsort 결함 방지)
    assert room.bracket["order"] == [1, 2, 3, 0]
    assert room.state == AuctionState.BRACKET_SELECT
    assert engine.current_picker(room) == 1


def test_bracket_pick_flow_four_teams():
    room = finish_with_points([100, 900, 500, 300])
    assert err_code(engine.pick_opponent, room, 2, 3) == "not_your_turn"
    assert err_code(engine.pick_opponent, room, 1, 1) == "bad_opponent"
    assert err_code(engine.pick_opponent, room, 1, 99) == "bad_opponent"
    assert err_code(engine.pick_opponent, room, 1, True) == "bad_value"
    engine.pick_opponent(room, 1, 0)
    # 남은 팀 2, 3 -> 선택지가 하나라 자동 확정
    assert room.state == AuctionState.COMPLETED
    assert room.bracket["pairs"] == [[1, 0], [2, 3]]
    assert room.bracket["byes"] == []


def test_bracket_odd_teams_gives_bye():
    room = finish_with_points([100, 900, 500])
    assert engine.current_picker(room) == 1
    engine.pick_opponent(room, 1, 2)
    assert room.state == AuctionState.COMPLETED
    assert room.bracket["pairs"] == [[1, 2]]
    assert room.bracket["byes"] == [0]


def test_bracket_two_teams_auto_pairs():
    room = finish_with_points([300, 700])
    assert room.state == AuctionState.COMPLETED
    assert room.bracket["pairs"] == [[1, 0]]


def test_bracket_tie_is_broken_randomly_but_deterministically_per_seed():
    a = finish_with_points([500, 500, 500, 500], seed=11)
    b = finish_with_points([500, 500, 500, 500], seed=11)
    c = finish_with_points([500, 500, 500, 500], seed=12)
    assert a.bracket["order"] == b.bracket["order"]
    assert sorted(a.bracket["order"]) == [0, 1, 2, 3]
    assert sorted(c.bracket["order"]) == [0, 1, 2, 3]


# --------------------------------------------------------------------------- 설정 잠금

def test_settings_locked_after_start():
    room, rng = make_room(3, 3, start=True)
    assert err_code(engine.apply_settings, room, {"base_points": 5}) == "bad_state"
    assert err_code(engine.apply_players, room, []) == "bad_state"
    assert err_code(engine.apply_teams, room, []) == "bad_state"
    assert err_code(engine.start_auction, room, 0.0, rng) == "bad_state"


# --------------------------------------------------------------------------- 저장/복구

def test_roundtrip_and_restart_pauses_running_auction():
    room, rng = make_room(3, 3, start=True)
    engine.place_bid(room, 1, 33, room.lot_seq, 4.0)
    engine.sync_remaining(room, 10.0)
    data = room.to_dict()
    restored = Room.from_dict(data)
    engine.restore_after_restart(restored)
    assert restored.paused is True
    assert restored.deadline is None
    assert restored.highest_bid == 33 and restored.highest_team == 1
    assert restored.remaining == pytest.approx(room.remaining)
    assert restored.to_dict()["players"] == data["players"]
    engine.resume(restored, 500.0)
    assert restored.deadline == pytest.approx(500.0 + room.remaining)


# --------------------------------------------------------------------------- 무작위 시뮬레이션

@pytest.mark.parametrize("seed", range(40))
def test_random_simulation_keeps_invariants(seed):
    r = random.Random(seed)
    n = r.randint(2, 6)
    m = r.randint(2, 5)
    s = Settings(
        base_points=r.choice([0, 50, 300, 1000]),
        bid_mode=r.choice(["FIXED", "RATIO"]),
        fixed_min_increment=r.choice([1, 5, 10]),
        ratio_min_increment=r.choice([0.05, 0.1, 0.5]),
        min_first_bid=r.choice([0, 1, 10]),
        reserve_per_slot=r.choice([0, 0, 10]),
        base_bid_time_sec=10,
        extension_time_sec=r.choice([0, 3, 10]),
    )
    handicaps = [r.randint(-100, 300) for _ in range(n)]
    room, rng = make_room(n, m, settings=s, seed=seed, start=True, handicaps=handicaps)
    initial = [t.initial_points for t in room.teams]
    t = 0.0
    for _ in range(5000):
        if not engine.is_running(room):
            break
        t += r.choice([0.5, 1.0, 3.0, 8.0, 15.0])
        engine.tick(room, t, rng)
        if not engine.is_running(room):
            break
        for _ in range(r.randint(0, 3)):
            tid = r.randrange(n)
            amount = engine.min_next_bid(room) + r.choice([0, 0, 1, 5, 20])
            try:
                engine.place_bid(room, tid, amount, room.lot_seq, t)
            except AuctionError:
                pass
            if r.random() < 0.05:
                try:
                    engine.pause(room, t)
                    t += 5
                    engine.resume(room, t)
                except AuctionError:
                    pass
    else:
        pytest.fail("경매가 종료되지 않음")

    # 대진 단계까지 진행
    while room.state == AuctionState.BRACKET_SELECT:
        picker = engine.current_picker(room)
        choices = [x for x in engine._unpaired(room) if x != picker]
        engine.pick_opponent(room, picker, r.choice(choices))

    assert room.state == AuctionState.COMPLETED
    # 모든 선수는 정확히 한 팀에 배정
    assigned = [pid for tm in room.teams for pid in tm.roster]
    assert sorted(assigned) == list(range(len(room.players)))
    for p in room.players:
        assert room.teams[p.assigned_team_id].roster.count(p.id) == 1
    # 정원 충족, 포인트 보존
    for tm, init in zip(room.teams, initial):
        assert len(tm.roster) == m - 1
        assert tm.points >= 0
        assert tm.points == init - sum(room.players[pid].winning_bid for pid in tm.roster)
    # 대진: 모든 팀이 정확히 한 번 편성
    teams_in_bracket = [x for pair in room.bracket["pairs"] for x in pair] + room.bracket["byes"]
    assert sorted(teams_in_bracket) == list(range(n))
    assert room.main_queue == [] and room.unsold_queue == [] and room.leftover == []
