"""서버 통합 테스트: HTTP API + Socket.IO 이벤트 + 보안 동작.

시간은 가짜 시계로 제어하고, 틱 루프 대신 manager.tick() 을 직접 호출한다.
"""
import json
import os
import random
import stat

import pytest

from server import engine
from server.app_config import AppConfig
from server.app_factory import create_app
from server.manager import RoomManager
from server.models import AuctionState
from server.storage import RoomStore

ADMIN = "adminkey-for-tests"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.cfg = AppConfig(data_dir=str(tmp_path / "data"), admin_key=ADMIN)
        self.manager = RoomManager(RoomStore(self.cfg.data_dir), clock=self.clock, rng=random.Random(7))
        self.app = create_app(self.cfg, manager=self.manager)
        self.http = self.app.flask.test_client()

    def client(self):
        return self.app.socketio.test_client(self.app.flask)

    def create(self, n=3, m=3, title="테스트 방"):
        r = self.http.post("/api/rooms", json={"admin_key": ADMIN, "title": title,
                                                "num_teams": n, "members_per_team": m})
        assert r.status_code == 201, r.get_json()
        return r.get_json()

    def join(self, client, room_id, token, **extra):
        return client.emit("join", {"room_id": room_id, "token": token, **extra}, callback=True)

    def tick(self, room_id, sec):
        self.clock.advance(sec)
        handle = self.manager.get(room_id)
        if self.manager.tick(handle):
            self.app.api.emit_state(handle)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def players_payload(count, prefix="선수"):
    return [{"name": f"{prefix}{i+1}", "role": "damage", "elo": 1500 + i, "intro": f"소개 {i+1}"}
            for i in range(count)]


def teams_payload(n):
    return [{"leader_name": f"팀장{i+1}", "leader_position": "TOP", "handicap": 0} for i in range(n)]


def setup_room(env, n=3, m=3, settings=None):
    """방 생성 + 주최자/팀장/관전자 접속 + 설정 + 매물 등록까지."""
    created = env.create(n, m)
    rid, tok = created["room_id"], created["tokens"]
    host = env.client()
    ack = env.join(host, rid, tok["host"])
    assert ack["ok"] and ack["role"] == "host"
    leaders = []
    for i in range(n):
        c = env.client()
        a = env.join(c, rid, tok["leaders"][i])
        assert a["ok"] and a["role"] == "leader" and a["team_id"] == i
        leaders.append(c)
    obs = env.client()
    a = env.join(obs, rid, tok["observer"], nickname="구경꾼")
    assert a["ok"] and a["role"] == "observer"
    payload = {"teams": teams_payload(n), "players": players_payload(n * (m - 1))}
    s = {"base_points": 1000, "base_bid_time_sec": 20, "extension_time_sec": 5,
         "fixed_min_increment": 10}
    s.update(settings or {})
    payload["settings"] = s
    ack = host.emit("host_setup", payload, callback=True)
    assert ack["ok"], ack
    return created, host, leaders, obs


def snap(client):
    """현재 스냅샷을 직접 요청한다 (브로드캐스트가 없었던 경우에도 사용)."""
    return client.emit("sync", {}, callback=True)["snapshot"]


def last_state(client):
    states = [e["args"][0] for e in client.get_received() if e["name"] == "state"]
    return states[-1] if states else None


# =============================================================================
# HTTP / 헤더 / 방 생성
# =============================================================================

def test_create_room_requires_admin_key(env):
    base = {"title": "x", "num_teams": 3, "members_per_team": 3}
    assert env.http.post("/api/rooms", json={**base, "admin_key": "wrong"}).status_code == 403
    assert env.http.post("/api/rooms", json=base).status_code == 403
    assert env.http.post("/api/rooms", data="not json").status_code == 400
    assert env.http.post("/api/rooms", json=[1, 2]).status_code == 400


def test_create_room_rejects_cross_origin(env):
    r = env.http.post("/api/rooms", json={"admin_key": ADMIN, "title": "x", "num_teams": 3, "members_per_team": 3},
                      headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_create_room_validates_inputs(env):
    def post(**kw):
        body = {"admin_key": ADMIN, "title": "t", "num_teams": 3, "members_per_team": 3, **kw}
        return env.http.post("/api/rooms", json=body)
    assert post(num_teams=1).status_code == 400
    assert post(num_teams=11).status_code == 400
    assert post(num_teams="3").status_code == 400
    assert post(num_teams=True).status_code == 400
    assert post(members_per_team=1).status_code == 400
    assert post(title="").status_code == 400
    assert post(title="가" * 41).status_code == 400
    assert post().status_code == 201


def test_admin_key_bruteforce_is_locked_out(env):
    codes = []
    for _ in range(14):
        codes.append(env.http.post("/api/rooms", json={"admin_key": "nope", "title": "t",
                                                       "num_teams": 3, "members_per_team": 3}).status_code)
    assert 429 in codes
    # 잠금 중에는 올바른 키도 거부
    assert env.http.post("/api/rooms", json={"admin_key": ADMIN, "title": "t",
                                             "num_teams": 3, "members_per_team": 3}).status_code == 429


def test_security_headers_present(env):
    r = env.http.get("/")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    s = env.http.get("/static/css/style.css")
    assert s.status_code == 200 and s.headers["X-Content-Type-Options"] == "nosniff"


def test_malicious_host_header_does_not_enter_csp(env):
    r = env.http.get("/", headers={"Host": "evil.com; script-src *"})
    assert "script-src *" not in r.headers["Content-Security-Policy"]


def test_room_page_route_validates_id(env):
    assert env.http.get("/r/abcdefgh").status_code == 200
    assert env.http.get("/r/..%2f..%2fetc").status_code == 404
    assert env.http.get("/r/a").status_code == 404
    assert env.http.get("/r/" + "a" * 40).status_code == 404


def test_error_responses_do_not_leak_internals(env):
    r = env.http.get("/nope")
    assert r.status_code == 404 and "Traceback" not in r.get_data(as_text=True)
    assert env.http.put("/api/rooms").status_code == 405


def test_server_info(env):
    r = env.http.get("/api/server-info").get_json()
    assert r["ok"] and r["urls"] and r["limits"]["max_teams"] == 10


# =============================================================================
# 접속/권한
# =============================================================================

def test_join_roles_and_generic_failure(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]

    wrong_token = env.client()
    a1 = env.join(wrong_token, rid, "x" * 32)
    wrong_room = env.client()
    a2 = env.join(wrong_room, "nonexist", tok["host"])
    malformed = env.client()
    a3 = env.join(malformed, rid, "<script>")
    assert a1 == a2 == a3                       # 실패 이유를 구분해 알려주지 않는다
    assert a1["ok"] is False and a1["code"] == "auth"

    ok = env.client()
    assert env.join(ok, rid, tok["host"])["role"] == "host"


def test_events_before_join_are_forbidden(env):
    c = env.client()
    assert c.emit("bid", {"amount": 10, "lot_seq": 1}, callback=True)["code"] == "forbidden"
    assert c.emit("host_start", {}, callback=True)["code"] == "forbidden"
    assert c.emit("chat", {"text": "hi"}, callback=True)["code"] == "forbidden"
    assert c.emit("sync", {}, callback=True)["code"] == "forbidden"


def test_join_bruteforce_lockout(env):
    rid = env.create()["room_id"]
    results = []
    for _ in range(25):
        c = env.client()
        results.append(env.join(c, rid, "a" * 32)["code"])
    assert "blocked" in results


def test_observer_and_leader_cannot_use_host_events(env):
    created, host, leaders, obs = setup_room(env)
    for client in (obs, leaders[0]):
        for ev in ("host_start", "host_pause", "host_resume", "host_force_sold", "host_force_unsold",
                   "host_pick", "host_setup", "host_reissue", "host_delete_room"):
            ack = client.emit(ev, {}, callback=True)
            assert ack["code"] == "forbidden", ev


def test_observer_cannot_bid(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    assert obs.emit("bid", {"amount": 10, "lot_seq": 1}, callback=True)["code"] == "forbidden"
    assert host.emit("bid", {"amount": 10, "lot_seq": 1}, callback=True)["code"] == "forbidden"


def test_non_dict_payload_rejected(env):
    created, host, leaders, obs = setup_room(env)
    assert leaders[0].emit("bid", "garbage", callback=True)["code"] == "bad_request"
    assert leaders[0].emit("bid", [1, 2], callback=True)["code"] == "bad_request"


# =============================================================================
# 경매 흐름
# =============================================================================

def test_full_auction_flow(env):
    created, host, leaders, obs = setup_room(env, n=3, m=2)     # 팀당 매물 1명, 총 3명
    rid = created["room_id"]
    assert host.emit("host_start", {}, callback=True)["ok"]
    st = last_state(obs)
    assert st["state"] == "MAIN_AUCTION" and st["lot"]["seq"] == 1
    seq = st["lot"]["seq"]

    # 팀장0이 입찰, 팀장1이 가로채기
    assert leaders[0].emit("bid", {"amount": 100, "lot_seq": seq}, callback=True)["ok"]
    r = leaders[0].emit("bid", {"amount": 200, "lot_seq": seq}, callback=True)
    assert r["code"] == "already_highest"
    r = leaders[1].emit("bid", {"amount": 105, "lot_seq": seq}, callback=True)
    assert r["code"] == "bid_too_low"
    assert leaders[1].emit("bid", {"amount": 110, "lot_seq": seq}, callback=True)["ok"]
    st = last_state(obs)
    assert st["lot"]["highest_bid"] == 110 and st["lot"]["highest_team"] == 1

    # 시간 경과 -> 낙찰 (기본 20초, 남은 시간이 연장(5초)보다 길면 연장되지 않는다)
    env.tick(rid, 4)
    assert snap(obs)["lot"]["highest_team"] == 1                 # 아직 진행 중
    env.tick(rid, 17)
    st = last_state(obs)
    assert st["teams"][1]["points"] == 890
    assert len(st["teams"][1]["roster"]) == 1

    # 이전 매물에 대한 입찰은 거부
    r = leaders[2].emit("bid", {"amount": 50, "lot_seq": seq}, callback=True)
    assert r["code"] == "stale_lot"


def test_client_supplied_team_id_is_ignored(env):
    created, host, leaders, obs = setup_room(env)
    host.emit("host_start", {}, callback=True)
    seq = last_state(obs)["lot"]["seq"]
    # 팀장2 가 team_id=0 을 사칭해도 서버는 토큰으로 정한 팀(2)으로 처리
    r = leaders[2].emit("bid", {"amount": 10, "lot_seq": seq, "team_id": 0, "team": 0}, callback=True)
    assert r["ok"]
    assert last_state(obs)["lot"]["highest_team"] == 2


def test_bid_amount_type_confusion(env):
    created, host, leaders, obs = setup_room(env)
    host.emit("host_start", {}, callback=True)
    seq = last_state(obs)["lot"]["seq"]
    for bad in ("10", 10.5, None, True, [10], {"a": 1}, 10**30):
        r = leaders[0].emit("bid", {"amount": bad, "lot_seq": seq}, callback=True)
        assert r["ok"] is False, bad


def test_pause_resume_force_controls(env):
    created, host, leaders, obs = setup_room(env)
    rid = created["room_id"]
    host.emit("host_start", {}, callback=True)
    seq = last_state(obs)["lot"]["seq"]
    assert host.emit("host_pause", {}, callback=True)["ok"]
    assert last_state(obs)["paused"] is True
    assert leaders[0].emit("bid", {"amount": 10, "lot_seq": seq}, callback=True)["code"] == "paused"
    env.tick(rid, 500)                                # 멈춘 동안은 만료되지 않는다
    assert snap(obs)["lot"]["seq"] == seq
    assert host.emit("host_resume", {}, callback=True)["ok"]
    assert leaders[0].emit("bid", {"amount": 10, "lot_seq": seq}, callback=True)["ok"]
    assert host.emit("host_force_sold", {}, callback=True)["ok"]
    st = last_state(obs)
    assert st["teams"][0]["points"] == 990
    # 입찰자가 없을 때 강제 낙찰은 오류
    assert host.emit("host_force_sold", {}, callback=True)["code"] == "no_bid"
    assert host.emit("host_force_unsold", {}, callback=True)["ok"]


def test_setup_locked_after_start_and_validated(env):
    created, host, leaders, obs = setup_room(env)
    bad = host.emit("host_setup", {"settings": {"base_bid_time_sec": 1}}, callback=True)
    assert bad["ok"] is False
    bad = host.emit("host_setup", {"settings": {"evil": 1}}, callback=True)
    assert bad["ok"] is False
    bad = host.emit("host_setup", {"unknown": 1}, callback=True)
    assert bad["ok"] is False
    bad = host.emit("host_setup", {"players": [{"name": ""}]}, callback=True)
    assert bad["ok"] is False
    assert host.emit("host_start", {}, callback=True)["ok"]
    late = host.emit("host_setup", {"settings": {"base_points": 5}}, callback=True)
    assert late["code"] == "bad_state"


def test_start_requires_exact_player_count(env):
    created = env.create(3, 3)
    host = env.client()
    env.join(host, created["room_id"], created["tokens"]["host"])
    host.emit("host_setup", {"players": players_payload(2)}, callback=True)
    r = host.emit("host_start", {}, callback=True)
    assert r["code"] == "player_count"


def test_bracket_flow_through_sockets(env):
    created, host, leaders, obs = setup_room(env, n=3, m=2, settings={"base_bid_time_sec": 5, "extension_time_sec": 0})
    rid = created["room_id"]
    host.emit("host_start", {}, callback=True)
    # 팀0 → 100P, 팀1 → 300P, 팀2 → 입찰 없이 자동 배정
    for team, amount in ((0, 100), (1, 300)):
        seq = last_state(obs)["lot"]["seq"]
        assert leaders[team].emit("bid", {"amount": amount, "lot_seq": seq}, callback=True)["ok"]
        env.tick(rid, 6)
    st = last_state(obs)
    assert st["state"] == "BRACKET_SELECT"
    assert st["bracket"]["order"] == [2, 0, 1]                  # 잔여: 팀2=1000, 팀0=900, 팀1=700
    assert st["bracket"]["picker"] == 2
    # 차례가 아닌 팀장은 지명 불가
    assert leaders[0].emit("pick_opponent", {"opponent": 1}, callback=True)["code"] == "not_your_turn"
    assert leaders[2].emit("pick_opponent", {"opponent": 1}, callback=True)["ok"]
    st = last_state(obs)
    assert st["state"] == "COMPLETED"
    assert st["bracket"]["pairs"] == [[2, 1]] and st["bracket"]["byes"] == [0]


def test_host_can_pick_for_absent_leader(env):
    created, host, leaders, obs = setup_room(env, n=3, m=2, settings={"base_bid_time_sec": 5, "extension_time_sec": 0})
    rid = created["room_id"]
    host.emit("host_start", {}, callback=True)
    for _ in range(3):
        env.tick(rid, 6)        # 전부 유찰 -> 재경매 -> 전부 유찰 -> 자동 배정
    for _ in range(6):
        env.tick(rid, 6)
    st = last_state(obs)
    assert st["state"] == "BRACKET_SELECT"
    assert host.emit("host_pick", {"opponent": st["bracket"]["order"][1]}, callback=True)["ok"]
    assert last_state(obs)["state"] == "COMPLETED"


# =============================================================================
# 개인정보: 역할별 공개 범위
# =============================================================================

def test_public_view_hides_player_details_before_start_and_in_blind_mode(env):
    created, host, leaders, obs = setup_room(env)
    pub = last_state(obs)
    host_state = last_state(host)
    assert "players" not in pub
    assert pub["queue"]["registered"] == 6 and pub["queue"]["upcoming"] is None
    dump = json.dumps(pub, ensure_ascii=False)
    assert "선수1" not in dump and "소개 1" not in dump
    assert len(host_state["players"]) == 6 and host_state["players"][0]["intro"] == "소개 1"

    host.emit("host_start", {}, callback=True)
    pub = last_state(obs)
    lot_name = pub["lot"]["player"]["name"]
    dump = json.dumps(pub, ensure_ascii=False)
    # 현재 매물만 보인다 (BLIND)
    others = [f"선수{i}" for i in range(1, 7) if f"선수{i}" != lot_name]
    assert all(name not in dump for name in others)
    assert pub["queue"]["upcoming"] is None and pub["queue"]["main_count"] == 5
    assert "main_ids" not in pub["queue"]
    assert host_state is not None


def test_public_order_mode_shows_names_but_not_intro_or_elo(env):
    created, host, leaders, obs = setup_room(env, settings={"order_public": True})
    host.emit("host_start", {}, callback=True)
    pub = last_state(obs)
    assert len(pub["queue"]["upcoming"]) == 5
    for item in pub["queue"]["upcoming"]:
        assert set(item) == {"id", "name", "role"}


def test_no_token_material_in_any_snapshot(env):
    created, host, leaders, obs = setup_room(env)
    host.emit("host_start", {}, callback=True)
    toks = [created["tokens"]["host"], created["tokens"]["observer"], *created["tokens"]["leaders"]]
    for client in (host, leaders[0], obs):
        env_dump = json.dumps(client.emit("sync", {}, callback=True), ensure_ascii=False)
        for t in toks:
            assert t not in env_dump
        assert "token" not in env_dump and "hash" not in env_dump


# =============================================================================
# 링크 재발급 / 채팅 / 삭제
# =============================================================================

def test_reissue_leader_token_invalidates_old_and_kicks(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    ack = host.emit("host_reissue", {"target": "leader", "team_id": 1}, callback=True)
    assert ack["ok"] and ack["token"] and ack["token"] != tok["leaders"][1]
    assert leaders[1].is_connected() is False                # 기존 링크 접속자는 즉시 끊긴다
    assert leaders[0].is_connected() is True
    fresh = env.client()
    assert env.join(fresh, rid, tok["leaders"][1])["ok"] is False          # 옛 링크 무효
    fresh2 = env.client()
    a = env.join(fresh2, rid, ack["token"])
    assert a["ok"] and a["team_id"] == 1                                    # 새 링크 유효
    # 다른 팀 링크는 영향 없음
    other = env.client()
    assert env.join(other, rid, tok["leaders"][0])["ok"]


def test_reissue_observer_token(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    ack = host.emit("host_reissue", {"target": "observer"}, callback=True)
    assert ack["ok"]
    assert env.join(env.client(), rid, tok["observer"])["ok"] is False
    assert env.join(env.client(), rid, ack["token"])["ok"] is True
    assert host.emit("host_reissue", {"target": "weird"}, callback=True)["ok"] is False
    assert host.emit("host_reissue", {"target": "leader", "team_id": 99}, callback=True)["ok"] is False
    assert host.emit("host_reissue", {"target": "leader", "team_id": "1"}, callback=True)["ok"] is False


def test_chat_is_sanitized_limited_and_rate_limited(env):
    created, host, leaders, obs = setup_room(env)
    obs.get_received()
    evil = "<img src=x onerror=alert(1)>‮\x00hello"
    assert leaders[0].emit("chat", {"text": evil}, callback=True)["ok"]
    msgs = [e["args"][0] for e in obs.get_received() if e["name"] == "chat"]
    assert msgs and "\x00" not in msgs[0]["text"] and "‮" not in msgs[0]["text"]
    assert msgs[0]["name"] == "팀장1" and msgs[0]["role"] == "leader"     # 이름은 서버가 결정
    assert leaders[0].emit("chat", {"text": "x" * 201}, callback=True)["ok"] is False
    assert leaders[0].emit("chat", {"text": "   "}, callback=True)["ok"] is False
    assert leaders[0].emit("chat", {"text": 123}, callback=True)["ok"] is False
    codes = [leaders[1].emit("chat", {"text": f"m{i}"}, callback=True).get("code") for i in range(10)]
    assert "rate_limited" in codes


def test_bid_spam_is_rate_limited(env):
    created, host, leaders, obs = setup_room(env)
    host.emit("host_start", {}, callback=True)
    seq = last_state(obs)["lot"]["seq"]
    codes = [leaders[0].emit("bid", {"amount": 10, "lot_seq": seq}, callback=True).get("code") for _ in range(30)]
    assert "rate_limited" in codes


def test_delete_room_requires_confirmation_and_removes_data(env):
    created, host, leaders, obs = setup_room(env)
    rid = created["room_id"]
    path = env.manager.store.rooms_dir / f"{rid}.json"
    assert path.exists()
    assert host.emit("host_delete_room", {}, callback=True)["code"] == "confirm"
    assert host.emit("host_delete_room", {"confirm": "delete"}, callback=True)["code"] == "confirm"
    assert host.emit("host_delete_room", {"confirm": "DELETE"}, callback=True)["ok"]
    assert not path.exists()
    assert env.manager.get(rid) is None
    assert env.join(env.client(), rid, created["tokens"]["host"])["ok"] is False


# =============================================================================
# 저장/복구/파일 보안
# =============================================================================

def test_saved_file_has_no_raw_tokens_and_is_private(env):
    created, host, leaders, obs = setup_room(env)
    rid = created["room_id"]
    path = env.manager.store.rooms_dir / f"{rid}.json"
    raw = path.read_text(encoding="utf-8")
    for t in [created["tokens"]["host"], created["tokens"]["observer"], *created["tokens"]["leaders"]]:
        assert t not in raw
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(env.manager.store.rooms_dir.stat().st_mode) == 0o700


def test_restart_recovers_room_paused_and_tokens_still_work(env, tmp_path):
    created, host, leaders, obs = setup_room(env)
    rid = created["room_id"]
    host.emit("host_start", {}, callback=True)
    seq = last_state(obs)["lot"]["seq"]
    leaders[0].emit("bid", {"amount": 70, "lot_seq": seq}, callback=True)
    env.clock.advance(3)

    # 서버 재시작 시뮬레이션: 새 매니저가 디스크에서 로드
    manager2 = RoomManager(RoomStore(env.cfg.data_dir), clock=env.clock, rng=random.Random(9))
    assert manager2.load_from_disk() == 1
    room = manager2.get(rid).room
    assert room.paused is True and room.state == AuctionState.MAIN_AUCTION
    assert room.highest_bid == 70 and room.highest_team == 0
    assert manager2.authenticate(rid, created["tokens"]["leaders"][2]).team_id == 2
    assert manager2.authenticate(rid, created["tokens"]["host"]).role == "host"
    assert manager2.authenticate(rid, "z" * 32) is None
    # 재개하면 이어서 진행된다
    changed, err, _ = manager2.run(manager2.get(rid), lambda r, now, rng: engine.resume(r, now))
    assert changed and err is None and room.paused is False


def test_corrupt_file_is_skipped(env):
    created = env.create()
    (env.manager.store.rooms_dir / "badbadbad.json").write_text("{not json", encoding="utf-8")
    m2 = RoomManager(RoomStore(env.cfg.data_dir), clock=env.clock)
    assert m2.load_from_disk() == 1


def test_storage_rejects_path_traversal(env):
    with pytest.raises(ValueError):
        env.manager.store.save("../../evil", {})
    with pytest.raises(ValueError):
        env.manager.store.delete("..")


def test_retention_purge(env):
    created = env.create()
    rid = created["room_id"]
    path = env.manager.store.rooms_dir / f"{rid}.json"
    old = path.stat().st_mtime - 40 * 86400
    os.utime(path, (old, old))
    env.manager.retention_days = 14
    assert env.manager.purge_expired() == [rid]
    assert env.manager.get(rid) is None and not path.exists()


def test_room_count_limit(env):
    env.manager.max_rooms = 2
    env.create()
    env.create()
    r = env.http.post("/api/rooms", json={"admin_key": ADMIN, "title": "t", "num_teams": 3, "members_per_team": 3})
    assert r.status_code == 409


def test_observer_cannot_impersonate_host_or_leader_in_chat(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    for fake in ("주최자", "팀장1", "팀장1".upper()):
        c = env.client()
        ack = env.join(c, rid, tok["observer"], nickname=fake)
        assert ack["ok"] and ack["nickname"].endswith("(관전)"), fake
        c.get_received()
        c.emit("chat", {"text": "hi"}, callback=True)
        msg = [e["args"][0] for e in c.get_received() if e["name"] == "chat"][0]
        assert msg["role"] == "observer"          # 역할은 서버가 정한다
    plain = env.client()
    assert env.join(plain, rid, tok["observer"], nickname="철수")["nickname"] == "철수"
