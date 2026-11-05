"""터널 자동 실행(--tunnel)과 접속 자가 점검 테스트. 실제 cloudflared 대신 가짜 실행 파일을 쓴다."""
import stat
import sys

import pytest

from server import selfcheck, tunnel


def fake_binary(tmp_path, body):
    path = tmp_path / "fake-cloudflared"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_find_url_parses_cloudflared_banner():
    line = "2026-11-05T13:45:27Z INF |  https://Tions-Fitness-arbitrary-paso.trycloudflare.com   |"
    assert tunnel.find_url(line) == "https://tions-fitness-arbitrary-paso.trycloudflare.com"
    assert tunnel.find_url("https://api.cloudflare.com/x") is None
    assert tunnel.find_url("no url here") is None
    # 비슷하게 생긴 다른 도메인은 받지 않는다
    assert tunnel.find_url("https://evil.com/?u=https://x.trycloudflare.com.evil.com") is not None  # 앞부분 일치만 취함
    assert tunnel.find_url("https://x.trycloudflare.com.evil.com").endswith("trycloudflare.com")


def test_start_quick_tunnel_reads_url_and_uses_ipv4_loopback(tmp_path):
    exe = fake_binary(tmp_path, (
        "print('args', sys.argv[1:], flush=True)\n"
        "print('| https://abc-def-ghi.trycloudflare.com |', flush=True)\n"
        "time.sleep(30)"))
    proc, url = tunnel.start_quick_tunnel(5099, binary=exe, timeout=10)
    try:
        assert url == "https://abc-def-ghi.trycloudflare.com"
        assert proc.poll() is None
    finally:
        tunnel.stop_tunnel(proc)
    assert proc.poll() is not None


def test_target_is_127_0_0_1_not_localhost(tmp_path):
    out = tmp_path / "args.txt"
    exe = fake_binary(tmp_path, (
        f"open({str(out)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        "print('https://x-y.trycloudflare.com', flush=True)\n"
        "time.sleep(30)"))
    proc, _ = tunnel.start_quick_tunnel(5077, binary=exe, timeout=10)
    try:
        assert "http://127.0.0.1:5077" in out.read_text()
        assert "localhost" not in out.read_text()
    finally:
        tunnel.stop_tunnel(proc)


def test_failure_when_no_url_is_printed(tmp_path):
    exe = fake_binary(tmp_path, "print('ERR something broke', flush=True)")
    with pytest.raises(tunnel.TunnelError) as e:
        tunnel.start_quick_tunnel(5099, binary=exe, timeout=5)
    assert "something broke" in str(e.value)


def test_missing_cloudflared_gives_install_hint(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda _n: None)
    with pytest.raises(tunnel.TunnelError) as e:
        tunnel.start_quick_tunnel(5099)
    assert "winget" in str(e.value)


def test_selfcheck_reports_failure_and_success(monkeypatch):
    ok, detail = selfcheck.check_once("http://127.0.0.1:1", timeout=1)      # 아무도 안 듣는 포트
    assert ok is False and detail
    msgs = []
    monkeypatch.setattr(selfcheck, "check_once", lambda url, timeout=8.0: (True, "HTTP 200"))
    selfcheck.run_in_background("https://x.trycloudflare.com", printer=msgs.append).join(5)
    assert msgs and "접속이 됩니다" in msgs[0]
    msgs.clear()
    monkeypatch.setattr(selfcheck, "check_once", lambda url, timeout=8.0: (False, "HTTP 502"))
    selfcheck.run_in_background("https://x.trycloudflare.com", attempts=2, delay=0.01, printer=msgs.append).join(5)
    assert msgs and "접속하지 못했습니다" in msgs[0] and "HTTP 502" in msgs[0]
