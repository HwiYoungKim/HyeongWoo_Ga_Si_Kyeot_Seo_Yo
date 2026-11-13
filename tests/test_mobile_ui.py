"""모바일 전용 화면: 감지 스크립트, 스타일 범위, 템플릿 연결, CSP 호환."""
import re
from pathlib import Path

import pytest

from tests.test_server_flow import env  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _selectors(css: str):
    """최상위 규칙의 선택자 목록 (@media 같은 at-rule 은 안쪽까지 펼쳐서 본다)."""
    css = _strip_comments(css)
    out, depth, buf = [], 0, ""
    for ch in css:
        if ch == "{":
            if depth == 0 and not buf.strip().startswith("@"):
                out.append(buf.strip())
            depth += 1 if buf.strip().startswith("@") or depth else 1
            buf = ""
        elif ch == "}":
            depth -= 1
            buf = ""
        else:
            buf += ch
    return out


def test_mobile_css_is_fully_scoped_to_mobile_attribute():
    css = (STATIC / "css" / "mobile.css").read_text(encoding="utf-8")
    sels = _selectors(css)
    assert len(sels) > 40
    for group in sels:
        for sel in (s.strip() for s in group.split(",")):
            assert sel.startswith('html[data-device="mobile"]'), f"PC 화면에 영향을 줄 수 있는 규칙: {sel}"


def test_detect_script_is_classic_and_sets_attributes():
    js = (STATIC / "js" / "device-detect.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", js, flags=re.M)    # <head> 의 일반 스크립트여야 한다
    assert "data-device" in js and "data-orient" in js
    assert "innerHTML" not in js and "eval(" not in js


def test_mobile_assets_do_not_use_inline_style_or_html_injection():
    for rel in ("js/lib/mobile.js", "js/device-detect.js"):
        src = (STATIC / rel).read_text(encoding="utf-8")
        assert "innerHTML" not in src and "setAttribute('style'" not in src and 'setAttribute("style"' not in src


@pytest.mark.parametrize("path", ["/", "/r/abcdef12"])
def test_pages_link_detection_and_mobile_css(env, path):
    r = env.http.get(path)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'viewport-fit=cover' in html and 'interactive-widget=resizes-content' in html
    assert "js/device-detect.js" in html and "css/mobile.css" in html
    # 감지 스크립트가 스타일시트보다 먼저 (화면이 그려지기 전에 속성이 정해진다)
    assert html.index("device-detect.js") < html.index("css/mobile.css")
    assert "<script>" not in html and " style=" not in html            # 인라인 금지 (CSP)


def test_mobile_assets_are_served_with_immutable_cache(env):
    html = env.http.get("/").get_data(as_text=True)
    urls = re.findall(r'(?:src|href)="(/s/[^"]+/(?:css/mobile\.css|js/device-detect\.js))"', html)
    assert len(urls) == 2
    for u in urls:
        r = env.http.get(u)
        assert r.status_code == 200
        assert "immutable" in r.headers["Cache-Control"]


def test_room_page_has_mini_timer_slot(env):
    html = env.http.get("/r/abcdef12").get_data(as_text=True)
    assert 'id="mini-timer"' in html
