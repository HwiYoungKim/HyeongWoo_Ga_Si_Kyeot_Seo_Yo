"""타이머 방식(RESET/EXTEND) 서버 통합 테스트."""
from tests.test_server_flow import env, setup_room, snap  # noqa: F401


def test_new_room_defaults_to_15s_reset(env):
    created, host, *_ = setup_room(env, settings={"base_bid_time_sec": 15, "timer_mode": "RESET"})
    st = snap(host)["settings"]
    assert st["base_bid_time_sec"] == 15 and st["timer_mode"] == "RESET"


def test_timer_mode_validation(env):
    created, host, *_ = setup_room(env)
    for bad in ("reset", "", None, 1, True):
        assert not host.emit("host_setup", {"settings": {"timer_mode": bad}}, callback=True)["ok"]
    assert host.emit("host_setup", {"settings": {"timer_mode": "EXTEND"}}, callback=True)["ok"]
    assert snap(host)["settings"]["timer_mode"] == "EXTEND"


def test_every_bid_resets_remaining_to_15s_over_socket(env):
    created, host, leaders, obs = setup_room(env, settings={"base_bid_time_sec": 15, "timer_mode": "RESET"})
    rid = created["room_id"]
    assert host.emit("host_start", {}, callback=True)["ok"]
    seq = snap(obs)["lot"]["seq"]
    env.tick(rid, 10)                                       # 15초 중 10초 경과 -> 5초 남음
    assert snap(obs)["remaining_ms"] == 5000
    assert leaders[0].emit("bid", {"amount": 10, "lot_seq": seq}, callback=True)["ok"]
    assert snap(obs)["remaining_ms"] == 15000               # 입찰 -> 15초로 초기화
    env.tick(rid, 14)
    assert leaders[1].emit("bid", {"amount": 20, "lot_seq": seq}, callback=True)["ok"]
    assert snap(obs)["remaining_ms"] == 15000
    env.tick(rid, 15)                                       # 입찰 없이 15초 -> 낙찰
    assert snap(obs)["lot"]["seq"] != seq
