"""외부 공개 주소(터널 주소)가 왜 안 열리는지 원인을 가려 주는 진단.

브라우저의 `DNS_PROBE_FINISHED_NXDOMAIN` 같은 오류는 "주소가 없다"는 결과만 알려 준다.
여기서는 같은 주소를 세 가지 방법으로 확인해 원인을 좁힌다.

  1) 이 PC 의 기본 DNS 로 주소를 찾는다.
  2) 공개 DNS(1.1.1.1, 8.8.8.8)에 직접 물어본다. (UDP 53, 표준 라이브러리만 사용)
  3) 공개 DNS 가 알려 준 IP 로 직접 접속해 /api/server-info 를 요청한다. (이 PC 의 DNS 와 무관)

비밀 정보는 보내지 않는다.
"""
from __future__ import annotations

import socket
import ssl
import struct
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
RCODE_NXDOMAIN = 3


# ---------------------------------------------------------------- DNS 질의 (A 레코드)
def build_query(host: str, qid: int = 0x2B1D) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)          # 재귀 요청, 질문 1개
    name = b"".join(bytes([len(p)]) + p.encode("idna") for p in host.strip(".").split(".")) + b"\x00"
    return header + name + struct.pack("!HH", 1, 1)                  # A, IN


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        if pos >= len(data):
            raise ValueError("DNS 응답이 잘렸습니다")
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:                                     # 압축 포인터
            return pos + 2
        pos += 1 + length


def parse_response(data: bytes) -> tuple[int, list[str]]:
    """(rcode, [IPv4...]) 를 돌려준다."""
    if len(data) < 12:
        raise ValueError("DNS 응답이 너무 짧습니다")
    _qid, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    rcode = flags & 0x000F
    pos = 12
    for _ in range(qd):
        pos = _skip_name(data, pos) + 4
    ips: list[str] = []
    for _ in range(an):
        pos = _skip_name(data, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[pos:pos + 4]))
        pos += rdlen
    return rcode, ips


@dataclass
class DnsResult:
    status: str                       # "ok" | "nxdomain" | "no_answer" | "unreachable"
    ips: list[str] = field(default_factory=list)
    detail: str = ""


def resolve_via(server: str, host: str, timeout: float = 3.0) -> DnsResult:
    query = build_query(host)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, (server, 53))
            data, _ = s.recvfrom(2048)
        rcode, ips = parse_response(data)
    except (OSError, ValueError, struct.error) as e:
        return DnsResult("unreachable", detail=f"{type(e).__name__}: {e}")
    if rcode == RCODE_NXDOMAIN:
        return DnsResult("nxdomain")
    if ips:
        return DnsResult("ok", ips)
    return DnsResult("no_answer", detail=f"rcode={rcode}")


def resolve_system(host: str) -> DnsResult:
    try:
        infos = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return DnsResult("nxdomain" if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)) else "unreachable",
                         detail=str(e))
    return DnsResult("ok", sorted({i[4][0] for i in infos}))


# ---------------------------------------------------------------- HTTP 확인
@dataclass
class HttpResult:
    status: Optional[int] = None
    detail: str = ""


def probe_http(host: str, ip: Optional[str] = None, timeout: float = 8.0) -> HttpResult:
    """ip 를 주면 DNS 를 거치지 않고 그 IP 로 직접 접속한다 (SNI/Host 는 host)."""
    try:
        sock = socket.create_connection((ip or host, 443), timeout=timeout)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            tls.sendall((f"GET /api/server-info HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0 (auction-netcheck)\r\n"
                         "Accept: application/json\r\nConnection: close\r\n\r\n").encode())
            head = b""
            while b"\r\n" not in head and len(head) < 2048:
                chunk = tls.recv(512)
                if not chunk:
                    break
                head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        return HttpResult(int(line.split()[1]), line)
    except (OSError, ValueError, IndexError, ssl.SSLError) as e:
        return HttpResult(None, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 판정 (순수 함수: 테스트하기 쉽게 분리)
@dataclass
class Verdict:
    code: str
    title: str
    steps: list[str]


def diagnose(system: DnsResult, public: dict[str, DnsResult], http: Optional[HttpResult]) -> Verdict:
    public_ok = any(r.status == "ok" for r in public.values())
    public_nx = any(r.status == "nxdomain" for r in public.values())
    public_dead = all(r.status == "unreachable" for r in public.values()) if public else True

    if http is not None and http.status == 200:
        if system.status == "ok":
            return Verdict("ok", "정상입니다. 이 PC 에서 외부 주소로 접속됩니다.",
                           ["참가자가 못 열면 그 사람의 DNS 문제일 수 있습니다. 그 사람에게 8.8.8.8 / 1.1.1.1 DNS 사용이나 모바일 데이터 접속을 권하세요."])
        return Verdict("local_dns", "터널은 정상인데 이 PC(네트워크)의 DNS 가 주소를 못 찾습니다.", _dns_steps())

    if system.status != "ok" and public_ok:
        return Verdict("local_dns", "주소는 존재하지만 이 PC(네트워크)의 DNS 가 못 찾습니다. (DNS 문제)", _dns_steps())

    if (public_nx and not public_ok) or (public_dead and system.status == "nxdomain"):
        return Verdict("gone", "이 주소는 이제 존재하지 않습니다. 터널이 꺼졌거나, 서버를 다시 켜서 주소가 바뀌었습니다.", [
            "서버 PC 의 명령 프롬프트 창이 아직 켜져 있는지, 거기에 '터널이 종료되었습니다' 메시지가 없는지 확인하세요.",
            "서버를 다시 켜고(python app.py --tunnel ...) 이번에 출력된 새 주소를 쓰세요. 주소는 켤 때마다 바뀝니다.",
            "이미 나눠 준 링크는 주소 부분(https://○○○.trycloudflare.com)만 새 주소로 바꾸면 그대로 쓸 수 있습니다. 뒤의 /r/…#t=… 부분은 그대로입니다.",
            "서버를 켰는데도 새 주소가 안 열린다면 서버 PC 에서 cloudflared 가 Cloudflare 에 연결하지 못한 것입니다. "
            "--tunnel-protocol http2 를 붙이거나 DNS 를 1.1.1.1 로 바꾸고 다시 시도하세요.",
        ])

    if public_dead and system.status != "ok":
        return Verdict("offline", "인터넷에 연결되어 있지 않거나, 이 네트워크가 DNS(UDP 53)를 막고 있습니다.", [
            "PC 의 인터넷 연결을 확인하세요. 학교/공용망이면 휴대폰 핫스팟으로 바꿔 시도해 보세요.",
        ])

    if http is not None and http.status:
        hints = {
            502: "터널은 열렸지만 서버(app.py)에 연결하지 못했습니다. app.py 가 켜져 있는지, 포트가 맞는지 확인하세요.",
            530: "터널이 끊겼습니다. 서버를 다시 켜서 새 주소를 쓰세요.",
            1033: "터널이 끊겼습니다. 서버를 다시 켜서 새 주소를 쓰세요.",
            429: "동시에 처리 중인 요청이 너무 많습니다(빠른 터널 한도 200건). 잠시 후 다시 시도하세요.",
        }
        return Verdict("http", f"주소는 있으나 서버가 HTTP {http.status} 로 응답했습니다.",
                       [hints.get(http.status, "서버 창의 로그를 확인하세요.")])

    return Verdict("unknown", "원인을 확정하지 못했습니다.", [
        f"세부 결과: 이 PC DNS={system.status}, 공개 DNS={ {k: v.status for k, v in public.items()} }, HTTP={http.detail if http else '시도 안 함'}",
        "이 출력 전체를 복사해 알려 주세요.",
    ])


def _dns_steps() -> list[str]:
    return [
        "Windows 설정 → 네트워크 및 인터넷 → 사용 중인 연결 → DNS 서버 할당 편집 → 수동 → IPv4 켜기 → 기본 1.1.1.1, 보조 8.8.8.8",
        "명령 프롬프트에서 `ipconfig /flushdns` 를 실행하고 브라우저를 완전히 닫았다 다시 여세요. (크롬 주소창: chrome://net-internals/#dns → Clear host cache)",
        "크롬 설정 → 개인정보 및 보안 → 보안 → '보안 DNS 사용' 을 Cloudflare 로 지정해도 됩니다.",
        "급하면 휴대폰 핫스팟 등 다른 네트워크로 접속해 보세요.",
    ]


# ---------------------------------------------------------------- 실행
def host_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if not parsed.hostname:
        raise ValueError(f"주소를 읽을 수 없습니다: {url}")
    return parsed.hostname


def run(url: str, printer=print) -> Verdict:
    host = host_of(url)
    system = resolve_system(host)
    public = {s: resolve_via(s, host) for s in PUBLIC_RESOLVERS}
    ip = next((r.ips[0] for r in public.values() if r.status == "ok"), None) or (system.ips[0] if system.ips else None)
    http = probe_http(host, ip) if ip else None
    def show(r: DnsResult) -> str:
        return {"ok": "찾음 " + ",".join(r.ips), "nxdomain": "없음(NXDOMAIN)", "no_answer": "답 없음", "unreachable": "응답 없음"}[r.status]
    printer(f"주소: {host}")
    printer(f"  이 PC 의 DNS : {show(system)}")
    for s, r in public.items():
        printer(f"  공개 DNS {s}: {show(r)}")
    printer(f"  직접 접속    : {http.detail if http else '(IP 를 알 수 없어 시도하지 않음)'}")
    verdict = diagnose(system, public, http)
    printer(f"\n[진단] {verdict.title}")
    for step in verdict.steps:
        printer(f"  - {step}")
    return verdict
