"""화면 데이터(JSON 꼴)의 변경분 계산.

입찰 한 번에 실제로 바뀌는 것은 현재가·최고 입찰자 정도인데 전체 상태를 다시 보내면 낭비가 크다.
그래서 서버는 "마지막으로 보낸 것"과 "지금 것"의 차이(패치)만 보내고, 클라이언트가 그것을 적용한다.
클라이언트 쪽 구현은 static/js/lib/patch.js 이며 이 파일의 apply() 와 같은 규칙을 따른다.

패치 형식 (JSON 배열, 첫 원소가 종류):
  ["=", 값]                 통째로 교체
  ["o", {키: 패치}, [키]]    객체: 바뀐 키만. 세 번째 원소(없을 수 있음)는 지워진 키 목록
  ["l", {"번호": 패치}]      길이가 같은 목록: 바뀐 칸만
  ["s", k, [값...]]          목록: 앞에서 k개를 버리고 뒤에 값들을 붙인다 (기록처럼 뒤로 쌓이는 목록)
"""
from __future__ import annotations

import json
from typing import Any

SAME: Any = object()        # "바뀐 것 없음" 표시 (패치를 만들지 않는다)


def _leaf_equal(a: Any, b: Any) -> bool:
    # True == 1 이 참인 파이썬과 달리 JSON 에서는 서로 다른 값이므로 타입까지 본다.
    return type(a) is type(b) and a == b


def equal(a: Any, b: Any) -> bool:
    return diff(a, b) is SAME


def _diff_list(a: list, b: list) -> Any:
    by_index: dict[str, Any] | None = None
    if len(a) == len(b):
        by_index = {}
        for i, (x, y) in enumerate(zip(a, b)):
            d = diff(x, y)
            if d is not SAME:
                by_index[str(i)] = d
        if not by_index:
            return SAME
        if len(by_index) * 2 <= len(b):            # 일부 칸만 바뀌었다
            return ["l", by_index]
    # 앞에서 k개가 빠지고 뒤에 새 값이 붙은 꼴인지 본다 (길이가 같아도 한 칸씩 밀린 경우가 있다)
    start = 1 if len(a) == len(b) else 0
    for k in range(start, len(a)):
        keep = len(a) - k
        if keep <= len(b) and all(diff(x, y) is SAME for x, y in zip(a[k:], b[:keep])):
            return ["s", k, b[keep:]]
    if by_index is not None and len(by_index) < len(b):
        return ["l", by_index]
    return ["=", b]


def diff(a: Any, b: Any) -> Any:
    """a 를 b 로 바꾸는 패치. 같으면 SAME."""
    if isinstance(a, dict) and isinstance(b, dict):
        sub: dict[str, Any] = {}
        for key, vb in b.items():
            if key in a:
                d = diff(a[key], vb)
                if d is not SAME:
                    sub[key] = d
            else:
                sub[key] = ["=", vb]
        removed = [key for key in a if key not in b]
        if not sub and not removed:
            return SAME
        return ["o", sub, removed] if removed else ["o", sub]
    if isinstance(a, list) and isinstance(b, list):
        return _diff_list(a, b)
    return SAME if _leaf_equal(a, b) else ["=", b]


def apply(base: Any, patch: Any) -> Any:
    """base 에 패치를 적용한 새 값을 돌려준다 (base 는 바꾸지 않는다). 형식이 맞지 않으면 ValueError."""
    if not isinstance(patch, list) or not patch:
        raise ValueError("bad patch")
    kind = patch[0]
    if kind == "=":
        return patch[1]
    if kind == "o":
        if not isinstance(base, dict):
            raise ValueError("object patch on non-object")
        out = dict(base)
        for key, sub in patch[1].items():
            out[key] = apply(base.get(key), sub)
        for key in (patch[2] if len(patch) > 2 else []):
            out.pop(key, None)
        return out
    if kind == "l":
        if not isinstance(base, list):
            raise ValueError("list patch on non-list")
        out_list = list(base)
        for index, sub in patch[1].items():
            i = int(index)
            if not 0 <= i < len(out_list):
                raise ValueError("index out of range")
            out_list[i] = apply(out_list[i], sub)
        return out_list
    if kind == "s":
        if not isinstance(base, list) or not 0 <= patch[1] <= len(base):
            raise ValueError("bad splice")
        return base[patch[1]:] + list(patch[2])
    raise ValueError("unknown patch kind")


def encoded_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))
