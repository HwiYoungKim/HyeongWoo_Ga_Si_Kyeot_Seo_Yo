"""실행 시점에 정해지는 서버 설정 (app.py 의 명령줄 옵션이 채운다)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import config


@dataclass
class AppConfig:
    data_dir: str = "data"
    admin_key: str = ""
    host: str = "127.0.0.1"
    port: int = 5000
    public_url: Optional[str] = None          # 터널(ngrok/cloudflared) 등 외부에 공개된 주소
    lan_ips: list[str] = field(default_factory=list)
    retention_days: float = config.DEFAULT_RETENTION_DAYS
    trust_proxy: bool = False                 # 리버스 프록시/터널 뒤에서만 켠다
    secure: bool = False                      # HTTPS 로 서비스 중인가 (--cert/--key 또는 프록시 TLS 종단)

    @property
    def lan_enabled(self) -> bool:
        return self.host not in ("127.0.0.1", "localhost", "::1")
