// 경매방 페이지 진입점.
// 접속 토큰은 URL 의 #t=... (fragment) 에 들어 있다. fragment 는 서버로 전송되지 않으므로
// 서버 로그/프록시/Referer 에 남지 않는다. 토큰은 소켓 'join' 에서 한 번 검증된다.
import { Connection } from './lib/net.js';
import { h, replace, toast, $ } from './lib/dom.js';
import { STATE_LABEL, ROLE_LABEL } from './lib/format.js';
import { loadMeta } from './lib/meta.js';
import * as common from './views/common.js';
import { mountChat } from './views/chat.js';
import { savedNickname } from './views/observer.js';

loadMeta();   // 영웅 목록/제한값을 소켓 접속과 동시에 미리 받아 둔다 (join 이후 대기 시간 제거)

const roomId = location.pathname.split('/').filter(Boolean).pop() || '';
const token = new URLSearchParams(location.hash.slice(1)).get('t') || '';

const el = {
  title: $('#room-title'),
  role: $('#role-badge'),
  stateLabel: $('#state-label'),
  conn: $('#conn-status'),
  stage: $('#stage'),
  rolePanel: $('#role-panel'),
  teams: $('#teams'),
  bracket: $('#bracket'),
  queue: $('#queue'),
  activity: $('#activity'),
  chat: $('#chat'),
};

const timerEl = h('div', { class: 'timer timer-hidden', 'aria-live': 'off' });
const lotBox = h('div', { class: 'lot' });
replace(el.stage, timerEl, lotBox);

function fatal(message) {
  document.body.classList.add('fatal');
  replace($('main'), h('div', { class: 'panel fatal-box' },
    h('h2', { text: '접속할 수 없습니다' }),
    h('p', { text: message }),
    h('p', { class: 'sub', text: '주최자에게 새 링크를 요청해 주세요.' }),
  ));
}

if (!/^[A-Za-z0-9_-]{6,16}$/.test(roomId) || !/^[A-Za-z0-9_-]{20,64}$/.test(token)) {
  fatal('링크에 접속 정보가 없거나 올바르지 않습니다.');
} else {
  start();
}

function start() {
  const conn = new Connection(roomId, token);
  conn.nickname = savedNickname();
  let ctx = null;
  let view = null;
  let chat = null;

  const STATUS = {
    online: ['연결됨', 'conn-ok'],
    offline: ['연결 끊김 — 다시 연결하는 중…', 'conn-bad'],
    joining: ['접속 중…', 'conn-wait'],
  };
  conn.on('status', (s) => {
    const [label, cls] = STATUS[s] || STATUS.offline;
    el.conn.textContent = label;
    el.conn.className = `conn ${cls}`;
  });
  conn.on('toast', (m) => toast(m, 'error'));
  conn.on('fatal', fatal);

  async function pick(opponent) {
    const event = conn.role === 'host' ? 'host_pick' : 'pick_opponent';
    const res = await conn.request(event, { opponent });
    if (!res.ok) toast(res.message || '지명하지 못했습니다.', 'error');
  }

  function render(state) {
    if (!ctx) return;
    document.title = `${state.room.title} — 선수 경매`;
    el.title.textContent = state.room.title;
    el.stateLabel.textContent = STATE_LABEL[state.state] + (state.paused ? ' · 일시정지' : '');
    common.renderLot(lotBox, state, ctx);
    common.renderTeams(el.teams, state, ctx);
    common.renderQueue(el.queue, state);
    common.renderActivity(el.activity, state);
    common.renderBracket(el.bracket, state, ctx);
    if (view) view.update(state);
  }

  conn.on('state', render);

  conn.on('joined', async (info) => {
    if (!info.first) {
      chat?.reset(info.chat);          // 재접속: 놓친 채팅 복구 (상태는 스냅샷으로 이미 갱신됨)
      render(conn.state);
      return;
    }
    await loadMeta();                 // 영웅 이름표/제한값을 받은 뒤에 화면을 만든다
    ctx = { conn, role: info.role, teamId: info.teamId, pick };
    el.role.textContent = ROLE_LABEL[info.role];
    el.role.className = `badge badge-${info.role}`;

    if (info.role === 'host') {
      view = (await import('./views/host.js')).mountHost(el.rolePanel, ctx);
    } else if (info.role === 'leader') {
      view = (await import('./views/leader.js')).mountLeader(el.rolePanel, ctx);
    } else {
      view = (await import('./views/observer.js')).mountObserver(el.rolePanel, ctx);
    }
    chat = mountChat(el.chat, conn, info.chat);
    render(conn.state);
  });

  // 남은 시간 표시는 0.1초 단위로 로컬에서 갱신한다 (서버가 1초마다 보정)
  setInterval(() => {
    if (conn.state) common.renderTimer(timerEl, conn.state, conn.remainingMs());
  }, 100);

  conn.connect();
}
