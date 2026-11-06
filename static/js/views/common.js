// 모든 역할이 공통으로 보는 화면 조각: 현재 매물, 팀 현황, 대기열, 활동 기록, 대진.
// 서버가 내려준 값만 그리며, 모든 문자열은 textContent 로 들어간다.
import { h, replace } from '../lib/dom.js';
import { fmt, activityText, teamName, STATE_LABEL } from '../lib/format.js';
import { rolesText } from '../lib/meta.js';
import { roleIcons, heroChips, profileLine, gallery } from './profile.js';

const RUNNING = new Set(['MAIN_AUCTION', 'UNSOLD_AUCTION']);
export const isRunning = (state) => RUNNING.has(state.state) && state.lot;

// ---------------------------------------------------------------- 현재 매물
// 매물 카드(이름/프로필/이미지)는 매물이 바뀔 때만 새로 만든다. 입찰이 들어올 때마다 이미지를
// 다시 만들면 깜빡이고 서버에 다시 요청하게 되므로, 입찰 현황 부분만 갱신한다.
export function renderLot(box, state, ctx) {
  if (isRunning(state)) {
    const lot = state.lot;
    const p = lot.player;
    if (box._cardSeq !== lot.seq || !box._live) {
      const live = h('div', { class: 'lot-live' });
      replace(box,
        h('div', { class: 'lot-card' },
          h('div', { class: 'lot-name', text: p.name }),
          profileLine(p),
          h('div', { class: 'hero-line' }, heroChips(p.heroes)),
          h('div', { class: 'lot-meta' }, h('span', { class: 'chip chip-muted', text: STATE_LABEL[state.state] })),
          p.intro ? h('p', { class: 'lot-intro', text: p.intro }) : null,
          gallery(ctx.conn, p.images),
        ),
        live);
      box._cardSeq = lot.seq;
      box._live = live;
    }
    const leader = lot.highest_team >= 0 ? teamName(state.teams, lot.highest_team) : null;
    replace(box._live,
      h('div', { class: 'bid-status' },
        h('div', { class: 'bid-now' },
          h('span', { class: 'label', text: '현재가' }),
          h('span', { class: 'value', text: lot.highest_team >= 0 ? `${fmt(lot.highest_bid)}P` : '입찰 없음' }),
        ),
        h('div', { class: 'bid-leader' },
          h('span', { class: 'label', text: '최고 입찰' }),
          h('span', { class: 'value', text: leader ?? '-' }),
        ),
        h('div', { class: 'bid-min' },
          h('span', { class: 'label', text: '다음 최소' }),
          h('span', { class: 'value', text: `${fmt(lot.min_next_bid)}P` }),
        ),
      ),
      lot.bids.length
        ? h('ol', { class: 'bid-history' },
            [...lot.bids].reverse().slice(0, 5).map((b) =>
              h('li', { text: `${teamName(state.teams, b.team)} · ${fmt(b.amount)}P` })))
        : null,
    );
    return;
  }
  box._cardSeq = null;
  box._live = null;
  const messages = {
    WAITING: '주최자가 경매를 시작하기를 기다리는 중입니다.',
    BRACKET_SELECT: '경매 종료! 잔여 포인트가 많은 팀부터 대진 상대를 지명합니다.',
    COMPLETED: '경매와 대진 편성이 모두 끝났습니다.',
    AUTO_ASSIGN: '남은 선수를 자동 배정하는 중입니다.',
  };
  replace(box, h('div', { class: 'lot-empty', text: messages[state.state] || '' }));
}

// 타이머 문구는 0.1초 단위로 갱신해야 하므로 별도 함수로 분리
export function renderTimer(el, state, remainingMs) {
  if (!isRunning(state)) {
    el.textContent = '';
    el.className = 'timer timer-hidden';
    return;
  }
  const sec = remainingMs / 1000;
  const label = sec >= 10 ? Math.ceil(sec).toString() : sec.toFixed(1);
  el.textContent = state.paused ? `일시정지 ${label}s` : `${label}s`;
  el.className = 'timer' + (state.paused ? ' timer-paused' : sec <= 5 ? ' timer-danger' : '');
}

// ---------------------------------------------------------------- 팀 현황
export function renderTeams(box, state, ctx) {
  const lot = isRunning(state) ? state.lot : null;
  const online = new Set(state.online_teams);
  replace(box,
    h('h2', { text: '팀 현황' }),
    h('div', { class: 'team-grid' },
      state.teams.map((t) => {
        const classes = ['team'];
        if (lot && lot.highest_team === t.id) classes.push('team-leading');
        if (ctx.role === 'leader' && ctx.teamId === t.id) classes.push('team-me');
        if (state.state === 'BRACKET_SELECT' && state.bracket.picker === t.id) classes.push('team-picker');
        const slots = state.room.members_per_team - 1;
        return h('div', { class: classes.join(' ') },
          h('div', { class: 'team-head' },
            h('span', { class: online.has(t.id) ? 'dot dot-on' : 'dot', title: online.has(t.id) ? '접속 중' : '미접속' }),
            h('strong', { text: t.leader_name }),
            t.leader_position ? h('span', { class: 'chip', text: t.leader_position }) : null,
          ),
          h('div', { class: 'team-points' },
            h('span', { class: 'value', text: `${fmt(t.points)}P` }),
            h('span', { class: 'sub', text: `/ ${fmt(t.initial_points)}P` }),
          ),
          h('div', { class: 'team-slots', text: `영입 ${t.roster.length}/${slots}` }),
          h('ul', { class: 'roster' },
            t.roster.map((p) => h('li', {},
              h('span', { text: p.name }),
              ...roleIcons(p),
              h('span', { class: 'sub', text: `${fmt(p.winning_bid)}P` }),
            ))),
        );
      })),
  );
}

// ---------------------------------------------------------------- 대기열
export function renderQueue(box, state) {
  const q = state.queue;
  const registered = state.state === 'WAITING';
  replace(box,
    h('h2', { text: '대기열' }),
    registered
      ? h('p', { text: `등록된 매물 ${q.registered}명 / 필요 ${q.required}명` })
      : h('ul', { class: 'plain' },
          h('li', { text: `정규 대기 ${q.main_count}명` }),
          h('li', { text: `유찰 대기 ${q.unsold_count}명` }),
          q.leftover_count ? h('li', { text: `재경매 유찰 ${q.leftover_count}명 (자동 배정 예정)` }) : null,
        ),
    q.upcoming
      ? h('div', {},
          h('h3', { text: '경매 순서 (공개)' }),
          h('ol', { class: 'upcoming' },
            q.upcoming.map((p) => h('li', { text: rolesText(p) ? `${p.name} · ${rolesText(p)}` : p.name }))))
      : (registered ? null : h('p', { class: 'sub', text: '경매 순서는 비공개입니다.' })),
    registered ? null : renderUnsoldList(q.unsold_players || []),
  );
}

// 유찰자 목록: 경매에서 팔리지 않은 선수들 (재경매 대기 / 재경매에서도 유찰)
function renderUnsoldList(list) {
  return h('div', { class: 'unsold-box' },
    h('h3', { text: `유찰자 목록 (${list.length}명)` }),
    list.length
      ? h('ul', { class: 'unsold-list' }, list.map((p) => h('li', {},
          ...roleIcons(p),
          h('span', { text: p.name }),
          h('span', { class: 'chip chip-sm chip-muted', text: p.round === 'leftover' ? '재경매 유찰' : '재경매 대기' }))))
      : h('p', { class: 'sub', text: '유찰된 선수가 없습니다.' }));
}

// ---------------------------------------------------------------- 활동 기록
export function renderActivity(box, state) {
  const items = [...state.activity].reverse().slice(0, 30);
  replace(box,
    h('h2', { text: '진행 기록' }),
    items.length
      ? h('ul', { class: 'activity' }, items.map((e) => h('li', { text: activityText(e, state.teams) })))
      : h('p', { class: 'sub', text: '아직 기록이 없습니다.' }),
  );
}

// ---------------------------------------------------------------- 대진
export function renderBracket(box, state, ctx) {
  if (state.state !== 'BRACKET_SELECT' && state.state !== 'COMPLETED') {
    replace(box);
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const b = state.bracket;
  const used = new Set([...b.pairs.flat(), ...b.byes]);
  const unpaired = b.order.filter((id) => !used.has(id));
  const picker = b.picker;
  const myTurn = ctx.role === 'leader' && picker === ctx.teamId;
  const hostCanPick = ctx.role === 'host' && picker !== null;

  replace(box,
    h('h2', { text: '대진' }),
    h('ol', { class: 'priority' },
      b.order.map((id) => h('li', {
        class: id === picker ? 'now' : '',
        text: `${teamName(state.teams, id)} (${fmt(state.teams[id].points)}P)`,
      }))),
    b.pairs.length
      ? h('ul', { class: 'pairs' }, b.pairs.map(([a, c]) =>
          h('li', { text: `${teamName(state.teams, a)}  vs  ${teamName(state.teams, c)}` })))
      : null,
    b.byes.length ? h('p', { text: `부전승: ${b.byes.map((id) => teamName(state.teams, id)).join(', ')}` }) : null,
    picker !== null
      ? h('div', { class: 'pick-box' },
          h('p', { text: myTurn ? '상대 팀을 지명하세요.'
            : hostCanPick ? `${teamName(state.teams, picker)} 팀 차례입니다. (주최자가 대신 지명 가능)`
            : `${teamName(state.teams, picker)} 팀이 상대를 지명하는 중입니다.` }),
          (myTurn || hostCanPick)
            ? h('div', { class: 'row' },
                unpaired.filter((id) => id !== picker).map((id) =>
                  h('button', { type: 'button', class: 'btn', onClick: () => ctx.pick(id),
                    text: teamName(state.teams, id) })))
            : null)
      : null,
  );
}
