"""선수 이미지 저장소.

업로드된 파일을 그대로 저장하지 않는다. 서버가 디코딩해서 새 WebP 로 다시 만든다.
- 파일 안에 숨은 것(EXIF 위치정보, 카메라 정보, 덧붙은 데이터, 스크립트)이 전부 사라진다.
- "이미지인 척하는 파일"(HTML/SVG/실행파일 등)은 디코딩에 실패해서 거부된다. SVG 는 아예 받지 않는다.
- 해상도 상한(이미지 폭탄 방지)과 긴 변 1600px 제한으로 용량을 줄인다.
- 파일 이름은 서버가 만든 무작위 id 뿐이다 (사용자가 준 이름/경로는 쓰지 않는다 → 경로 조작 불가).
- 폴더 0700 / 파일 0600 (POSIX).
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .validators import IMAGE_ID_RE

log = logging.getLogger("auction.images")

Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS
_ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]


class ImageError(Exception):
    """사용자에게 그대로 보여줘도 안전한 메시지를 가진 이미지 처리 오류."""

    def __init__(self, message: str, code: str = "bad_image"):
        super().__init__(message)
        self.message = message
        self.code = code


class ImageStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.dir = Path(root).resolve() / "images"
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 조회
    def _path(self, image_id: str) -> Path:
        if not IMAGE_ID_RE.match(image_id):
            raise ValueError("invalid image id")
        return self.dir / f"{image_id}.webp"

    def path_for(self, image_id: str) -> Optional[Path]:
        if not isinstance(image_id, str) or not IMAGE_ID_RE.match(image_id):
            return None
        path = self._path(image_id)
        return path if path.is_file() else None

    def exists(self, image_id: str) -> bool:
        return self.path_for(image_id) is not None

    def ages(self) -> dict[str, float]:
        """{image_id: 마지막 수정 이후 경과 초}"""
        now = time.time()
        out: dict[str, float] = {}
        for path in self.dir.glob("*.webp"):
            if IMAGE_ID_RE.match(path.stem):
                try:
                    out[path.stem] = now - path.stat().st_mtime
                except OSError:
                    continue
        return out

    # ------------------------------------------------------------------ 저장/삭제
    def save_upload(self, raw: bytes) -> str:
        """업로드 원본 바이트를 검증·재인코딩해 저장하고 id 를 돌려준다."""
        if not raw:
            raise ImageError("빈 파일입니다.")
        if len(raw) > config.MAX_UPLOAD_BYTES:
            raise ImageError("이미지가 너무 큽니다.", "too_large")
        encoded = self._reencode(raw)
        with self._lock:
            if len(list(self.dir.glob("*.webp"))) >= config.MAX_STORED_IMAGES:
                raise ImageError("저장할 수 있는 이미지 수 한도에 도달했습니다.", "image_limit")
            image_id = secrets.token_urlsafe(12)
            while self._path(image_id).exists():
                image_id = secrets.token_urlsafe(12)
            self._write(self._path(image_id), encoded)
        return image_id

    @staticmethod
    def _reencode(raw: bytes) -> bytes:
        try:
            img = Image.open(io.BytesIO(raw), formats=_ALLOWED_FORMATS)
            width, height = img.size                       # 헤더만 읽은 상태에서 크기 검사
            if width < 1 or height < 1 or width * height > config.MAX_IMAGE_PIXELS:
                raise ImageError("이미지 해상도가 너무 큽니다.", "too_large")
            img.seek(0)                                    # 움직이는 GIF 는 첫 프레임만 사용
            img.load()
            img = ImageOps.exif_transpose(img)             # 회전 정보를 픽셀에 반영한 뒤 메타데이터는 버린다
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=82, method=4)   # 새로 만든 파일: EXIF/ICC 등 없음
            return out.getvalue()
        except ImageError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, EOFError,
                SyntaxError, MemoryError):
            raise ImageError("지원하지 않거나 손상된 이미지입니다. (JPG, PNG, WebP, GIF 만 가능)") from None

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, image_id: str) -> bool:
        try:
            self._path(image_id).unlink()
            return True
        except (FileNotFoundError, ValueError):
            return False
        except OSError:
            log.warning("이미지 삭제 실패: %s", image_id)
            return False
