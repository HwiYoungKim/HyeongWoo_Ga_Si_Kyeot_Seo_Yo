"""관전자는 닉네임을 정해야 들어갈 수 있다."""
import pytest

from tests.test_server_flow import env, setup_room, snap  # noqa: F401


def raw_join(client, created, token, **extra):
    return client.emit("join", {"room_id": created["room_id"], "token": token, **extra}, callback=True)


@pytest.mark.parametrize("nickname", [None, "", "   ", "\u0000\u0007", "\n\t"])
def test_observer_cannot_enter_without_nickname(env, nickname):
    created, host, leaders, obs = setup_room(env)
    newcomer = env.client()
    extra = {} if nickname is None else {"nickname": nickname}
    ack = raw_join(newcomer, created, created["tokens"]["observer"], **extra)
    assert ack["ok"] is False and ack["code"] == "nickname_required" and "닉네임" in ack["message"]
    assert "snapshot" not in ack and "chat" not in ack                 # 방의 상태도 채팅도 내려주지 않는다
    # 들어오지 못했으므로 방의 방송도 받지 못하고, 어떤 요청도 할 수 없다
    assert host.emit("host_start", {}, callback=True)["ok"]
    assert [e["name"] for e in newcomer.get_received()] == []
    assert newcomer.emit("sync", {}, callback=True)["ok"] is False
    assert newcomer.emit("chat", {"text": "안녕"}, callback=True)["ok"] is False


@pytest.mark.parametrize("bad", [123, ["a"], "가" * 17])
def test_invalid_nickname_is_rejected_with_reason(env, bad):
    created, host, leaders, obs = setup_room(env)
    ack = raw_join(env.client(), created, created["tokens"]["observer"], nickname=bad)
    assert ack["ok"] is False and ack["code"] == "nickname_required" and "닉네임" in ack["message"]


def test_observer_enters_with_nickname_and_chats_under_it(env):
    created, host, leaders, obs = setup_room(env)
    newcomer = env.client()
    assert raw_join(newcomer, created, created["tokens"]["observer"])["code"] == "nickname_required"
    ack = raw_join(newcomer, created, created["tokens"]["observer"], nickname="  구경꾼  ")
    assert ack["ok"] and ack["role"] == "observer" and ack["nickname"] == "구경꾼" and "snapshot" in ack
    host.get_received()
    assert newcomer.emit("chat", {"text": "화이팅"}, callback=True)["ok"]
    chats = [e["args"][0] for e in host.get_received() if e["name"] == "chat"]
    assert chats and chats[-1]["name"] == "구경꾼" and chats[-1]["text"] == "화이팅"


def test_host_and_leader_need_no_nickname(env):
    created = env.create()
    for token in (created["tokens"]["host"], created["tokens"]["leaders"][0]):
        ack = raw_join(env.client(), created, token)
        assert ack["ok"] and ack["nickname"]


def test_changing_to_empty_nickname_keeps_the_current_session(env):
    created, host, leaders, obs = setup_room(env)
    watcher = env.client()
    assert raw_join(watcher, created, created["tokens"]["observer"], nickname="처음이름")["ok"]
    ack = raw_join(watcher, created, created["tokens"]["observer"], nickname="")
    assert ack["code"] == "nickname_required"
    assert snap(watcher)["state"] == "WAITING"                         # 기존 접속은 그대로
    host.get_received()
    assert watcher.emit("chat", {"text": "그대로"}, callback=True)["ok"]
    assert [e["args"][0]["name"] for e in host.get_received() if e["name"] == "chat"] == ["처음이름"]
    assert raw_join(watcher, created, created["tokens"]["observer"], nickname="바꾼이름")["nickname"] == "바꾼이름"


def test_reserved_names_are_still_marked(env):
    created, host, leaders, obs = setup_room(env)
    ack = raw_join(env.client(), created, created["tokens"]["observer"], nickname="주최자")
    assert ack["ok"] and ack["nickname"] == "주최자 (관전)"


def test_missing_nickname_does_not_count_as_a_failed_login(env):
    created, host, leaders, obs = setup_room(env)
    for _ in range(30):
        assert raw_join(env.client(), created, created["tokens"]["observer"])["code"] == "nickname_required"
    assert raw_join(env.client(), created, created["tokens"]["observer"], nickname="나중에")["ok"]
