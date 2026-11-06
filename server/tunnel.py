"""외부 접속용 Cloudflare 퀵 터널을 app.py 가 대신 켜 준다 (--tunnel).

두 개의 cmd 창과 주소 복사/붙여넣기를 없애기 위한 편의 기능이다.
- cloudflared 를 자식 프로세스로 실행하고, 출력에서 https://xxxx.trycloudflare.com 주소를 읽는다.
- 서버를 종료하면 터널도 같이 종료한다.
- 터널 대상은 localhost 가 아니라 127.0.0.1 로 고정한다. Windows 에서 localhost 가 IPv6(::1)로
  풀리면 IPv4(127.0.0.1)에서만 듣는 서버에 연결하지 못해 502 오류가 나기 때문이다.
"""
from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import threading
import time
from collections import deque

URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.IGNORECASE)


class TunnelError(Exception):
    pass


def find_url(text: str) -> str | None:
    m = URL_RE.search(text)
    return m.group(0).lower() if m else None


CONNECTED_RE = re.compile(r"Registered tunnel connection", re.IGNORECASE)


def _launch(exe: str, port: int, protocol: str) -> subprocess.Popen:
    """cloudflared 를 띄우고, 출력에서 주소(url_event)와 '실제 연결 성공'(connected)을 감지한다."""
    cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]
    if protocol in ("quic", "http2"):
        cmd += ["--protocol", protocol]         # 학교/공용망에서 UDP(quic)가 막혀 느릴 때 http2 로 바꿔 본다
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    atexit.register(stop_tunnel, proc)
    proc.auction_url = []                       # type: ignore[attr-defined]
    proc.auction_log = deque(maxlen=60)         # type: ignore[attr-defined]
    proc.auction_url_event = threading.Event()  # type: ignore[attr-defined]
    proc.auction_connected = threading.Event()  # type: ignore[attr-defined]

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:               # 끝까지 읽어서 파이프가 막히지 않게 한다
            line = line.strip()
            proc.auction_log.append(line)
            if not proc.auction_url:
                url = find_url(line)
                if url:
                    proc.auction_url.append(url)
                    proc.auction_url_event.set()
            if CONNECTED_RE.search(line):
                proc.auction_connected.set()
        proc.auction_url_event.set()

    threading.Thread(target=reader, name="tunnel-reader", daemon=True).start()
    return proc


def _wait_url(proc: subprocess.Popen, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not proc.auction_url and proc.poll() is None:
        proc.auction_url_event.wait(0.3)
    return proc.auction_url[0] if proc.auction_url else None


def start_quick_tunnel(port: int, *, timeout: float = 45.0, binary: str | None = None,
                       protocol: str = "auto", connect_timeout: float = 25.0,
                       retry_http2: bool = True) -> tuple[subprocess.Popen, str]:
    """터널을 켜고 (프로세스, 주소)를 돌려준다.

    cloudflared 는 주소를 먼저 발급받아 출력하고 Cloudflare 와의 실제 연결은 그 뒤에 맺는다.
    네트워크(DNS/방화벽)가 막혀 있으면 주소만 있고 연결이 없어서 그 주소가 열리지 않는다(NXDOMAIN).
    그래서 "Registered tunnel connection" 이 보일 때까지 기다리고, 안 되면 http2 로 한 번 더 시도한다.
    연결 여부는 tunnel_connected(proc) 로 확인한다.
    """
    exe = binary or shutil.which("cloudflared")
    if not exe:
        raise TunnelError(
            "cloudflared 를 찾을 수 없습니다. 설치: winget install --id Cloudflare.cloudflared "
            "(설치 후 cmd 를 닫았다가 새로 여세요)")
    proc = _launch(exe, port, protocol)
    url = _wait_url(proc, timeout)
    if not url:
        tail = list(proc.auction_log)[-6:]
        stop_tunnel(proc)
        raise TunnelError("터널 주소를 받지 못했습니다. cloudflared 출력:\n  " + "\n  ".join(tail))
    if not proc.auction_connected.wait(connect_timeout) and protocol == "auto" and retry_http2 and proc.poll() is None:
        stop_tunnel(proc)                       # 기본 방식(quic/UDP)이 안 붙음 → TCP(http2) 로 재시도
        second = _launch(exe, port, "http2")
        second_url = _wait_url(second, timeout)
        if second_url:
            second.auction_connected.wait(connect_timeout)
            second.auction_first_attempt = list(proc.auction_log)   # type: ignore[attr-defined]
            return second, second_url
        stop_tunnel(second)
        raise TunnelError("터널 주소를 받지 못했습니다. cloudflared 출력:\n  " + "\n  ".join(list(second.auction_log)[-6:]))
    return proc, url


def tunnel_connected(proc: subprocess.Popen) -> bool:
    return bool(getattr(proc, "auction_connected", None) and proc.auction_connected.is_set())


def tunnel_log_tail(proc: subprocess.Popen, n: int = 12) -> list[str]:
    return list(getattr(proc, "auction_log", []))[-n:]


def stop_tunnel(proc: subprocess.Popen) -> None:
    proc._auction_stopping = True            # 정상 종료이므로 감시 스레드가 경고를 내지 않게 한다
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def watch_tunnel(proc: subprocess.Popen, printer=print) -> threading.Thread:
    """터널 프로세스가 중간에 종료되면 바로 알린다 (그 순간부터 외부 주소는 열리지 않는다)."""
    def work() -> None:
        code = proc.wait()
        if getattr(proc, "_auction_stopping", False):
            return
        printer("\n[경고] 외부 접속용 터널(cloudflared)이 종료되었습니다 (코드 %s).\n"
                "       지금부터 외부에서는 접속할 수 없습니다. 서버를 다시 켜면 새 주소가 만들어집니다.\n"
                "       (이미 나눠 준 링크는 주소 부분만 새 주소로 바꾸면 그대로 쓸 수 있습니다.)" % code)

    t = threading.Thread(target=work, name="tunnel-watch", daemon=True)
    t.start()
    return t
