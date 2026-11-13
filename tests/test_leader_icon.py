"""팀장 프로필 아이콘: 저장, 공개 범위, 검증, 정리."""
import io

from PIL import Image

from server.models import Team
from tests.test_server_flow import env, last_state, setup_room, snap, teams_payload  # noqa: F401


def png(color=(30, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def upload(env, rid, token, data):
    return env.http.post(f"/api/rooms/{rid}/images", data=data, headers={**bearer(token), "Origin": "http://localhost"},
                         content_type="application/octet-stream").get_json()["id"]


def with_icons(n, icons):
    teams = teams_payload(n)
    for i, iid in icons.items():
        teams[i]["leader_image"] = iid
    return teams


def test_icon_is_saved_and_shown_to_everyone(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"], png())
    assert host.emit("host_setup", {"teams": with_icons(3, {1: iid})}, callback=True)["ok"]

    for client in (host, obs, leaders[0]):
        teams = snap(client)["teams"]
        assert [t["leader_image"] for t in teams] == ["", iid, ""]
    # 경매 전에도, 관전자·다른 팀장도 아이콘은 받을 수 있다 (팀 현황에 항상 보인다)
    for token in (tok["host"], tok["observer"], tok["leaders"][0], tok["leaders"][1]):
        r = env.http.get(f"/api/rooms/{rid}/images/{iid}?v=preview", headers=bearer(token))
        assert r.status_code == 200 and r.mimetype == "image/webp"
    assert env.http.get(f"/api/rooms/{rid}/images/{iid}").status_code == 403          # 토큰 없이는 불가
    assert env.manager.get(rid).room.teams[1].leader_image == iid


def test_icon_survives_restart_format_and_old_rooms_load(env):
    team = Team.from_dict({"id": 0, "leader_name": "옛날팀장", "token_hash": "x"})            # 아이콘 필드가 없던 저장본
    assert team.leader_image == ""
    assert Team.from_dict({**team.to_dict(), "leader_image": "abc", "unknown_key": 1}).leader_image == "abc"


def test_icon_validation(env):
    created, host, leaders, obs = setup_room(env)
    for bad in ("../../etc/passwd", "zz", 123, ["a" * 32]):
        res = host.emit("host_setup", {"teams": with_icons(3, {0: bad})}, callback=True)
        assert not res["ok"] and "아이콘" in res["message"], bad
    gone = upload(env, created["room_id"], created["tokens"]["host"], png())
    assert env.manager.images.delete(gone)
    res = host.emit("host_setup", {"teams": with_icons(3, {0: gone})}, callback=True)        # 형식은 맞지만 없는 이미지
    assert not res["ok"] and "업로드되지 않았거나" in res["message"]
    assert all(t["leader_image"] == "" for t in snap(host)["teams"])


def test_only_host_can_set_icons(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"], png())
    assert not leaders[0].emit("host_setup", {"teams": with_icons(3, {0: iid})}, callback=True)["ok"]
    assert env.http.post(f"/api/rooms/{rid}/images", data=png(), headers={**bearer(tok["leaders"][0]), "Origin": "http://localhost"},
                         content_type="application/octet-stream").status_code == 403


def test_removed_or_replaced_icon_is_deleted_but_used_one_is_kept(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    first, second = upload(env, rid, tok["host"], png((1, 2, 3))), upload(env, rid, tok["host"], png((9, 8, 7)))
    assert host.emit("host_setup", {"teams": with_icons(3, {0: first})}, callback=True)["ok"]
    env.manager.gc_images(grace_sec=0, only={first})
    assert env.manager.images.exists(first)                                                  # 쓰는 중이면 정리되지 않는다
    assert host.emit("host_setup", {"teams": with_icons(3, {0: second})}, callback=True)["ok"]    # 교체
    assert not env.manager.images.exists(first) and env.manager.images.exists(second)
    assert host.emit("host_setup", {"teams": with_icons(3, {})}, callback=True)["ok"]             # 제거
    assert not env.manager.images.exists(second)


def test_setup_without_icon_field_keeps_working(env):
    created, host, leaders, obs = setup_room(env)
    assert host.emit("host_setup", {"teams": teams_payload(3)}, callback=True)["ok"]
    assert all(t["leader_image"] == "" for t in snap(obs)["teams"])


def test_icon_change_reaches_clients_as_small_patch(env):
    created, host, leaders, obs = setup_room(env)
    rid, tok = created["room_id"], created["tokens"]
    iid = upload(env, rid, tok["host"], png())
    last_state(obs)
    assert host.emit("host_setup", {"teams": with_icons(3, {2: iid})}, callback=True)["ok"]
    events = [e for e in obs.get_received() if e["name"] in ("state", "patch")]
    assert [e["name"] for e in events] == ["patch"]
    assert events[0]["args"][0]["p"][1]["teams"] == ["l", {"2": ["o", {"leader_image": ["=", iid]}]}]


def test_image_reads_survive_everyone_loading_at_once_from_one_ip(env):
    """터널을 쓰면 참가자 전원과 주최자가 같은 IP 로 보인다. 30명 x 사진 2장 + 주최자 썸네일이 몰려도 막히지 않는다."""
    created, host, leaders, obs = setup_room(env, 4, 5)
    rid, tok = created["room_id"], created["tokens"]
    ids = [upload(env, rid, tok["host"], png((i, 2, 3))) for i in range(2)]
    players = [{"name": f"선수{i+1}", "role": "damage", "elo": 1500, "images": ids} for i in range(16)]
    assert host.emit("host_setup", {"players": players}, callback=True)["ok"]
    assert host.emit("host_start", {}, callback=True)["ok"]
    codes = [env.http.get(f"/api/rooms/{rid}/images/{ids[0]}?v=preview", headers=bearer(tok["host"])).status_code
             for _ in range(32)]                                             # 주최자 화면의 선수 카드 썸네일
    codes += [env.http.get(f"/api/rooms/{rid}/images/{iid}?v=preview", headers=bearer(tok["observer"])).status_code
              for _ in range(30) for iid in ids]                              # 시청자 30명 x 2장
    assert set(codes) == {200}, {c: codes.count(c) for c in set(codes)}

