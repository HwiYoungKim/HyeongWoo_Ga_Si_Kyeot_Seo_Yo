// 팀장 화면: 입찰 컨트롤.
// 입력창은 한 번만 만들고 상태가 바뀔 때는 값만 갱신한다 (다른 사람 입찰이 들어와도 입력 중인 숫자가 지워지지 않게).
import { h, replace, toast } from '../lib/dom.js';
import { fmt } from '../lib/format.js';
import { isRunning } from './common.js';

export function mountLeader(root, ctx) {
  const { conn } = ctx;
  const info = h('div', { class: 'leader-info' });
  const hint = h('div', { class: 'sub bid-hint' });
  const result = h('div', { class: 'bid-result', role: 'status' });
  const quickBox = h('div', { class: 'quick-bids' });
  const input = h('input', { type: 'number', inputmode: 'numeric', step: 1, min: 0, placeholder: '직접 입력', 'aria-label': '입찰 금액' });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary', text: '입찰' });
  let dirty = false;
  let lastSeq = null;
  let latest = null;

  async function placeBid(amount) {
    const st = conn.state;
    if (!st || !st.lot) return;
    result.textContent = '';
    const res = await conn.request('bid', { amount, lot_seq: st.lot.seq });
    if (!res.ok) {
      result.textContent = res.message || '입찰하지 못했습니다.';
      result.className = 'bid-result bid-fail';
      if (res.code === 'stale_lot') toast('다음 매물로 넘어갔습니다.', 'info');
    } else {
      result.textContent = '입찰했습니다.';
      result.className = 'bid-result bid-ok';
      dirty = false;
      input.value = '';
    }
  }

  input.addEventListener('input', () => { dirty = true; });
  // novalidate: 브라우저 기본 검증 대신 서버가 판정하고 그 사유를 화면에 보여준다.
  const form = h('form', { class: 'bid-form', novalidate: '', onSubmit: (ev) => {
    ev.preventDefault();
    const n = Number(input.value);
    if (!Number.isInteger(n)) { result.textContent = '정수로 입력해 주세요.'; result.className = 'bid-result bid-fail'; return; }
    placeBid(n);
  } }, input, submit);

  replace(root, h('h2', { text: '내 입찰' }), info, quickBox, form, hint, result);

  function update(state) {
    latest = state;
    const me = state.teams[ctx.teamId];
    const running = isRunning(state);
    const lot = state.lot;
    const iAmHighest = running && lot.highest_team === ctx.teamId;
    const slotsLeft = me.open_slots;
    const canBid = running && !state.paused && !iAmHighest && slotsLeft > 0;

    replace(info,
      h('strong', { text: me.leader_name }),
      h('span', { text: ` · 남은 포인트 ${fmt(me.points)}P` }),
      running ? h('span', { text: ` · 입찰 가능 최대 ${fmt(me.max_bid)}P` }) : null,
      h('span', { text: ` · 빈자리 ${slotsLeft}` }),
    );

    // 매물이 바뀌면 입력 초기화
    if (running && lot.seq !== lastSeq) {
      lastSeq = lot.seq;
      dirty = false;
      input.value = '';
      result.textContent = '';
    }

    if (running) {
      input.min = lot.min_next_bid;
      input.max = me.max_bid;
      if (!dirty) input.placeholder = `${fmt(lot.min_next_bid)} 이상`;
    }
    input.disabled = !canBid;
    submit.disabled = !canBid;

    // 빠른 입찰 버튼 — 눌러서 바로 입찰
    const base = running && lot.highest_team >= 0 ? lot.highest_bid : 0;
    const min = running ? lot.min_next_bid : 0;
    const choices = [{ label: `최소 ${fmt(min)}P`, amount: min }];
    for (const k of [10, 50, 100]) {
      const amount = Math.max(min, base + k);
      if (!choices.some((c) => c.amount === amount)) choices.push({ label: `+${k} (${fmt(amount)}P)`, amount });
    }
    if (running && me.max_bid >= min && !choices.some((c) => c.amount === me.max_bid)) {
      choices.push({ label: `최대 ${fmt(me.max_bid)}P`, amount: me.max_bid });
    }
    replace(quickBox, choices.map((c) => {
      const ok = canBid && c.amount <= me.max_bid;
      return h('button', { type: 'button', class: 'btn', disabled: !ok, onClick: () => placeBid(c.amount), text: c.label });
    }));

    hint.textContent = !running ? '경매가 진행 중일 때 입찰할 수 있습니다.'
      : state.paused ? '일시정지 중입니다.'
      : iAmHighest ? '현재 최고 입찰자입니다. 다른 팀이 입찰하면 다시 입찰할 수 있습니다.'
      : slotsLeft <= 0 ? '팀 정원이 가득 찼습니다.'
      : '';
  }

  return { update };
}
