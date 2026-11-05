"""서버를 켠 직후, 외부 공개 주소(--public-url / --tunnel)로 실제 접속이 되는지 스스로 확인한다.

이 PC 에서 공개 주소로 /api/server-info 를 한 번 요청해 본다. 성공하면 "터널 → 서버" 경로는 정상이다.
(이 PC 의 인터넷에서 본 결과이므로 참가자 쪽 네트워크/DNS 문제까지 보장하지는 않는다.)
비밀 정보는 보내지 않는다.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

HINTS = {
    502: "터널은 열렸지만 서버에 연결하지 못했습니다. 서버 포트와 터널 포트가 같은지, 터널이 127.0.0.1 을 가리키는지 확인하세요.",
    530: "터널이 끊겼거나 주소가 바뀌었습니다. 터널을 다시 켜고 새 주소를 사용하세요.",
    1033: "터널이 끊겼거나 주소가 바뀌었습니다. 터널을 다시 켜고 새 주소를 사용하세요.",
    404: "서버가 아닌 곳으로 연결되었습니다. 주소를 다시 확인하세요.",
}


def check_once(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    req = urllib.request.Request(url.rstrip("/") + "/api/server-info",
                                 headers={"User-Agent": "Mozilla/5.0 (auction-selfcheck)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, HINTS.get(e.code, f"HTTP {e.code}")
    except Exception as e:                      # DNS 실패, 시간 초과 등
        return False, f"{type(e).__name__}: {e}"


def run_in_background(url: str, *, attempts: int = 8, delay: float = 4.0, printer=print) -> threading.Thread:
    def work() -> None:
        last = ""
        for _ in range(attempts):
            ok, detail = check_once(url)
            if ok:
                printer(f"[확인] 외부 주소로 접속이 됩니다: {url}")
                return
            last = detail
            time.sleep(delay)
        printer(f"[경고] 외부 주소로 접속하지 못했습니다: {url}\n       사유: {last}\n"
                "       - 터널 창이 켜져 있는지, 주소가 이번에 받은 것인지 확인하세요.\n"
                "       - 이 PC 에서도 안 열리면 DNS 를 1.1.1.1 / 8.8.8.8 로 바꿔 보세요.")

    t = threading.Thread(target=work, name="selfcheck", daemon=True)
    t.start()
    return t
