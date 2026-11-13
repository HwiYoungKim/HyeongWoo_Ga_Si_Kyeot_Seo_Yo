// static/js/lib/net.js 의 변경분(patch) 처리 검증. 가짜 소켓으로 서버 메시지를 흉내 낸다.
// 실행: node tests/js/net_protocol.mjs  (pytest 가 tests/test_frontend_net.py 에서 호출한다)
import assert from 'node:assert/strict';

const handlers = {};
const requests = [];       // 클라이언트가 보낸 요청: {event, payload, reply}
const socket = {
  connected: true,
  on(event, fn) { handlers[event] = fn; },
  timeout() {
    return { emit(event, payload, cb) { requests.push({ event, payload, reply: (ack) => cb(null, ack) }); } };
  },
  disconnect() { this.connected = false; },
};
globalThis.window = { io: () => socket };

const { Connection } = await import('../../static/js/lib/net.js');
const tick = () => new Promise((r) => setTimeout(r, 0));
const base = (seq, bid = 0) => ({ seq, rev: seq, paused: false, remaining_ms: 15000, lot: { seq: 1, highest_bid: bid }, teams: [{ id: 0, points: 1000 }], activity: [] });
const bidPatch = (from, bid) => ({ base: from, seq: from + 1, remaining_ms: 15000, p: ['o', { rev: ['=', from + 1], lot: ['o', { highest_bid: ['=', bid] }] }] });
const take = (event) => { const i = requests.findIndex((r) => r.event === event); assert.notEqual(i, -1, `${event} 요청이 없다`); return requests.splice(i, 1)[0]; };
const count = (event) => requests.filter((r) => r.event === event).length;

const conn = new Connection('room1234', 't'.repeat(24));
const seen = [];
conn.on('state', (s) => seen.push(s.seq));
conn.connect();

// 1) 접속 응답보다 먼저 온 patch 는 보관했다가 스냅샷 뒤에 이어 적용한다
handlers.connect();
handlers.patch(bidPatch(5, 10));                 // 아직 상태가 없다
take('join').reply({ ok: true, role: 'observer', team_id: null, nickname: 'n', snapshot: base(5), chat: [] });
await tick();
assert.equal(conn.state.seq, 6);
assert.equal(conn.state.lot.highest_bid, 10);
assert.equal(count('sync'), 0);

// 2) 순서대로 온 patch: 바뀐 부분만 새로 만들고 나머지는 그대로 재사용한다
const teamsBefore = conn.state.teams;
handlers.patch(bidPatch(6, 20));
assert.equal(conn.state.seq, 7);
assert.equal(conn.state.lot.highest_bid, 20);
assert.equal(conn.state.teams, teamsBefore);
assert.equal(conn.state.remaining_ms, 15000);

// 3) 이미 반영된(오래된) patch 는 무시한다
handlers.patch(bidPatch(5, 999));
assert.equal(conn.state.lot.highest_bid, 20);
assert.equal(count('sync'), 0);

// 4) 하나를 놓치면(8번이 안 옴) sync 를 한 번만 요청하고, 먼저 온 것은 보관했다가 이어 적용한다
handlers.patch(bidPatch(8, 40));                 // base 8 인데 나는 7
handlers.patch(bidPatch(9, 50));
assert.equal(count('sync'), 1);
assert.equal(conn.state.seq, 7);
take('sync').reply({ ok: true, snapshot: base(8, 30) });
await tick();
assert.equal(conn.state.seq, 10);
assert.equal(conn.state.lot.highest_bid, 50);

// 5) 재동기화 응답이 그사이 도착한 방송보다 오래된 경우: 가진 것을 유지한다
handlers.timer({ rev: 999, lot_seq: 1, remaining_ms: 1, paused: false });   // rev 불일치 → sync 요청
assert.equal(count('sync'), 1);
handlers.patch(bidPatch(10, 60));                // 응답보다 먼저 방송이 도착
take('sync').reply({ ok: true, snapshot: base(10, 50) });
await tick();
assert.equal(conn.state.seq, 11);
assert.equal(conn.state.lot.highest_bid, 60);

// 6) 전체 상태 방송(변경이 커서 patch 대신 온 경우)은 순번이 더 새로울 때만 받는다
handlers.state(base(9, 1));
assert.equal(conn.state.seq, 11);
handlers.state(base(12, 70));
assert.equal(conn.state.seq, 12);
assert.equal(conn.state.lot.highest_bid, 70);

// 7) 잘못된 patch 는 적용하지 않고 sync 를 요청한다
handlers.patch({ base: 12, seq: 13, remaining_ms: 1, p: ['l', { 0: ['=', 1] }] });   // 객체에 목록 patch
assert.equal(conn.state.seq, 12);
take('sync').reply({ ok: true, snapshot: base(13, 80) });
await tick();
assert.equal(conn.state.seq, 13);

// 8) 타이머는 rev 가 같으면 남은 시간만 고친다
handlers.timer({ rev: 13, lot_seq: 1, remaining_ms: 4321, paused: false });
assert.equal(conn.state.remaining_ms, 4321);
assert.equal(count('sync'), 0);

// 9) 재접속하면 순번이 다시 시작될 수 있다: 이전 연결의 큰 순번이 새 스냅샷을 막지 않는다
handlers.disconnect();
handlers.connect();
handlers.patch(bidPatch(2, 5));                  // 새 연결의 patch (이전 상태 13번에 이어 붙이면 안 된다)
assert.equal(conn.state.seq, 13);
take('join').reply({ ok: true, role: 'observer', team_id: null, nickname: 'n', snapshot: base(2, 0), chat: [] });
await tick();
assert.equal(conn.state.seq, 3);
assert.equal(conn.state.lot.highest_bid, 5);

assert.ok(seen.length >= 8);
console.log(JSON.stringify({ ok: true, states: seen.length }));
