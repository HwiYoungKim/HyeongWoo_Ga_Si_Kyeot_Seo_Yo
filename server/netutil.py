"""네트워크 관련 작은 유틸."""
from __future__ import annotations

import ipaddress
import socket
from typing import Mapping


def client_ip(environ: Mapping[str, str], trust_proxy: bool) -> str:
    """요청자 IP. trust_proxy 가 켜진 경우에만 X-Forwarded-For 의 마지막 항목(직전 프록시가 본 주소)을 쓴다.

    프록시 없이 직접 노출된 서버에서 X-Forwarded-For 를 믿으면 공격자가 IP 를 위조해
    접속 실패 제한을 우회할 수 있으므로 기본값은 신뢰하지 않는다.
    """
    if trust_proxy:
        xff = environ.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            last = xff.split(",")[-1].strip()
            try:
                ipaddress.ip_address(last)
                return last
            except ValueError:
                pass
    return environ.get("REMOTE_ADDR", "") or "unknown"


def lan_ips() -> list[str]:
    """이 PC의 사설 IPv4 주소 후보 (같은 공유기 안의 다른 사람이 접속할 주소)."""
    found: list[str] = []
    try:
        # 실제 패킷을 보내지 않고, 외부로 나갈 때 쓰이는 인터페이스 주소만 알아낸다.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if ip and not ip.startswith("127.")]
