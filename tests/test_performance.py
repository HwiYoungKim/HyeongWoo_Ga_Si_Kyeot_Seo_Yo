"""접속 속도/실시간성 최적화 테스트: 정적 파일 캐시·압축, 모듈 미리받기, 저장 지연 쓰기, 접속 현황 묶음 전송."""
import gzip
import json
import re
import time

import pytest

from server import tunnel
from server.assets import Assets
from server.storage import RoomStore, WriteBehind
from tests.test_server_flow import env, setup_room  # noqa: F401


# ---------------------------------------------------------------- 정적 파일
def _room_page(env):
    return env.http.get("/r/abcdef12").get_data(as_text=True)


def test_page_references_versioned_assets_and_preloads_modules(env):
    html = _room_page(env)
    ver = re.search(r"/s/([0-9a-f]{10})/js/room\.js", html).group(1)
    assert f"/s/{ver}/css/style.css" in html and f"/s/{ver}/vendor/socket.io.min.js" in html
    pre = re.findall(r'<link rel="modulepreload" href="([^"]+)"', html)
    for needed in ("js/lib/net.js", "js/views/common.js", "js/views/host.js", "js/views/leader.js", "js/views/observer.js"):
        assert f"/s/{ver}/{needed}" in pre, needed
    assert "/static/" not in html


def test_versioned_asset_is_immutable_gzip_and_revalidates(env):
    ver = re.search(r"/s/([0-9a-f]{10})/", _room_page(env)).group(1)
    url = f"/s/{ver}/js/views/common.js"
    r = env.http.get(url, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "immutable" in r.headers["Cache-Control"] and "max-age=31536000" in r.headers["Cache-Control"]
    assert r.headers["Content-Encoding"] == "gzip" and r.headers["Content-Type"].startswith("text/javascript")
    plain = env.http.get(url, headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in plain.headers
    assert gzip.decompress(r.get_data()) == plain.get_data()
    assert len(r.get_data()) < len(plain.get_data()) * 0.6
    again = env.http.get(url, headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304
    # 보안 헤더는 정적 응답에도 붙는다
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and r.headers["X-Content-Type-Options"] == "nosniff"


def test_stale_version_is_served_but_not_cached(env):
    r = env.http.get("/s/0000000000/js/room.js")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("path", [
    "/s/x/../../server/config.py", "/s/x/..%2f..%2fserver%2fconfig.py", "/s/x/js/../../app.py",
    "/s/x/%2e%2e/app.py", "/s/x/nope.js", "/s/x/",
])
def test_static_route_never_serves_files_outside_static(env, path):
    assert env.http.get(path).status_code in (404, 308, 301)


def test_html_and_server_info_are_gzipped_but_secret_endpoints_are_not(env):
    r = env.http.get("/r/abcdef12", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Content-Encoding") == "gzip" and "<html" in gzip.decompress(r.get_data()).decode()
    assert r.headers["Cache-Control"] == "no-store"
    info = env.http.get("/api/server-info", headers={"Accept-Encoding": "gzip"})
    assert info.headers.get("Content-Encoding") == "gzip" and json.loads(gzip.decompress(info.get_data()))["ok"]
    created = env.http.post("/api/rooms", json={"admin_key": "adminkey-for-tests", "title": "t", "num_teams": 2, "members_per_team": 2},
                            headers={"Accept-Encoding": "gzip"})
    assert created.status_code == 201 and "Content-Encoding" not in created.headers


def test_asset_version_changes_when_content_changes(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "a.js").write_text("export const a = 1;\n" * 30)
    v1 = Assets.load(tmp_path).version
    assert Assets.load(tmp_path).version == v1
    (tmp_path / "js" / "a.js").write_text("export const a = 2;\n" * 30)
    assert Assets.load(tmp_path).version != v1


def test_module_graph_follows_static_and_dynamic_imports(tmp_path):
    (tmp_path / "js" / "lib").mkdir(parents=True)
    (tmp_path / "js" / "main.js").write_text("import { x } from './lib/x.js';\nconst v = await import('./later.js');\n")
    (tmp_path / "js" / "lib" / "x.js").write_text("import { y } from '../y.js';\nexport const x = 1;\n")
    (tmp_path / "js" / "y.js").write_text("export const y = 1;\n")
    (tmp_path / "js" / "later.js").write_text("export {};\n")
    a = Assets.load(tmp_path)
    assert a.module_closure("js/main.js") == ["js/later.js", "js/lib/x.js", "js/y.js"]


# ---------------------------------------------------------------- 저장 지연 쓰기
def test_write_behind_coalesces_and_flushes(tmp_path):
    store = RoomStore(tmp_path)
    calls = []
    orig = store.save_text
    store.save_text = lambda rid, text: (calls.append(text), orig(rid, text))[1]
    w = WriteBehind(store, delay=0.15)
    for i in range(20):
        w.submit("abcdef12", json.dumps({"rev": i}))
    assert calls == []                          # 아직 디스크에 쓰지 않았다 (입찰 경로가 기다리지 않는다)
    time.sleep(0.5)
    assert len(calls) == 1 and json.loads(calls[0]) == {"rev": 19}      # 20번의 저장이 1번으로 묶이고 최신값만 남는다
    w.submit("abcdef12", json.dumps({"rev": 20}))
    w.flush()                                   # 종료 시에는 기다리지 않고 즉시 쓴다
    assert json.loads((tmp_path / "rooms" / "abcdef12.json").read_text()) == {"rev": 20}
    w.close()


def test_discard_prevents_deleted_room_from_reappearing(tmp_path):
    store = RoomStore(tmp_path)
    w = WriteBehind(store, delay=0.1)
    w.submit("abcdef12", "{}")
    w.discard("abcdef12")
    time.sleep(0.4)
    assert not (tmp_path / "rooms" / "abcdef12.json").exists()
    w.close()


def test_async_manager_saves_after_flush(tmp_path):
    from server.manager import RoomManager
    m = RoomManager(RoomStore(tmp_path), async_save=True)
    created = m.create_room("t", 2, 2)
    m.flush()
    assert (tmp_path / "rooms" / f"{created.handle.room.id}.json").exists()
    m.delete_room(created.handle.room.id)
    time.sleep(0.4)
    assert not (tmp_path / "rooms" / f"{created.handle.room.id}.json").exists()


# ---------------------------------------------------------------- 접속 현황 묶음 전송
def test_presence_broadcasts_are_coalesced(env):
    created, host, leaders, obs = setup_room(env, 3, 3)
    host.get_received()
    extra = [env.client() for _ in range(10)]
    for c in extra:
        assert env.join(c, created["room_id"], created["tokens"]["observer"])["ok"]
    time.sleep(0.6)
    states = [m for m in host.get_received() if m["name"] == "state"]
    assert 1 <= len(states) <= 3, len(states)          # 10명 입장 → 전체 상태 전송은 1~2번
    online = states[-1]["args"][0]
    assert online["room"]["title"]


def test_tunnel_protocol_flag_is_passed_to_cloudflared(tmp_path):
    import stat
    import sys
    out = tmp_path / "args.txt"
    exe = tmp_path / "fake"
    exe.write_text(f"#!{sys.executable}\nimport sys,time\nopen({str(out)!r},'w').write(' '.join(sys.argv[1:]))\n"
                   "print('https://a-b.trycloudflare.com', flush=True)\nprint('INF Registered tunnel connection connIndex=0', flush=True)\ntime.sleep(20)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    proc, _ = tunnel.start_quick_tunnel(5000, binary=str(exe), timeout=10, protocol="http2")
    try:
        assert "--protocol http2" in out.read_text()
    finally:
        tunnel.stop_tunnel(proc)
