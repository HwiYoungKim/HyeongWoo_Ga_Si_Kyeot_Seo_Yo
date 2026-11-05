"""오버워치 영웅 목록 (서버가 허용하는 값의 기준표).

선수가 고를 수 있는 영웅은 이 목록의 id 뿐이다. 클라이언트가 보낸 임의의 문자열은 저장하지 않는다.
영웅은 시즌마다 추가되므로 새 영웅이 나오면 이 파일에 한 줄만 추가하면 된다.
(영웅 이미지/초상화는 Blizzard 의 저작물이므로 넣지 않고, 이름 텍스트만 사용한다.)

기준: 2026년 시즌 5 로스터 (탱커 15 · 딜러 23 · 힐러 16 = 54명)
"""
from __future__ import annotations

# (id, 표시 이름, 역할)  역할: tank | damage | support
HEROES: tuple[tuple[str, str, str], ...] = (
    # 탱커
    ("dmon", "D.Mon", "tank"),
    ("dva", "D.Va", "tank"),
    ("domina", "Domina", "tank"),
    ("doomfist", "Doomfist", "tank"),
    ("hazard", "Hazard", "tank"),
    ("junker-queen", "Junker Queen", "tank"),
    ("mauga", "Mauga", "tank"),
    ("orisa", "Orisa", "tank"),
    ("ramattra", "Ramattra", "tank"),
    ("reinhardt", "Reinhardt", "tank"),
    ("roadhog", "Roadhog", "tank"),
    ("sigma", "Sigma", "tank"),
    ("winston", "Winston", "tank"),
    ("wrecking-ball", "Wrecking Ball", "tank"),
    ("zarya", "Zarya", "tank"),
    # 딜러
    ("anran", "Anran", "damage"),
    ("ashe", "Ashe", "damage"),
    ("bastion", "Bastion", "damage"),
    ("cassidy", "Cassidy", "damage"),
    ("echo", "Echo", "damage"),
    ("emre", "Emre", "damage"),
    ("freja", "Freja", "damage"),
    ("genji", "Genji", "damage"),
    ("hanzo", "Hanzo", "damage"),
    ("junkrat", "Junkrat", "damage"),
    ("mei", "Mei", "damage"),
    ("pharah", "Pharah", "damage"),
    ("reaper", "Reaper", "damage"),
    ("shion", "Shion", "damage"),
    ("sierra", "Sierra", "damage"),
    ("sojourn", "Sojourn", "damage"),
    ("soldier-76", "Soldier: 76", "damage"),
    ("symmetra", "Symmetra", "damage"),
    ("torbjorn", "Torbjörn", "damage"),
    ("tracer", "Tracer", "damage"),
    ("vendetta", "Vendetta", "damage"),
    ("venture", "Venture", "damage"),
    ("widowmaker", "Widowmaker", "damage"),
    # 힐러(지원)
    ("ana", "Ana", "support"),
    ("baptiste", "Baptiste", "support"),
    ("brigitte", "Brigitte", "support"),
    ("doctrine", "Doctrine", "support"),
    ("illari", "Illari", "support"),
    ("jetpack-cat", "Jetpack Cat", "support"),
    ("juno", "Juno", "support"),
    ("kiriko", "Kiriko", "support"),
    ("lifeweaver", "Lifeweaver", "support"),
    ("lucio", "Lúcio", "support"),
    ("mercy", "Mercy", "support"),
    ("mizuki", "Mizuki", "support"),
    ("moira", "Moira", "support"),
    ("sombra", "Sombra", "support"),
    ("wuyang", "Wuyang", "support"),
    ("zenyatta", "Zenyatta", "support"),
)

HERO_IDS: frozenset[str] = frozenset(h[0] for h in HEROES)
ROLES: tuple[str, ...] = ("tank", "damage", "support")


def hero_list_for_client() -> list[dict[str, str]]:
    return [{"id": i, "name": n, "role": r} for i, n, r in HEROES]
