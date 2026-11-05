"""클라이언트 입력 검증/정제.

원칙: 서버는 클라이언트가 보낸 모든 값을 신뢰하지 않는다.
- 타입을 엄격히 확인한다 (bool 은 int 가 아니다. 문자열 "10" 도 정수로 받지 않는다).
- 길이/범위를 제한한다.
- 제어문자와 방향 전환 문자(BiDi override: 화면상 이름을 속이는 데 쓰임)를 제거한다.
- HTML 이스케이프는 여기서 하지 않는다. 표시 단계(클라이언트의 textContent)에서 처리하는 것이
  이중 이스케이프 없이 안전하다. 프론트는 innerHTML 을 쓰지 않는다.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional

from . import config

# 제어문자(탭/개행 제외 -> 아래에서 공백으로 치환), 제로폭 문자, BiDi 제어 문자
_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]"
)
_SPACE_RE = re.compile(r"[ \t\r\n   ]+")

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,16}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


class ValidationError(Exception):
    """사용자에게 그대로 보여줘도 안전한 메시지를 가진 검증 오류."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def clean_text(
    value: Any,
    max_len: int,
    *,
    field: str = "값",
    required: bool = False,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field}: 문자열이어야 합니다.")
    text = unicodedata.normalize("NFC", value)
    text = _CONTROL_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if required and not text:
        raise ValidationError(f"{field}을(를) 입력해 주세요.")
    if len(text) > max_len:
        raise ValidationError(f"{field}은(는) {max_len}자 이하여야 합니다.")
    return text


def clean_int(
    value: Any,
    lo: int,
    hi: int,
    *,
    field: str = "값",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field}: 정수여야 합니다.")
    if value < lo or value > hi:
        raise ValidationError(f"{field}: {lo} ~ {hi} 범위여야 합니다.")
    return value


def clean_float(value: Any, lo: float, hi: float, *, field: str = "값") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field}: 숫자여야 합니다.")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):   # NaN / inf 차단
        raise ValidationError(f"{field}: 올바른 숫자가 아닙니다.")
    if value < lo or value > hi:
        raise ValidationError(f"{field}: {lo} ~ {hi} 범위여야 합니다.")
    return value


def clean_bool(value: Any, *, field: str = "값") -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field}: true/false 여야 합니다.")
    return value


def is_valid_room_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ROOM_ID_RE.match(value))


def is_valid_token(value: Any) -> bool:
    return isinstance(value, str) and bool(TOKEN_RE.match(value))


# --- 경매 설정 -------------------------------------------------------------------

def parse_settings_patch(patch: Any) -> dict[str, Any]:
    """설정 일부 변경 요청을 검증해 {키: 값} 으로 돌려준다. 모르는 키는 거부한다."""
    if not isinstance(patch, dict):
        raise ValidationError("설정 형식이 올바르지 않습니다.")
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "base_points":
            out[key] = clean_int(value, *config.POINTS_RANGE, field="기본 포인트")
        elif key == "bid_mode":
            if value not in ("FIXED", "RATIO"):
                raise ValidationError("입찰 방식은 FIXED 또는 RATIO 여야 합니다.")
            out[key] = value
        elif key == "fixed_min_increment":
            out[key] = clean_int(value, *config.FIXED_INCREMENT_RANGE, field="고정 입찰 단위")
        elif key == "ratio_min_increment":
            out[key] = clean_float(value, *config.RATIO_INCREMENT_RANGE, field="비율 입찰 단위")
        elif key == "min_first_bid":
            out[key] = clean_int(value, *config.POINTS_RANGE, field="첫 입찰 최소 금액")
        elif key == "reserve_per_slot":
            out[key] = clean_int(value, *config.RESERVE_RANGE, field="빈자리당 유보 포인트")
        elif key == "base_bid_time_sec":
            out[key] = clean_int(value, *config.BASE_TIME_RANGE, field="기본 입찰 시간")
        elif key == "extension_time_sec":
            out[key] = clean_int(value, *config.EXTENSION_RANGE, field="연장 시간")
        elif key == "order_public":
            out[key] = clean_bool(value, field="경매 순서 공개")
        else:
            raise ValidationError(f"알 수 없는 설정 항목입니다: {clean_text(str(key), 30)}")
    return out


def parse_room_title(value: Any) -> str:
    return clean_text(value, config.TITLE_LEN, field="방 제목", required=True)


def parse_team_entries(entries: Any, expected: int) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) != expected:
        raise ValidationError(f"팀 정보는 정확히 {expected}개여야 합니다.")
    out = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"{i}번 팀 정보 형식이 올바르지 않습니다.")
        out.append({
            "leader_name": clean_text(entry.get("leader_name"), config.NAME_LEN,
                                      field=f"{i}번 팀장 이름", required=True),
            "leader_position": clean_text(entry.get("leader_position"), config.POSITION_LEN,
                                          field=f"{i}번 팀장 포지션"),
            "handicap": clean_int(entry.get("handicap", 0), *config.HANDICAP_RANGE,
                                  field=f"{i}번 팀장 핸디캡"),
        })
    return out


def parse_player_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValidationError("매물 목록 형식이 올바르지 않습니다.")
    if len(entries) > config.MAX_PLAYERS:
        raise ValidationError(f"매물은 최대 {config.MAX_PLAYERS}명까지 등록할 수 있습니다.")
    out = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValidationError(f"{i}번 매물 형식이 올바르지 않습니다.")
        elo: Optional[int] = entry.get("elo")
        if elo is not None and elo != "":
            elo = clean_int(elo, *config.ELO_RANGE, field=f"{i}번 매물 Elo")
        else:
            elo = None
        out.append({
            "name": clean_text(entry.get("name"), config.NAME_LEN,
                               field=f"{i}번 매물 이름", required=True),
            "position": clean_text(entry.get("position"), config.POSITION_LEN,
                                   field=f"{i}번 매물 포지션"),
            "elo": elo,
            "intro": clean_text(entry.get("intro"), config.INTRO_LEN,
                                field=f"{i}번 매물 소개"),
        })
    return out
