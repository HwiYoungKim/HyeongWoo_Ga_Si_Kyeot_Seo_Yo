"""정적 파일 전달 최적화.

- 시작할 때 static 폴더의 모든 파일을 한 번 읽어 메모리에 올린다 (매 요청마다 디스크를 보지 않는다).
- 텍스트 파일(js/css/svg)은 gzip 으로 미리 압축해 둔다. 전송량이 보통 1/3~1/4 로 줄어든다.
- 파일 내용으로 만든 버전 값을 주소에 넣는다:  /s/<버전>/js/room.js
  → 내용이 바뀌지 않는 한 브라우저가 1년간 다시 묻지 않는다 (재방문 시 정적 파일 요청 0건).
  JS 안의 상대 경로 import 는 같은 /s/<버전>/ 아래로 풀리므로 모듈 전체가 함께 캐시된다.
- 방 화면이 쓰는 모든 JS 모듈 목록을 계산해 HTML 에 modulepreload 로 넣는다.
  (import 사슬을 따라 한 단계씩 받는 대신 한꺼번에 병렬로 받아 왕복 횟수를 줄인다.)
"""
from __future__ import annotations

import gzip
import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TEXT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
              ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8", ".json": "application/json"}
MIN_GZIP_BYTES = 256

_STATIC_IMPORT_RE = re.compile(r"""(?:import|export)\s[^'"`;]*?from\s*['"](\.{1,2}/[^'"]+)['"]|import\s*['"](\.{1,2}/[^'"]+)['"]""")
_DYNAMIC_IMPORT_RE = re.compile(r"""import\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")


@dataclass
class Asset:
    path: str
    body: bytes
    gz: Optional[bytes]
    etag: str
    mimetype: str


@dataclass
class Assets:
    root: Path
    files: dict[str, Asset] = field(default_factory=dict)
    version: str = "0"
    _graph: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> "Assets":
        self = cls(root=root)
        digest = hashlib.sha256()
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            body = path.read_bytes()
            etag = hashlib.sha256(body).hexdigest()[:16]
            digest.update(rel.encode())
            digest.update(etag.encode())
            suffix = path.suffix.lower()
            mimetype = TEXT_TYPES.get(suffix) or mimetypes.guess_type(rel)[0] or "application/octet-stream"
            gz = None
            if suffix in TEXT_TYPES and len(body) >= MIN_GZIP_BYTES:
                packed = gzip.compress(body, compresslevel=9, mtime=0)
                if len(packed) < len(body):
                    gz = packed
            self.files[rel] = Asset(rel, body, gz, etag, mimetype)
        self.version = digest.hexdigest()[:10]
        self._build_graph()
        return self

    # ---------------------------------------------------------------- 주소
    def url(self, rel: str) -> str:
        if rel not in self.files:
            raise KeyError(f"static 파일이 없습니다: {rel}")
        return f"/s/{self.version}/{rel}"

    def get(self, rel: str) -> Optional[Asset]:
        # 경로 조작 방지: 미리 읽어 둔 파일 목록에 있는 이름만 허용한다.
        return self.files.get(rel)

    # ---------------------------------------------------------------- 모듈 그래프
    def _resolve(self, importer: str, spec: str) -> Optional[str]:
        base = Path(importer).parent
        parts: list[str] = []
        for seg in (base / spec).as_posix().split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(seg)
        rel = "/".join(parts)
        return rel if rel in self.files else None

    def _build_graph(self) -> None:
        self._graph = {}
        for rel, asset in self.files.items():
            if not rel.endswith(".js") or rel.startswith("vendor/"):
                continue
            text = asset.body.decode("utf-8", "replace")
            deps = []
            for m in _STATIC_IMPORT_RE.finditer(text):
                deps.append(m.group(1) or m.group(2))
            for m in _DYNAMIC_IMPORT_RE.finditer(text):
                deps.append(m.group(1))
            self._graph[rel] = [r for r in (self._resolve(rel, d) for d in deps) if r]

    def module_closure(self, entry: str) -> list[str]:
        """entry 모듈이 (정적/동적 import 로) 쓰는 모든 JS 모듈. entry 자신은 제외."""
        seen: list[str] = []
        stack = [entry]
        while stack:
            cur = stack.pop()
            for dep in self._graph.get(cur, []):
                if dep not in seen and dep != entry:
                    seen.append(dep)
                    stack.append(dep)
        return sorted(seen)

    def preload_urls(self, entry: str) -> list[str]:
        return [self.url(r) for r in self.module_closure(entry)]


# --- Flask 연결 -------------------------------------------------------------------

IMMUTABLE = "public, max-age=31536000, immutable"
COMPRESSIBLE_PATHS = ("/", "/api/server-info")


def _accepts_gzip(request) -> bool:
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


def register_static(app, assets: Assets) -> None:
    from flask import Response, abort, request

    @app.get("/s/<version>/<path:rel>")
    def versioned_static(version: str, rel: str):
        asset = assets.get(rel)
        if asset is None:
            abort(404)
        # 오래된 버전 주소(서버가 업데이트된 뒤 열려 있던 탭 등)는 최신 파일을 주되 캐시는 하지 않게 한다.
        cache = IMMUTABLE if version == assets.version else "no-cache"
        etag = f'"{asset.etag}"'
        headers = {"ETag": etag, "Cache-Control": cache, "Vary": "Accept-Encoding"}
        if etag in request.headers.get("If-None-Match", ""):
            return Response(status=304, headers=headers)
        body = asset.body
        if asset.gz is not None and _accepts_gzip(request):
            body = asset.gz
            headers["Content-Encoding"] = "gzip"
        resp = Response(body, mimetype=asset.mimetype.split(";")[0], headers=headers)
        if "charset" in asset.mimetype:
            resp.headers["Content-Type"] = asset.mimetype
        resp.headers["Content-Length"] = str(len(body))
        return resp

    @app.context_processor
    def _inject():
        return {"asset": assets.url, "preload_modules": assets.preload_urls}

    @app.after_request
    def _compress_dynamic(resp):
        """HTML 페이지와 서버 정보(JSON)만 gzip 한다. 토큰/키가 담긴 응답(방 생성 등)은 대상이 아니다."""
        path = request.path
        is_page = path in COMPRESSIBLE_PATHS or path.startswith("/r/")
        if (not is_page or resp.status_code != 200 or resp.direct_passthrough
                or "Content-Encoding" in resp.headers or not _accepts_gzip(request)):
            return resp
        data = resp.get_data()
        if len(data) < 600:
            return resp
        packed = gzip.compress(data, compresslevel=6, mtime=0)
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        resp.headers.add("Vary", "Accept-Encoding")
        return resp
