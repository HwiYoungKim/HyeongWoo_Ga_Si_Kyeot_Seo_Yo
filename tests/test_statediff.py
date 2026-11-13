"""변경분(패치) 계산과 적용: 파이썬 구현, 무작위 검증, 브라우저용 구현(patch.js)과의 일치."""
import copy
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from server import statediff
from server.statediff import SAME, apply, diff

ROOT = Path(__file__).resolve().parent.parent


def roundtrip(a, b):
    p = diff(a, b)
    if p is SAME:
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        return None
    p = json.loads(json.dumps(p))               # 실제로는 JSON 으로 전송된다
    before = copy.deepcopy(a)
    assert apply(a, p) == b
    assert a == before                           # 원본은 바뀌지 않는다
    return p


def test_identical_values_produce_no_patch():
    v = {"a": [1, {"b": None}], "c": "x"}
    assert diff(v, copy.deepcopy(v)) is SAME


def test_bool_and_int_are_different_values():
    assert diff({"a": True}, {"a": 1}) == ["o", {"a": ["=", 1]}]
    assert diff([0], [False]) == ["=", [False]]


def test_object_patch_touches_only_changed_keys():
    a = {"keep": [1, 2, 3], "x": 1, "gone": 0}
    b = {"keep": [1, 2, 3], "x": 2, "new": "n"}
    p = roundtrip(a, b)
    assert p == ["o", {"x": ["=", 2], "new": ["=", "n"]}, ["gone"]]
    assert apply(a, p)["keep"] is a["keep"]      # 안 바뀐 부분은 그대로 재사용


def test_list_patch_by_index_when_few_items_change():
    a = [{"id": i, "points": 1000} for i in range(4)]
    b = copy.deepcopy(a)
    b[2]["points"] = 900
    assert roundtrip(a, b) == ["l", {"2": ["o", {"points": ["=", 900]}]}]


def test_list_append_and_sliding_window():
    assert roundtrip([1, 2], [1, 2, 3, 4]) == ["s", 0, [3, 4]]          # 뒤에 추가
    assert roundtrip([1, 2, 3], [2, 3, 4]) == ["s", 1, [4]]             # 한 칸 밀림 (기록 상한)
    assert roundtrip([1, 2, 3], [2, 3]) == ["s", 1, []]                 # 앞에서 빠짐 (대기열)
    assert roundtrip([1, 2, 3], [9, 8]) == ["=", [9, 8]]                # 관계없는 목록은 교체
    assert roundtrip([], [1]) == ["=", [1]]


def test_type_change_replaces_value():
    assert roundtrip({"lot": None}, {"lot": {"seq": 1}}) == ["o", {"lot": ["=", {"seq": 1}]}]
    assert roundtrip({"lot": {"seq": 1}}, {"lot": None}) == ["o", {"lot": ["=", None]}]


@pytest.mark.parametrize("bad", [None, [], ["?", 1], ["o", {"a": ["=", 1]}], ["l", {"5": ["=", 1]}], ["s", 9, []]])
def test_apply_rejects_malformed_patches(bad):
    with pytest.raises(ValueError):
        apply([1, 2] if bad and bad[0] != "o" else 5, bad)


# ---------------------------------------------------------------- 무작위 검증
def _rand_value(rng, depth=0):
    kind = rng.random()
    if depth > 3 or kind < 0.35:
        return rng.choice([None, True, False, 0, 1, 2, 17, -3, 1.5, "", "a", "팀장", "x" * rng.randint(0, 6)])
    if kind < 0.7:
        return {rng.choice("abcdefgh"): _rand_value(rng, depth + 1) for _ in range(rng.randint(0, 5))}
    return [_rand_value(rng, depth + 1) for _ in range(rng.randint(0, 6))]


def _mutate(rng, v, depth=0):
    """값을 조금 바꾼 사본. 실제 상태 변화와 비슷하게: 일부 키 변경, 목록 끝에 추가, 앞에서 제거 등."""
    v = copy.deepcopy(v)
    r = rng.random()
    if isinstance(v, dict) and v and r < 0.8:
        for key in rng.sample(sorted(v), k=rng.randint(1, min(2, len(v)))):
            if rng.random() < 0.15:
                del v[key]
            else:
                v[key] = _mutate(rng, v[key], depth + 1)
        if rng.random() < 0.2:
            v[rng.choice("ijkl")] = _rand_value(rng, depth + 1)
        return v
    if isinstance(v, list) and r < 0.8:
        op = rng.choice(["append", "slide", "pop_front", "edit", "shuffle"])
        if op == "append":
            v.append(_rand_value(rng, depth + 1))
        elif op == "slide" and v:
            v = v[1:] + [_rand_value(rng, depth + 1)]
        elif op == "pop_front" and v:
            v = v[rng.randint(1, len(v)):]
        elif op == "edit" and v:
            i = rng.randrange(len(v))
            v[i] = _mutate(rng, v[i], depth + 1)
        else:
            rng.shuffle(v)
        return v
    return _rand_value(rng, depth + 1)


def _cases(n, seed):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a = _rand_value(rng)
        b = _mutate(rng, a) if rng.random() < 0.85 else _rand_value(rng)
        out.append((a, b))
    return out


def test_random_patches_reproduce_the_target():
    for a, b in _cases(3000, seed=20261113):
        roundtrip(a, b)


def test_chained_patches_stay_in_sync():
    rng = random.Random(5)
    server = client = _rand_value(rng)
    client = copy.deepcopy(client)
    for _ in range(400):
        nxt = _mutate(rng, server)
        p = diff(server, nxt)
        if p is not SAME:
            client = apply(client, json.loads(json.dumps(p)))
        server = nxt
        assert client == server


def test_browser_implementation_matches_python(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 가 없어 patch.js 를 실행할 수 없습니다")
    cases = []
    for a, b in _cases(600, seed=77):
        p = diff(a, b)
        if p is not SAME:
            cases.append({"a": a, "p": p, "b": b})
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(cases), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "import { isDeepStrictEqual } from 'node:util';\n"
        f"const {{ applyPatch }} = await import({json.dumps((ROOT / 'static/js/lib/patch.js').as_uri())});\n"
        "const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "let bad = 0;\n"
        "for (const c of cases) {\n"
        "  const before = JSON.stringify(c.a);\n"
        "  const out = applyPatch(c.a, c.p);\n"
        "  if (!isDeepStrictEqual(out, c.b) || JSON.stringify(c.a) !== before) bad += 1;\n"
        "}\n"
        # __proto__ 는 실제처럼 JSON.parse 로 만들어야 "자기 키"가 된다 (객체 리터럴에서는 프로토타입 지정 문법이다)
        "const evil = JSON.parse('[\"o\",{\"__proto__\":[\"=\",1]}]');\n"
        "for (const p of [null, [], ['?', 1], ['l', {'5': ['=', 1]}], ['s', 9, []], evil]) {\n"
        "  try { applyPatch(p && p[0] === 'o' ? {} : [1, 2], p); bad += 1; } catch { /* 예외가 나야 정상 */ }\n"
        "}\n"
        "console.log(JSON.stringify({ total: cases.length, bad }));\n",
        encoding="utf-8",
    )
    out = subprocess.run([node, str(script), str(data)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["total"] > 300 and result["bad"] == 0, result


def test_encoded_size_is_compact_json_length():
    assert statediff.encoded_size({"a": [1, 2]}) == len('{"a":[1,2]}')
