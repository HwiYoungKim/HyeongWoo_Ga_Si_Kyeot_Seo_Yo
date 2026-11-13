"""상태 전송 규약: 접속 때 전체 상태(seq 포함), 그 뒤로는 변경분(patch)만.

여기의 Tracker 는 브라우저 클라이언트(static/js/lib/net.js)와 같은 규칙으로 동작한다:
순번이 이어지면 적용하고, 어긋나면 실패로 본다(실제 클라이언트는 sync 를 요청한다).
"""
import json
import random

import pytest

from server import engine, security, socket_events, statediff
from tests.test_server_flow import apply_events, env, setup_room, snap  # noqa: F401


def size(obj):
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode())


def comparable(view):
    """시간에 따라 변하는 값은 빼고 비교한다."""
    return {k: v for k, v in view.items() if k != "remaining_ms"}


class Tracker:
    def __init__(self, client):
        self.client = client
        self.view = client._view
        self.events = []            # (이름, 크기)

    def pump(self):
        received = [e for e in self.client.get_received() if e["name"] in ("state", "patch")]
        self.events += [(e["name"], size(e["args"][0])) for e in received]
        view = apply_events(self.view, received)
        assert view is not None, "순번이 이어지지 않았다 (클라이언트가 재동기화해야 하는 상황)"
        self.view = view
        return received

    def assert_in_sync(self):
        self.pump()
        fresh = snap(self.client)
        assert fresh["seq"] == self.view["seq"]
        assert comparable(fresh) == comparable(self.view)
        assert self.client.get_received() == [] or all(
            e["name"] not in ("state", "patch") for e in self.client.get_received())


def test_join_snapshot_carries_sequence_number(env):
    created, host, leaders, obs = setup_room(env)
    assert isinstance(obs._view["seq"], int) and obs._view["seq"] >= 1
    assert isinstance(host._view["seq"], int)
    assert obs._view["role_view"] == "public" and host._view["role_view"] == "host"


def test_bid_sends_one_small_patch_instead_of_full_state(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    t_obs, t_host = Tracker(obs), Tracker(host)
    t_obs.pump(), t_host.pump()
    seq = t_obs.view["lot"]["seq"]
    full_pub, full_host = size(t_obs.view), size(t_host.view)

    assert leaders[1].emit("bid", {"amount": 50, "lot_seq": seq}, callback=True)["ok"]
    for tracker, full in ((t_obs, full_pub), (t_host, full_host)):
        received = tracker.pump()
        assert [e["name"] for e in received] == ["patch"]
        message = received[0]["args"][0]
        assert set(message) == {"base", "seq", "remaining_ms", "p"} and message["seq"] == message["base"] + 1
        assert size(message) < 0.25 * full, (size(message), full)
        assert tracker.view["lot"]["highest_bid"] == 50 and tracker.view["lot"]["highest_team"] == 1
        assert tracker.view["remaining_ms"] > 0
        tracker.assert_in_sync()


def test_views_stay_identical_to_server_through_a_whole_auction(env):
    created, host, leaders, obs = setup_room(env, n=4, m=4, settings={"base_bid_time_sec": 5, "extension_time_sec": 0})
    rid = created["room_id"]
    # 이 테스트는 검증용 sync 를 아주 빠르게 반복하므로 요청 속도 제한을 풀어 둔다
    for name in ("default", "bid", "host"):
        env.app.api.buckets[name] = security.RateLimiter(capacity=10**6, refill_per_sec=10**6)
    trackers = [Tracker(c) for c in (host, obs, leaders[0], leaders[3])]
    rng = random.Random(3)

    def check():
        for t in trackers:
            t.assert_in_sync()

    assert host.emit("host_start", {}, callback=True)["ok"]
    check()
    for step in range(200):
        st = snap(obs)
        if st["state"] in ("BRACKET_SELECT", "COMPLETED"):
            break
        action = rng.random()
        if st["lot"] and action < 0.55:
            team = rng.randrange(4)
            leaders[team].emit("bid", {"amount": st["lot"]["min_next_bid"] + rng.choice([0, 10, 40]),
                                       "lot_seq": st["lot"]["seq"]}, callback=True)      # 거절돼도 상관없다
        elif action < 0.65:
            host.emit("host_pause" if not st["paused"] else "host_resume", {}, callback=True)
        elif action < 0.72:
            host.emit("host_force_unsold", {}, callback=True)
        else:
            env.tick(rid, 6)
        check()
    for _ in range(10):
        st = snap(obs)
        if st["state"] == "COMPLETED":
            break
        assert st["state"] == "BRACKET_SELECT"
        if st["paused"]:
            host.emit("host_resume", {}, callback=True)
        picker = st["bracket"]["picker"]
        used = {t for pair in st["bracket"]["pairs"] for t in pair} | set(st["bracket"]["byes"])
        options = [t for t in st["bracket"]["order"] if t not in used and t != picker]
        assert leaders[picker].emit("pick_opponent", {"opponent": options[0]}, callback=True)["ok"]
        check()
    assert snap(obs)["state"] == "COMPLETED"
    check()

    # 관전자 한 명이 받은 양: 전체 상태를 매번 보냈을 때의 30% 이하
    t_obs = trackers[1]
    sent = sum(n for _, n in t_obs.events)
    as_full_states = len(t_obs.events) * size(t_obs.view)
    assert len(t_obs.events) > 20
    assert sent < 0.3 * as_full_states, (sent, as_full_states)
    print(f"\n관전자 수신: 변경분 {sent}B / 전체 상태였다면 {as_full_states}B = {sent / as_full_states:.0%} ({len(t_obs.events)}회)")


def test_late_joiner_continues_from_snapshot(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    seq = snap(obs)["lot"]["seq"]
    assert leaders[0].emit("bid", {"amount": 30, "lot_seq": seq}, callback=True)["ok"]

    late = env.client()
    assert env.join(late, created["room_id"], created["tokens"]["observer"], nickname="늦은사람")["ok"]
    tracker = Tracker(late)
    assert tracker.view["lot"]["highest_bid"] == 30
    assert leaders[2].emit("bid", {"amount": 80, "lot_seq": seq}, callback=True)["ok"]
    tracker.assert_in_sync()
    assert tracker.view["lot"]["highest_team"] == 2


def test_observer_join_and_sync_do_not_broadcast(env):
    created, host, leaders, obs = setup_room(env)
    obs.get_received(), host.get_received()
    before = snap(obs)["seq"]
    newcomer = env.client()
    assert env.join(newcomer, created["room_id"], created["tokens"]["observer"])["ok"]
    assert snap(newcomer)["seq"] == before                     # 바뀐 것이 없으면 순번도 그대로
    for client in (obs, host):
        assert [e["name"] for e in client.get_received() if e["name"] in ("state", "patch")] == []


def test_public_and_host_sequences_are_independent_and_host_patch_is_partial(env):
    created, host, leaders, obs = setup_room(env, n=3, m=2, settings={"base_bid_time_sec": 5, "extension_time_sec": 0})
    rid = created["room_id"]
    assert host.emit("host_start", {}, callback=True)["ok"]
    t_host, t_obs = Tracker(host), Tracker(obs)
    t_host.pump(), t_obs.pump()
    seq = t_obs.view["lot"]["seq"]
    assert leaders[0].emit("bid", {"amount": 100, "lot_seq": seq}, callback=True)["ok"]
    t_host.pump(), t_obs.pump()
    env.tick(rid, 6)                                           # 낙찰
    received = t_host.pump()
    patch = received[-1]["args"][0]["p"]
    assert received[-1]["name"] == "patch" and patch[0] == "o"
    assert patch[1]["players"][0] == "l" and len(patch[1]["players"][1]) == 1     # 낙찰된 선수 한 명만
    assert patch[1]["teams"][0] == "l"
    assert "players" not in t_obs.pump()[-1]["args"][0]["p"][1]                   # 공용 화면에는 선수 전체 목록이 없다
    t_host.assert_in_sync(), t_obs.assert_in_sync()


def test_large_change_falls_back_to_full_state(env, monkeypatch):
    monkeypatch.setattr(socket_events, "PATCH_MAX_RATIO", 0.0)
    created, host, leaders, obs = setup_room(env)
    tracker = Tracker(obs)
    tracker.pump()
    before = tracker.view["seq"]
    assert host.emit("host_start", {}, callback=True)["ok"]
    received = tracker.pump()
    assert [e["name"] for e in received] == ["state"]
    assert received[0]["args"][0]["seq"] == before + 1 and tracker.view["state"] == "MAIN_AUCTION"
    tracker.assert_in_sync()


def test_timer_sends_pending_change_before_the_heartbeat(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    tracker = Tracker(obs)
    tracker.pump()
    handle = env.manager.get(created["room_id"])
    env.manager.run(handle, lambda room, now, rng: engine.pause(room, now))      # 방송 없이 상태만 바꾼다
    env.app.api.emit_timer(handle)
    names = [e["name"] for e in obs.get_received() if e["name"] in ("state", "patch", "timer")]
    assert names == ["patch", "timer"]


def test_skipped_patch_is_detected_by_sequence(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    obs.get_received()
    view = snap(obs)
    seq = view["lot"]["seq"]
    assert leaders[0].emit("bid", {"amount": 10, "lot_seq": seq}, callback=True)["ok"]
    assert leaders[1].emit("bid", {"amount": 20, "lot_seq": seq}, callback=True)["ok"]
    events = [e for e in obs.get_received() if e["name"] == "patch"]
    assert len(events) == 2
    assert apply_events(view, events[1:]) is None              # 하나를 건너뛰면 적용하지 않는다 (재동기화 대상)
    assert apply_events(view, events[:1])["lot"]["highest_bid"] == 10
    assert apply_events(view, events)["lot"]["highest_bid"] == 20


def test_room_delete_forgets_broadcast_state(env):
    created, host, leaders, obs = setup_room(env)
    rid = created["room_id"]
    assert rid in env.app.api._broadcasts
    assert host.emit("host_delete_room", {"confirm": "DELETE"}, callback=True)["ok"]
    assert rid not in env.app.api._broadcasts


def test_patch_never_carries_fields_the_public_view_lacks(env):
    """주최자 전용 정보(전체 선수 목록, 대기 순서, 핸디캡)가 공용 변경분에 섞여 나가지 않는다."""
    created, host, leaders, obs = setup_room(env, n=3, m=3, settings={"base_bid_time_sec": 5, "extension_time_sec": 0})
    rid = created["room_id"]
    obs.get_received()
    assert host.emit("host_start", {}, callback=True)["ok"]
    for _ in range(8):
        st = snap(obs)
        if not st["lot"]:
            break
        leaders[st["lot"]["seq"] % 3].emit("bid", {"amount": st["lot"]["min_next_bid"], "lot_seq": st["lot"]["seq"]}, callback=True)
        env.tick(rid, 6)
    blob = json.dumps([e["args"][0] for e in obs.get_received() if e["name"] in ("state", "patch")], ensure_ascii=False)
    for secret in ("main_ids", "unsold_ids", "leftover_ids", "handicap", "required_players", '"uid"'):
        assert secret not in blob, secret
