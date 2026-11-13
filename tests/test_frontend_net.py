"""브라우저 클라이언트(net.js)의 변경분 처리: 순서 보장, 누락 감지, 재접속."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_client_applies_patches_and_recovers_from_gaps():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 가 없어 net.js 를 실행할 수 없습니다")
    out = subprocess.run([node, str(ROOT / "tests" / "js" / "net_protocol.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1])["ok"] is True
