"""외부 주소 진단(netdiag), 터널 감시, 시작 자가 점검의 진단 출력 테스트."""
import stat
import struct
import sys
import time

import pytest

from server import netdiag, selfcheck, tunnel
from server.netdiag import DnsResult, HttpResult


def fake_answer(host, rcode=0, ips=()):
    """DNS 서버 응답을 흉내 낸 바이트열."""
    query = netdiag.build_query(host)
    flags = 0x8180 | rcode
    out = struct.pack("!HHHHHH", 0x2B1D, flags, 1, len(ips), 0, 0) + query[12:]
    for ip in ips:
        out += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes(int(x) for x in ip.split("."))
    return out


def test_dns_packet_roundtrip():
    q = netdiag.build_query("abc-def.trycloudflare.com")
    assert q[:2] == b"\x2b\x1d" and b"\x07abc-def\x0dtrycloudflare\x03com\x00" in q
    assert netdiag.parse_response(fake_answer("a.b.com", 3)) == (3, [])
    assert netdiag.parse_response(fake_answer("a.b.com", 0, ["104.16.1.2", "104.16.3.4"])) == (0, ["104.16.1.2", "104.16.3.4"])
    with pytest.raises(ValueError):
        netdiag.parse_response(b"\x00\x01")


def test_host_of():
    assert netdiag.host_of("https://hit-gnu.trycloudflare.com/r/abc#t=secret") == "hit-gnu.trycloudflare.com"
    assert netdiag.host_of("hit-gnu.trycloudflare.com") == "hit-gnu.trycloudflare.com"
    with pytest.raises(ValueError):
        netdiag.host_of("https://")


NX, OK, DEAD = DnsResult("nxdomain"), DnsResult("ok", ["104.16.0.1"]), DnsResult("unreachable")


@pytest.mark.parametrize("system,public,http,code", [
    (NX, {"1.1.1.1": NX, "8.8.8.8": NX}, None, "gone"),                              # 오늘 상황: 주소가 사라짐
    (NX, {"1.1.1.1": DEAD, "8.8.8.8": DEAD}, None, "gone"),                           # UDP 53 이 막혔지만 이 PC DNS 도 '없음'
    (NX, {"1.1.1.1": OK, "8.8.8.8": OK}, HttpResult(200, "HTTP/1.1 200 OK"), "local_dns"),   # 터널 정상, 이 PC DNS 만 문제
    (NX, {"1.1.1.1": OK, "8.8.8.8": NX}, HttpResult(530, "HTTP/1.1 530"), "local_dns"),
    (OK, {"1.1.1.1": OK, "8.8.8.8": OK}, HttpResult(200, "HTTP/1.1 200 OK"), "ok"),
    (OK, {"1.1.1.1": OK, "8.8.8.8": OK}, HttpResult(502, "HTTP/1.1 502"), "http"),
    (OK, {"1.1.1.1": OK, "8.8.8.8": OK}, HttpResult(530, "HTTP/1.1 530"), "http"),
    (DnsResult("unreachable"), {"1.1.1.1": DEAD, "8.8.8.8": DEAD}, None, "offline"),
])
def test_diagnose(system, public, http, code):
    v = netdiag.diagnose(system, public, http)
    assert v.code == code and v.title and v.steps


def test_gone_verdict_tells_how_to_reuse_old_links():
    v = netdiag.diagnose(NX, {"1.1.1.1": NX, "8.8.8.8": NX}, None)
    assert any("주소 부분" in s and "/r/" in s for s in v.steps)


def test_selfcheck_failure_prints_diagnosis(monkeypatch):
    monkeypatch.setattr(selfcheck, "check_once", lambda url, timeout=8.0: (False, "URLError: nodename nor servname"))
    monkeypatch.setattr(netdiag, "resolve_system", lambda h: NX)
    monkeypatch.setattr(netdiag, "resolve_via", lambda s, h, timeout=3.0: NX)
    lines = []
    selfcheck.run_in_background("https://dead-one.trycloudflare.com", attempts=1, delay=0.01, printer=lines.append).join(5)
    text = "\n".join(lines)
    assert "[경고]" in text and "[진단] 이 주소는 이제 존재하지 않습니다" in text and "python netcheck.py https://dead-one" in text


def test_selfcheck_survives_diagnosis_crash(monkeypatch):
    monkeypatch.setattr(selfcheck, "check_once", lambda url, timeout=8.0: (False, "boom"))
    monkeypatch.setattr(netdiag, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    lines = []
    selfcheck.run_in_background("https://x.trycloudflare.com", attempts=1, delay=0.01, printer=lines.append).join(5)
    assert any("자동 진단 실패" in l for l in lines)


def _fake_exe(tmp_path, body):
    exe = tmp_path / "fake-cf"
    exe.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_tunnel_watch_warns_when_process_dies(tmp_path):
    exe = _fake_exe(tmp_path, "print('https://a-b.trycloudflare.com', flush=True)\nprint('INF Registered tunnel connection connIndex=0', flush=True)\ntime.sleep(1.2)\nsys.exit(7)")
    proc, _ = tunnel.start_quick_tunnel(5000, binary=exe, timeout=10)
    lines = []
    tunnel.watch_tunnel(proc, printer=lines.append).join(8)
    assert lines and "터널(cloudflared)이 종료" in lines[0] and "코드 7" in lines[0] and "주소 부분만" in lines[0]


def test_tunnel_watch_is_silent_on_normal_stop(tmp_path):
    exe = _fake_exe(tmp_path, "print('https://a-b.trycloudflare.com', flush=True)\nprint('INF Registered tunnel connection connIndex=0', flush=True)\ntime.sleep(30)")
    proc, _ = tunnel.start_quick_tunnel(5000, binary=exe, timeout=10)
    lines = []
    t = tunnel.watch_tunnel(proc, printer=lines.append)
    tunnel.stop_tunnel(proc)
    t.join(8)
    assert lines == []
