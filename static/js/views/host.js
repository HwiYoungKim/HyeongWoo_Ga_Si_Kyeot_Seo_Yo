// 주최자 화면: 진행 조작, 경매 설정/매물 등록, 링크 재발급, 방 삭제.
// 입력 폼은 한 번만 만들고 서버 값으로 채운다 (상태 갱신이 입력 중인 내용을 덮어쓰지 않도록).
import { h, replace, toast, copyText } from '../lib/dom.js';
import { fmt } from '../lib/format.js';
import { isRunning } from './common.js';

export function mountHost(root, ctx) {
  const { conn } = ctx;
  let initialized = false;
  let dirty = false;
  let latest = null;

  const act = async (event, payload = {}) => {
    const res = await conn.request(event, payload);
    if (!res.ok) toast(res.message || '실패했습니다.', 'error');
    return res;
  };

  // ------------------------------------------------------------ 진행 조작
  const btnStart = h('button', { type: 'button', class: 'btn btn-primary', text: '경매 시작', onClick: onStart });
  const btnPause = h('button', { type: 'button', class: 'btn', text: '일시정지', onClick: () => act('host_pause') });
  const btnResume = h('button', { type: 'button', class: 'btn btn-primary', text: '재개', onClick: () => act('host_resume') });
  const btnSold = h('button', { type: 'button', class: 'btn', text: '강제 낙찰', onClick: () => act('host_force_sold') });
  const btnUnsold = h('button', { type: 'button', class: 'btn btn-warn', text: '유찰 처리', onClick: () => act('host_force_unsold') });
  const status = h('p', { class: 'sub' });
  const controls = h('div', { class: 'host-controls' },
    h('div', { class: 'row' }, btnStart, btnPause, btnResume, btnSold, btnUnsold), status);

  // ------------------------------------------------------------ 설정 폼
  const f = {};   // 필드 참조 모음
  const numField = (key, label, min, max, hint) => {
    const input = h('input', { type: 'number', step: 1, min, max, inputmode: 'numeric' });
    f[key] = input;
    return h('label', { class: 'field' }, h('span', { text: label }), input, hint ? h('small', { class: 'sub', text: hint }) : null);
  };

  f.bid_mode = h('select', {}, h('option', { value: 'FIXED', text: '고정 금액 단위' }), h('option', { value: 'RATIO', text: '비율(%) 단위' }));
  f.order_public = h('select', {}, h('option', { value: 'false', text: '비공개 (BLIND)' }), h('option', { value: 'true', text: '공개 (PUBLIC)' }));
  const fixedField = numField('fixed_min_increment', '최소 입찰 단위(P)', 1, 100000);
  const ratioField = numField('ratio_percent', '최소 입찰 비율(%)', 1, 100, '현재가의 N% 이상 (올림)');
  const modeSync = () => {
    fixedField.hidden = f.bid_mode.value !== 'FIXED';
    ratioField.hidden = f.bid_mode.value !== 'RATIO';
  };
  f.bid_mode.addEventListener('change', modeSync);

  const settingsForm = h('div', { class: 'form-grid' },
    numField('members_per_team', '팀당 인원 (팀장 포함)', 2, 10),
    numField('base_points', '팀장 기본 포인트', 0, 1000000),
    h('label', { class: 'field' }, h('span', { text: '입찰 단위 방식' }), f.bid_mode),
    fixedField, ratioField,
    numField('min_first_bid', '첫 입찰 최소 금액(P)', 0, 1000000),
    numField('base_bid_time_sec', '기본 입찰 시간(초)', 3, 120),
    numField('extension_time_sec', '입찰 시 연장 시간(초)', 0, 120, '0이면 연장 없음(순발력 모드)'),
    numField('reserve_per_slot', '빈자리당 유보 포인트', 0, 100000, '0이면 사용 안 함. 정원을 못 채우는 상황 방지용'),
    h('label', { class: 'field' }, h('span', { text: '경매 순서' }), f.order_public),
  );

  // 팀장 표
  const teamRows = [];
  const teamTable = h('div', { class: 'table-like' });
  function buildTeamRows(count) {
    teamRows.length = 0;
    const rows = [];
    for (let i = 0; i < count; i++) {
      const row = {
        name: h('input', { type: 'text', maxlength: 20, placeholder: `팀장 ${i + 1}` }),
        pos: h('input', { type: 'text', maxlength: 20, placeholder: '포지션' }),
        handicap: h('input', { type: 'number', step: 1, placeholder: '핸디캡(±P)', value: 0 }),
      };
      teamRows.push(row);
      rows.push(h('div', { class: 'row-grid team-row' }, h('span', { class: 'idx', text: String(i + 1) }), row.name, row.pos, row.handicap));
    }
    replace(teamTable, rows);
  }

  // 매물 표
  const playerRows = [];
  const playerTable = h('div', { class: 'table-like' });
  const counter = h('strong', { class: 'counter' });
  function addPlayerRow(p = {}) {
    const row = {
      name: h('input', { type: 'text', maxlength: 20, placeholder: '이름(닉네임)', value: p.name ?? '' }),
      pos: h('input', { type: 'text', maxlength: 20, placeholder: '포지션', value: p.position ?? '' }),
      elo: h('input', { type: 'number', step: 1, min: 0, max: 9999, placeholder: 'Elo', value: p.elo ?? '' }),
      intro: h('input', { type: 'text', maxlength: 128, placeholder: '소개(선택)', value: p.intro ?? '' }),
    };
    row.el = h('div', { class: 'row-grid player-row' }, row.name, row.pos, row.elo, row.intro,
      h('button', { type: 'button', class: 'btn btn-sm', text: '삭제', onClick: () => {
        playerRows.splice(playerRows.indexOf(row), 1);
        row.el.remove();
        dirty = true;
        updateCounter();
      } }));
    playerRows.push(row);
    playerTable.appendChild(row.el);
    updateCounter();
  }
  function setPlayers(list) {
    playerRows.length = 0;
    replace(playerTable);
    for (const p of list) addPlayerRow(p);
    updateCounter();
  }
  function requiredCount() {
    const m = Number(f.members_per_team.value) || 2;
    return (latest ? latest.room.num_teams : 0) * (m - 1);
  }
  function updateCounter() {
    const filled = playerRows.filter((r) => r.name.value.trim()).length;
    const need = requiredCount();
    counter.textContent = `등록 ${filled}명 / 필요 ${need}명`;
    counter.className = 'counter ' + (filled === need ? 'ok' : 'bad');
  }

  const paste = h('textarea', { rows: 4, placeholder: '한 줄에 한 명: 이름, 포지션, Elo, 소개 (쉼표 또는 탭으로 구분)\n예) 홍길동, MID, 1500, 서포터 가능' });
  const pasteBtn = h('button', { type: 'button', class: 'btn', text: '붙여넣은 목록 추가', onClick: () => {
    for (const line of paste.value.split('\n')) {
      if (!line.trim()) continue;
      const parts = line.split(/\t|,/).map((s) => s.trim());
      const elo = parts[2] !== undefined && parts[2] !== '' && !Number.isNaN(Number(parts[2])) ? Number(parts[2]) : '';
      addPlayerRow({ name: parts[0], position: parts[1] ?? '', elo, intro: parts.slice(3).join(', ') });
    }
    paste.value = '';
    dirty = true;
    updateCounter();
  } });

  const saveBtn = h('button', { type: 'button', class: 'btn btn-primary', text: '설정 저장', onClick: () => save() });

  const setupBox = h('div', { class: 'host-setup' },
    h('h3', { text: '1. 경매 설정' }), settingsForm,
    h('h3', { text: '2. 팀장 (이름 · 포지션 · 시작 포인트 가감)' }), teamTable,
    h('h3', { text: '3. 매물(선수) 등록' }),
    h('p', {}, counter, h('span', { class: 'sub', text: '  매물 수 = 팀 수 × (팀당 인원 − 1)' })),
    playerTable,
    h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn', text: '행 추가', onClick: () => { addPlayerRow(); dirty = true; } }),
      h('button', { type: 'button', class: 'btn', text: '전체 비우기', onClick: () => { setPlayers([]); dirty = true; } })),
    paste, pasteBtn,
    h('div', { class: 'row save-row' }, saveBtn),
  );
  setupBox.addEventListener('input', () => { dirty = true; updateCounter(); });

  function collect() {
    const int = (el) => Number(el.value === '' ? 0 : el.value);
    return {
      members_per_team: int(f.members_per_team),
      settings: {
        base_points: int(f.base_points),
        bid_mode: f.bid_mode.value,
        fixed_min_increment: int(f.fixed_min_increment),
        ratio_min_increment: int(f.ratio_percent) / 100,
        min_first_bid: int(f.min_first_bid),
        base_bid_time_sec: int(f.base_bid_time_sec),
        extension_time_sec: int(f.extension_time_sec),
        reserve_per_slot: int(f.reserve_per_slot),
        order_public: f.order_public.value === 'true',
      },
      teams: teamRows.map((r) => ({
        leader_name: r.name.value, leader_position: r.pos.value, handicap: int(r.handicap),
      })),
      players: playerRows.filter((r) => r.name.value.trim()).map((r) => ({
        name: r.name.value, position: r.pos.value,
        elo: r.elo.value === '' ? null : Number(r.elo.value), intro: r.intro.value,
      })),
    };
  }

  function fill(state) {
    const s = state.settings;
    f.members_per_team.value = state.room.members_per_team;
    f.base_points.value = s.base_points;
    f.bid_mode.value = s.bid_mode;
    f.fixed_min_increment.value = s.fixed_min_increment;
    f.ratio_percent.value = Math.round(s.ratio_min_increment * 100);
    f.min_first_bid.value = s.min_first_bid;
    f.base_bid_time_sec.value = s.base_bid_time_sec;
    f.extension_time_sec.value = s.extension_time_sec;
    f.reserve_per_slot.value = s.reserve_per_slot;
    f.order_public.value = String(s.order_public);
    modeSync();
    if (teamRows.length !== state.teams.length) buildTeamRows(state.teams.length);
    state.teams.forEach((t, i) => {
      teamRows[i].name.value = t.leader_name;
      teamRows[i].pos.value = t.leader_position;
      teamRows[i].handicap.value = t.handicap ?? 0;
    });
    setPlayers(state.players || []);
    dirty = false;
  }

  async function save() {
    const res = await act('host_setup', collect());
    if (res.ok) {
      toast('저장했습니다.', 'ok');
      if (conn.state) fill(conn.state);   // 서버가 정리한 값으로 다시 채움
    }
    return res.ok;
  }

  async function onStart() {
    if (dirty && !(await save())) return;
    await act('host_start');
  }

  // ------------------------------------------------------------ 링크 재발급
  const baseInput = h('input', { type: 'text', list: 'base-urls', placeholder: 'https://… 또는 http://192.168.0.x:5000' });
  const baseList = h('datalist', { id: 'base-urls' });
  baseInput.value = location.origin;
  const issuedBox = h('div', { class: 'issued' });
  const reissueRow = h('div', { class: 'row wrap' });
  const linksBox = h('details', { class: 'panel-sub' },
    h('summary', { text: '참가자 링크 재발급' }),
    h('p', { class: 'sub', text: '링크는 방을 만들 때 한 번만 표시됩니다. 분실하거나 유출됐다면 재발급하세요. 이전 링크는 즉시 무효가 되고 접속 중이던 사람은 끊깁니다.' }),
    h('label', { class: 'field' }, h('span', { text: '링크에 쓸 서버 주소' }), baseInput, baseList),
    reissueRow,
    issuedBox,
  );
  fetch('/api/server-info').then((r) => r.json()).then((info) => {
    if (!info.ok) return;
    for (const u of info.urls) baseList.appendChild(h('option', { value: u.url, label: u.label }));
    if (info.urls.length && info.lan_enabled) baseInput.value = info.urls[0].url;
  }).catch(() => {});

  function buildLink(token) {
    let origin = location.origin;
    try {
      const u = new URL(baseInput.value.trim());
      if (u.protocol === 'http:' || u.protocol === 'https:') origin = u.origin;
    } catch { /* 잘못된 입력이면 현재 주소를 사용 */ }
    return `${origin}/r/${conn.roomId}#t=${token}`;
  }

  async function reissue(target, teamId, label) {
    const payload = target === 'leader' ? { target, team_id: teamId } : { target };
    const res = await act('host_reissue', payload);
    if (!res.ok) return;
    const link = buildLink(res.token);
    issuedBox.prepend(h('div', { class: 'issued-row' },
      h('strong', { text: `${label} (새 링크)` }),
      h('input', { type: 'text', readOnly: true, value: link, onFocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn btn-sm', text: '복사', onClick: async () => toast((await copyText(link)) ? '복사했습니다.' : '복사하지 못했습니다.', 'ok') }),
    ));
  }

  // ------------------------------------------------------------ 방 삭제
  const delInput = h('input', { type: 'text', placeholder: 'DELETE 입력', autocomplete: 'off' });
  const dangerBox = h('details', { class: 'panel-sub danger' },
    h('summary', { text: '방 영구 삭제' }),
    h('p', { class: 'sub', text: '선수 정보를 포함한 방 데이터가 서버에서 즉시 완전히 삭제되고 모든 참가자가 끊깁니다. 되돌릴 수 없습니다.' }),
    h('div', { class: 'row' }, delInput,
      h('button', { type: 'button', class: 'btn btn-warn', text: '삭제', onClick: async () => {
        if (delInput.value !== 'DELETE') { toast('DELETE 를 정확히 입력해 주세요.', 'error'); return; }
        await act('host_delete_room', { confirm: 'DELETE' });
      } })),
  );

  replace(root, h('h2', { text: '주최자 패널' }), controls, setupBox, linksBox, dangerBox);

  // ------------------------------------------------------------ 갱신
  function update(state) {
    latest = state;
    const running = isRunning(state);
    if (!initialized) {
      buildTeamRows(state.teams.length);
      replace(reissueRow,
        h('button', { type: 'button', class: 'btn btn-sm', text: '관전자 링크 재발급', onClick: () => reissue('observer', null, '관전자 공용') }),
        state.teams.map((t) => h('button', { type: 'button', class: 'btn btn-sm', text: `팀 ${t.id + 1} 링크 재발급`,
          onClick: () => reissue('leader', t.id, `팀 ${t.id + 1}`) })));
      fill(state);
      initialized = true;
    }
    const waiting = state.state === 'WAITING';
    setupBox.hidden = !waiting;
    btnStart.hidden = !waiting;
    btnPause.hidden = !(running && !state.paused);
    btnResume.hidden = !(running && state.paused);
    btnSold.hidden = !(running && state.lot.highest_team >= 0);
    btnUnsold.hidden = !running;

    const online = state.online_teams.length;
    status.textContent = waiting
      ? `팀장 접속 ${online}/${state.teams.length}명. 설정을 마치고 시작을 누르세요.`
      : running ? `팀장 접속 ${online}/${state.teams.length}명 · 남은 매물 ${state.queue.main_count + state.queue.unsold_count + state.queue.leftover_count}명`
      : '';
    if (waiting) updateCounter();
  }

  return { update };
}
