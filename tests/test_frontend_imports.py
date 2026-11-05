"""프런트엔드 모듈의 import 가 실제로 존재하는 export 를 가리키는지 정적으로 검사한다.

(파일을 덮어써서 `Connection` 이 사라지는 사고를 파이썬 테스트에서도 잡기 위한 안전망)
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "static" / "js"
IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'(\.{1,2}/[^']+)'")
EXPORT_RE = re.compile(r"export\s+(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z0-9_$]+)")
EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]*)\}")


def exports_of(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    names = set(EXPORT_RE.findall(text))
    for group in EXPORT_LIST_RE.findall(text):
        names.update(n.split(" as ")[-1].strip() for n in group.split(",") if n.strip())
    return names


def test_every_named_import_exists():
    problems = []
    for js in ROOT.rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        for names, rel in IMPORT_RE.findall(text):
            target = (js.parent / rel).resolve()
            if not target.is_file():
                problems.append(f"{js.relative_to(ROOT)}: 파일 없음 {rel}")
                continue
            have = exports_of(target)
            for name in (n.split(" as ")[0].strip() for n in names.split(",") if n.strip()):
                if name not in have:
                    problems.append(f"{js.relative_to(ROOT)}: {rel} 에 export {name} 없음")
    assert not problems, "\n".join(problems)
