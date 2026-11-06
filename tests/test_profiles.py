"""선수 프로필(배틀태그/포지션/영웅/이미지)과 영구 명부, 이미지 API 권한 테스트."""
import io
import json

import pytest
from PIL import Image

from server import config, heroes
from server.models import Player
from server.validators import (
    ValidationError, parse_battletag, parse_hero_ids, parse_image_ids, parse_player_entries,
)

from tests.test_server_flow import (  # noqa: F401  (fixture 재사용)
    ADMIN, Env, env, players_payload, setup_room, snap, teams_payload,
)


def png_bytes(size=(64, 48), color=(200, 30, 30), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def upload(env, rid, token, data=None, **kw):
    return env.http.post(f"/api/rooms/{rid}/images", data=data if data is not None else png_bytes(),
                         headers={**bearer(token), "Content-Type": "application/octet-stream"}, **kw)


def keep_uids(host, players):
    """화면(클라이언트)이 하듯이, 이미 저장된 선수에는 서버가 준 uid 를 그대로 실어 보낸다."""
    current = snap(host)["players"]
    return [{**p, "uid": current[i]["uid"]} for i, p in enumerate(players)]


def entry(i, **over):
    base = {"name": f"선수{i}", "role": "tank", "elo": 1500, "intro": "", "battletag": "",
            "heroes": [], "images": []}
    base.update(over)
    return base


# =============================================================================
# 검증기
# =============================================================================

@pytest.mark.parametrize("tag", ["Player#1234", "홍길동#12345678", "Abc#0000", "Ünïcode#9999"])
def test_battletag_valid(tag):
    assert parse_battletag(tag) == tag


@pytest.mark.parametrize("tag", [
    "1abc#1234", "ab#1234", "abc#123", "abc#123456789", "abc_d#1234", "abc#12a4", "abc 1#1234",
    "a" * 13 + "#1234", "abc", "#1234", "abc#", "<b>x</b>#1234", "abc#１２３４",
])
def test_battletag_invalid(tag):
    with pytest.raises(ValidationError):
        parse_battletag(tag)


def test_battletag_optional_and_type_checked():
    assert parse_battletag("") == ""
    assert parse_battletag(None) == ""
    with pytest.raises(ValidationError):
        parse_battletag(1234)


def test_hero_ids_rules():
    assert parse_hero_ids(["tracer", "mercy"]) == ["tracer", "mercy"]
    assert parse_hero_ids(None) == []
    with pytest.raises(ValidationError):
        parse_hero_ids(["tracer"] * 2)                       # 중복
    with pytest.raises(ValidationError):
        parse_hero_ids(["tracer", "mercy", "ana", "zarya", "sigma", "mei"])   # 6개
    with pytest.raises(ValidationError):
        parse_hero_ids(["not-a-hero"])
    with pytest.raises(ValidationError):
        parse_hero_ids("tracer")
    with pytest.raises(ValidationError):
        parse_hero_ids([["tracer"]])
    assert len(parse_hero_ids([h[0] for h in heroes.HEROES[:5]])) == 5


def test_image_ids_rules():
    ok = "A" * 16
    assert parse_image_ids([ok]) == [ok]
    assert parse_image_ids([]) == []
    with pytest.raises(ValidationError):
        parse_image_ids([ok, "B" * 16, "C" * 16])            # 3장
    with pytest.raises(ValidationError):
        parse_image_ids([ok, ok])
    with pytest.raises(ValidationError):
        parse_image_ids(["../../etc/passwd"])
    with pytest.raises(ValidationError):
        parse_image_ids(["short"])


def test_player_entries_require_role_and_reject_duplicates():
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, role="")])
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, role="mid")])
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, battletag="Abc#1234"), entry(2, battletag="abc#1234")])
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, uid="a" * 16), entry(2, uid="a" * 16)])
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, uid="NOT-HEX")])
    out = parse_player_entries([entry(1, battletag="Abc#1234", heroes=["tracer"])])
    assert out[0]["uid"] == "" and out[0]["roles"] == ["tank"]


def test_player_from_dict_tolerates_old_and_extra_keys():
    old = {"id": 0, "name": "옛선수", "position": "MID", "elo": 1, "intro": "", "winning_bid": 0,
           "assigned_team_id": -1, "future_field": 1}
    p = Player.from_dict(old)
    assert p.name == "옛선수" and p.roles == [] and p.images == [] and p.heroes == []


# =============================================================================
# 이미지 저장소
# =============================================================================

def test_image_is_reencoded_and_metadata_stripped(env):
    buf = io.BytesIO()
    img = Image.new("RGB", (3200, 2400), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    img.save(buf, "JPEG", exif=exif)
    iid = env.manager.images.save_upload(buf.getvalue())
    path = env.manager.images.path_for(iid)
    data = path.read_bytes()
    assert b"SecretCameraMaker" not in data
    out = Image.open(path)
    assert out.format == "WEBP" and max(out.size) == config.IMAGE_MAX_SIDE
    assert not out.getexif()
    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("payload", [
    b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    b"<html><script>alert(1)</script></html>",
    b"MZ" + b"\x00" * 200,
    b"GIF89a" + b"\x00" * 5,
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
    b"",
])
def test_non_images_are_rejected(env, payload):
    from server.images import ImageError
    with pytest.raises(ImageError):
        env.manager.images.save_upload(payload)


def test_pixel_bomb_is_rejected(env, monkeypatch):
    from server.images import ImageError
    monkeypatch.setattr(config, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ImageError):
        env.manager.images.save_upload(png_bytes((100, 100)))


# =============================================================================
# 이미지 업로드/조회 HTTP API
# =============================================================================

def test_upload_requires_host_token(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    assert upload(env, rid, tok["host"]).status_code == 201
    assert upload(env, rid, tok["leaders"][0]).status_code == 403
    assert upload(env, rid, tok["observer"]).status_code == 403
    assert upload(env, rid, "x" * 32).status_code == 403
    assert env.http.post(f"/api/rooms/{rid}/images", data=png_bytes()).status_code == 403   # 헤더 없음
    assert upload(env, "nonexist1", tok["host"]).status_code == 403                         # 다른 방
    # 토큰을 쿼리스트링으로 보내는 방식은 지원하지 않는다
    r = env.http.post(f"/api/rooms/{rid}/images?t={tok['host']}", data=png_bytes())
    assert r.status_code == 403


def test_upload_rejects_cross_origin_and_garbage_and_huge(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    r = env.http.post(f"/api/rooms/{rid}/images", data=png_bytes(),
                      headers={**bearer(tok["host"]), "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert upload(env, rid, tok["host"], data=b"<script>alert(1)</script>").status_code == 400
    big = b"\x00" * (config.MAX_UPLOAD_BYTES + 1)
    assert upload(env, rid, tok["host"], data=big).status_code == 413
    # 일반 JSON API 의 64KB 제한은 그대로다
    big_json = {"admin_key": "x" * 70000, "title": "t", "num_teams": 3, "members_per_team": 3}
    assert env.http.post("/api/rooms", json=big_json).status_code == 413


def test_upload_rate_limited(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    codes = [upload(env, rid, tok["host"]).status_code for _ in range(12)]
    assert codes.count(201) == 8 and 429 in codes


def test_image_visibility_per_role(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    ids = [upload(env, rid, tok["host"]).get_json()["id"] for _ in range(4)]
    players = players_payload(6)
    players = [{**p, "role": "support"} for p in players]
    players[0]["images"] = ids[:2]
    players[1]["images"] = ids[2:3]
    ack = host.emit("host_setup", {"players": players}, callback=True)
    assert ack["ok"], ack

    def get(token, iid):
        return env.http.get(f"/api/rooms/{rid}/images/{iid}", headers=bearer(token)).status_code

    # 경매 전: 주최자만 볼 수 있다
    for iid in ids[:3]:
        assert get(tok["host"], iid) == 200
        assert get(tok["leaders"][0], iid) == 404
        assert get(tok["observer"], iid) == 404

    assert host.emit("host_start", {}, callback=True)["ok"]
    state = snap(obs)
    lot_player = state["lot"]["player"]
    lot_images = lot_player["images"]
    all_images = {iid for p in snap(host)["players"] for iid in p["images"]}
    # 현재 매물의 이미지는 팀장·관전자·주최자 모두 받는다. 대기 중 선수의 이미지는 못 받는다.
    for iid in all_images:
        expected_public = 200 if iid in lot_images else 404
        assert get(tok["leaders"][1], iid) == expected_public
        assert get(tok["observer"], iid) == expected_public
        assert get(tok["host"], iid) == 200
    # 공개 스냅샷에는 다른 선수의 이미지 id 가 아예 없다
    pub_text = json.dumps(state)
    for iid in all_images - set(lot_images):
        assert iid not in pub_text


def test_image_endpoint_rejects_bad_ids(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    for bad in ["..", "%2e%2e", "a" * 15, "a" * 17, "../rooms/x"]:
        r = env.http.get(f"/api/rooms/{rid}/images/{bad}", headers=bearer(tok["host"]))
        assert r.status_code in (404, 405)


def test_image_response_headers(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"]).get_json()["id"]
    r = env.http.get(f"/api/rooms/{rid}/images/{iid}", headers=bearer(tok["host"]))
    assert r.status_code == 200 and r.mimetype == "image/webp"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "blob:" in r.headers["Content-Security-Policy"]


# =============================================================================
# host_setup: 새 필드, 이미지 확인, 영구 명부
# =============================================================================

def test_setup_saves_profile_fields_and_exposes_them(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"]).get_json()["id"]
    players = players_payload(6)
    players[0].update(battletag="Hero#1234", roles=["support"], heroes=["mercy", "ana"], images=[iid])
    players[1].update(roles=["tank", "support"])
    assert host.emit("host_setup", {"players": players}, callback=True)["ok"]
    hv = snap(host)["players"][0]
    assert hv["battletag"] == "Hero#1234" and hv["roles"] == ["support"]
    assert snap(host)["players"][1]["roles"] == ["tank", "support"]
    assert hv["heroes"] == ["mercy", "ana"] and hv["images"] == [iid] and len(hv["uid"]) == 16
    assert "position" not in hv


def test_setup_rejects_unknown_image_and_bad_fields(env):
    created, host, *_ = setup_room(env)
    base = players_payload(6)
    for patch in [{"images": ["A" * 16]}, {"heroes": ["nope"]}, {"battletag": "bad"}, {"role": "mid"}]:
        players = [dict(p) for p in base]
        players[0].update(patch)
        ack = host.emit("host_setup", {"players": players}, callback=True)
        assert not ack["ok"], patch


def test_roster_is_permanent_across_room_delete_and_restart(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"]).get_json()["id"]
    players = players_payload(6)
    players[0].update(battletag="Hero#1234", role="support", heroes=["mercy"], images=[iid])
    assert host.emit("host_setup", {"players": keep_uids(host, players)}, callback=True)["ok"]

    roster = host.emit("host_roster_list", {}, callback=True)["players"]
    assert len(roster) == 6
    saved = next(p for p in roster if p["battletag"] == "Hero#1234")
    assert saved["images"] == [iid] and saved["heroes"] == ["mercy"]

    # 방을 삭제해도 명부와 이미지는 남는다
    assert host.emit("host_delete_room", {"confirm": "DELETE"}, callback=True)["ok"]
    assert env.manager.get(rid) is None
    assert env.manager.images.exists(iid)

    # 서버를 다시 켜도 남는다 (같은 data 폴더로 새 매니저)
    from server.manager import RoomManager
    from server.storage import RoomStore
    again = RoomManager(RoomStore(env.cfg.data_dir))
    assert any(p["uid"] == saved["uid"] for p in again.roster.list())
    assert again.images.exists(iid)
    import stat
    assert (again.roster.path.stat().st_mode & 0o777) == 0o600


def test_roster_load_into_new_room_keeps_uid(env):
    created, host, *_ = setup_room(env)
    uid_before = {p["uid"] for p in snap(host)["players"]}
    roster = host.emit("host_roster_list", {}, callback=True)["players"]
    assert {p["uid"] for p in roster} == uid_before

    # 명부의 선수를 그대로 다시 저장하면 같은 uid 로 갱신된다 (중복 생성 안 됨)
    players = [{k: p[k] for k in ("uid", "name", "battletag", "roles", "heroes", "images", "elo", "intro")}
               for p in roster]
    players[0]["name"] = "이름수정"
    assert host.emit("host_setup", {"players": players}, callback=True)["ok"]
    after = host.emit("host_roster_list", {}, callback=True)["players"]
    assert len(after) == 6 and any(p["name"] == "이름수정" for p in after)


def test_roster_delete_removes_unreferenced_images_immediately(env):
    created, host, *_ = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"]).get_json()["id"]
    players = players_payload(6)
    players[0]["images"] = [iid]
    assert host.emit("host_setup", {"players": keep_uids(host, players)}, callback=True)["ok"]
    uid = snap(host)["players"][0]["uid"]

    # 아직 방의 선수가 이미지를 쓰고 있으므로 명부에서만 지워도 이미지는 남는다
    assert host.emit("host_roster_delete", {"uid": uid}, callback=True)["ok"]
    assert env.manager.images.exists(iid)
    # 방에서 빼고 저장하면(유예시간 0으로 정리) 이미지가 사라진다
    players = players_payload(6)
    assert host.emit("host_setup", {"players": keep_uids(host, players)}, callback=True)["ok"]
    env.manager.gc_images(grace_sec=0)
    assert not env.manager.images.exists(iid)


def test_roster_delete_validation_and_roles(env):
    created, host, leaders, obs = setup_room(env)
    assert not leaders[0].emit("host_roster_list", {}, callback=True)["ok"]
    assert not obs.emit("host_roster_delete", {"uid": "a" * 16}, callback=True)["ok"]
    assert not host.emit("host_roster_delete", {"uid": "../x"}, callback=True)["ok"]
    assert not host.emit("host_roster_delete", {"uid": "a" * 16}, callback=True)["ok"]


def test_image_removed_by_host_is_deleted_on_save(env):
    created, host, *_ = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    a = upload(env, rid, tok["host"]).get_json()["id"]
    b = upload(env, rid, tok["host"]).get_json()["id"]
    players = players_payload(6)
    players[0]["images"] = [a, b]
    assert host.emit("host_setup", {"players": keep_uids(host, players)}, callback=True)["ok"]
    players[0]["images"] = [b]                       # a 를 제거하고 저장
    assert host.emit("host_setup", {"players": keep_uids(host, players)}, callback=True)["ok"]
    assert not env.manager.images.exists(a)           # 유예 시간을 기다리지 않고 즉시 삭제
    assert env.manager.images.exists(b)


def test_orphan_images_are_collected_after_grace(env):
    created = env.create()
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"]).get_json()["id"]
    assert env.manager.gc_images() == 0 and env.manager.images.exists(iid)   # 유예 시간 안에는 보존
    assert env.manager.gc_images(grace_sec=0) == 1 and not env.manager.images.exists(iid)


def test_roster_capacity_enforced(env, monkeypatch):
    created, host, *_ = setup_room(env)           # 명부에 6명 저장됨
    monkeypatch.setattr(config, "MAX_ROSTER", 6)
    players = players_payload(6, prefix="새선수")  # uid 가 없으므로 새 선수 6명 -> 12명이 되어 한도 초과
    ack = host.emit("host_setup", {"players": players}, callback=True)
    assert not ack["ok"] and ack["code"] == "roster_full"
    # 실패하면 방의 선수 목록은 바뀌지 않는다
    assert snap(host)["players"][0]["name"] == "선수1"


def test_started_room_cannot_change_players(env):
    created, host, *_ = setup_room(env)
    assert host.emit("host_start", {}, callback=True)["ok"]
    assert not host.emit("host_setup", {"players": players_payload(6)}, callback=True)["ok"]


# =============================================================================
# 포지션 중복 선택 (탱커/딜러/힐러 중 1개 이상)
# =============================================================================

def test_roles_accept_multiple_and_normalize_order():
    out = parse_player_entries([entry(1, roles=["support", "tank"])])
    assert out[0]["roles"] == ["tank", "support"]                       # 항상 탱커→딜러→힐러 순
    out = parse_player_entries([entry(1, roles=["damage", "damage", "tank"])])
    assert out[0]["roles"] == ["tank", "damage"]                        # 중복은 하나로
    out = parse_player_entries([{k: v for k, v in entry(1).items() if k != "roles"} | {"role": "support"}])
    assert out[0]["roles"] == ["support"]                               # 예전 형식(role 문자열)도 받는다


@pytest.mark.parametrize("bad", [[], "", None, ["mid"], ["tank", "mid"], [1], "mid", {"tank": 1}, ["tank"] * 4])
def test_roles_reject_empty_and_unknown(bad):
    with pytest.raises(ValidationError):
        parse_player_entries([entry(1, roles=bad)])


def test_old_saved_room_and_roster_files_are_migrated(tmp_path):
    import json
    from server.manager import RoomManager
    from server.roster import RosterStore
    from server.storage import RoomStore

    old_player = {"id": 0, "name": "옛선수", "role": "support", "elo": 1, "intro": "", "winning_bid": 0, "assigned_team_id": -1}
    assert Player.from_dict(old_player).roles == ["support"]
    assert Player.from_dict({**old_player, "role": "mid"}).roles == []

    uid = "a" * 16
    (tmp_path / "roster.json").write_text(json.dumps({"version": 1, "players": {uid: {"name": "옛", "battletag": "", "role": "tank",
                                                                                     "heroes": [], "images": [], "elo": None, "intro": ""}}}))
    got = RosterStore(tmp_path).get(uid)
    assert got["roles"] == ["tank"] and "role" not in got


def test_multi_role_player_visible_to_everyone_and_in_unsold_list(env):
    created, host, leaders, obs = setup_room(env)
    players = players_payload(6, prefix="다중")          # setup_room 이 만든 선수와 이름이 겹치지 않게
    players[0].update(roles=["tank", "damage", "support"])
    assert host.emit("host_setup", {"players": players}, callback=True)["ok"]
    listed = {p["name"]: p for p in host.emit("host_roster_list", {}, callback=True)["players"]}
    assert listed[players[0]["name"]]["roles"] == ["tank", "damage", "support"]
