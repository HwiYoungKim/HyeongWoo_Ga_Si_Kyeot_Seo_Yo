"""테스트 공용 헬퍼."""
import random

from server import engine
from server.models import Room, Settings


def make_room(n_teams=3, members=3, *, settings=None, seed=1, start=False, now=0.0, handicaps=None):
    """N팀, 팀당 members명 구성의 방을 만들고 (선택) 경매를 시작한다."""
    room = Room(
        id="testroom", title="테스트", num_teams=n_teams, members_per_team=members,
        host_hash="h", observer_hash="o", settings=settings or Settings(),
    )
    engine.init_teams(room, [f"hash{i}" for i in range(n_teams)])
    if handicaps:
        engine.apply_teams(room, [
            {"leader_name": f"팀장{i+1}", "leader_position": "", "handicap": handicaps[i]}
            for i in range(n_teams)
        ])
    need = n_teams * (members - 1)
    engine.apply_players(room, [
        {"name": f"선수{i+1}", "position": "", "elo": 1000 + i, "intro": ""} for i in range(need)
    ])
    rng = random.Random(seed)
    if start:
        engine.start_auction(room, now, rng)
    return room, rng
