// 표시용 문자열 포맷터

export const fmt = (n) => Number(n).toLocaleString('ko-KR');

export const STATE_LABEL = {
  WAITING: '시작 대기',
  MAIN_AUCTION: '정규 경매',
  UNSOLD_AUCTION: '유찰자 재경매',
  AUTO_ASSIGN: '자동 배정',
  BRACKET_SELECT: '대진 선택',
  COMPLETED: '종료',
};

export const ROLE_LABEL = { host: '주최자', leader: '팀장', observer: '관전자' };

export function teamName(teams, id) {
  const t = teams && teams[id];
  return t ? t.leader_name : '?';
}

// 활동 로그 한 줄을 사람이 읽는 문장으로 바꾼다.
export function activityText(e, teams) {
  const tn = (id) => teamName(teams, id);
  switch (e.type) {
    case 'start': return '경매가 시작되었습니다.';
    case 'sold': return `${e.player_name ?? '선수'} → ${tn(e.team)} 팀 낙찰 (${fmt(e.amount)}P)`;
    case 'unsold': return `${e.player_name ?? '선수'} 유찰`;
    case 'unsold_round': return `유찰자 ${e.count}명 재경매를 시작합니다.`;
    case 'auto_assign': return `${e.player_name ?? '선수'} → ${tn(e.team)} 팀 자동 배정 (0P)`;
    case 'forced_stop': return '모든 팀장이 포인트를 소진해 경매를 종료하고, 남은 선수를 자동 배정합니다.';
    case 'forced_sold': return `${tn(e.team)} 팀의 입찰을 넘길 수 있는 팀이 없어 즉시 낙찰합니다.`;
    case 'paused': return e.reason === 'restart' ? '서버 재시작으로 일시정지되었습니다.' : '일시정지';
    case 'resumed': return '경매 재개';
    case 'bracket_start': return '대진 선택을 시작합니다. (잔여 포인트가 많은 팀부터 지명)';
    case 'pair': return `대진: ${tn(e.a)} vs ${tn(e.b)}${e.auto ? ' (자동 확정)' : ''}`;
    case 'bye': return `${tn(e.team)} 팀 부전승`;
    case 'completed': return '모든 편성이 끝났습니다.';
    default: return String(e.type);
  }
}

export function formatTime(ms) {
  const sec = Math.max(0, ms) / 1000;
  return sec >= 10 ? sec.toFixed(0) : sec.toFixed(1);
}

export function clockTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
}
