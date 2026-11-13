// Socket.IO 연결 관리: 자동 재접속 + 재접속 시 자동 재인증 + 변경분(패치) 적용 + 타이머 보정.
//
// 끊김 없는 동기화의 핵심
//  1) 연결이 끊기면 socket.io 가 자동으로 재연결하고, 'connect' 마다 join 을 다시 보내
//     서버가 현재 전체 상태(스냅샷)를 돌려준다 → 끊겨 있던 동안의 변화를 즉시 따라잡는다.
//  2) 그 뒤로 서버는 바뀐 부분만 담은 patch 를 보낸다 (데이터 절약). 상태에는 순번(seq)이 있고
//     patch 는 "base 번을 seq 번으로 바꾸는 변경분"이다. 내 순번이 base 와 다르면 하나를 놓친 것이므로
//     sync 로 전체 상태를 다시 받는다. 먼저 도착한 미래의 patch 는 잠시 보관했다가 이어서 적용한다.
//  3) 서버가 몇 초마다 보내는 timer 메시지의 rev 가 내 상태의 rev 와 다를 때도 sync 를 요청한다.
//  4) 남은 시간은 "수신 시각 + 남은 ms" 로 로컬에서 계산하고, 서버 timer/patch 로 계속 보정한다.
import { applyPatch } from './patch.js';

const MAX_PENDING_PATCHES = 64;

export class Connection {
  constructor(roomId, token) {
    this.roomId = roomId;
    this.token = token;
    this.nickname = '';
    this.socket = null;
    this.state = null;
    this.stateAt = 0;
    this.role = null;
    this.teamId = null;
    this.handlers = {};
    this.joined = false;
    this._syncing = false;
    this._pending = [];     // 아직 적용하지 못한(순번이 앞선) patch
    this._seqLive = false;  // 지금 가진 상태의 순번이 현재 연결에서 받은 것인가 (재접속하면 순번이 다시 시작될 수 있다)
  }

  on(event, fn) {
    (this.handlers[event] ||= []).push(fn);
    return this;
  }

  _emit(event, ...args) {
    for (const fn of this.handlers[event] || []) fn(...args);
  }

  connect() {
    const io = window.io;
    if (!io) {
      this._emit('fatal', '실시간 통신 모듈을 불러오지 못했습니다.');
      return;
    }
    // 웹소켓으로 바로 접속한다 (폴링으로 시작해 업그레이드하는 기본 방식보다 왕복 3~4번이 적다).
    // 웹소켓이 막힌 네트워크에서는 tryAllTransports 로 폴링으로 자동 전환한다.
    this.socket = io({
      transports: ['websocket', 'polling'],
      tryAllTransports: true,
      reconnectionDelay: 300,
      reconnectionDelayMax: 3000,
    });
    const s = this.socket;

    s.on('connect', () => { this._emit('status', 'joining'); this._join(); });
    s.on('disconnect', () => {
      this.joined = false;
      this._pending = [];
      this._seqLive = false;
      this._emit('status', 'offline');
    });
    s.on('connect_error', () => this._emit('status', 'offline'));

    s.on('state', (state) => this._applyState(state, false));
    s.on('patch', (m) => this._onPatch(m));
    s.on('timer', (t) => this._onTimer(t));
    s.on('chat', (m) => this._emit('chat', m));
    s.on('room_closed', () => {
      this.closed = true;
      s.disconnect();
      this._emit('fatal', '주최자가 방을 삭제했습니다.');
    });
  }

  async _join() {
    const res = await this.request('join', {
      room_id: this.roomId, token: this.token, nickname: this.nickname,
    });
    if (!res.ok) {
      if (res.code === 'auth') {
        this.socket.disconnect();                // 잘못된 링크는 재시도하지 않는다
        this._emit('fatal', res.message);
      } else if (res.code === 'nickname_required') {
        // 관전자는 닉네임을 정해야 들어갈 수 있다. 화면이 입력창을 띄우고 rejoin(닉네임) 을 부른다.
        this._emit('need_nickname', { message: res.message, entered: this.joined });
      } else {
        this._emit('status', 'offline');
        this._emit('toast', res.message || '접속에 실패했습니다.');
      }
      return;
    }
    const first = !this.joined && this.role === null;
    this.joined = true;
    this.role = res.role;
    this.teamId = res.team_id;
    this.nickname = res.nickname;
    this._applyState(res.snapshot, true);
    this._emit('status', 'online');
    this._emit('joined', { role: res.role, teamId: res.team_id, nickname: res.nickname, chat: res.chat, first });
  }

  // 관전자 닉네임 변경 등으로 같은 연결에서 다시 join
  rejoin(nickname) {
    this.nickname = nickname;
    return this._join();
  }

  request(event, payload = {}, timeoutMs = 8000) {
    return new Promise((resolve) => {
      if (!this.socket || !this.socket.connected) {
        resolve({ ok: false, code: 'offline', message: '서버와 연결이 끊겨 있습니다.' });
        return;
      }
      this.socket.timeout(timeoutMs).emit(event, payload, (err, ack) => {
        if (err || !ack) resolve({ ok: false, code: 'timeout', message: '서버 응답이 없습니다.' });
        else resolve(ack);
      });
    });
  }

  // 전체 상태 수신 (접속/재동기화 응답은 force=true)
  _applyState(state, force) {
    const newer = this._seqLive && this.state && this.state.seq >= state.seq;   // 이미 더 새로운 것을 갖고 있다
    if (newer && !force) return;                                              // 오래된 메시지 무시
    // 접속/재동기화 응답이 그사이 도착한 방송보다 늦게 온 경우에는 가진 것을 그대로 둔다.
    if (!(newer && this.state.seq > state.seq)) this.state = state;
    this._seqLive = true;
    this._drainPending();     // 보관해 둔 patch 중 이어지는 것을 적용한다
    this.stateAt = performance.now();
    this._emit('state', this.state);
  }

  // 변경분 수신
  _onPatch(m) {
    if (!m || typeof m.seq !== 'number' || typeof m.base !== 'number') return;
    const st = this._seqLive ? this.state : null;      // 이전 연결에서 받은 상태에는 이어 붙일 수 없다
    if (st && m.seq <= st.seq) return;                 // 이미 반영된 변경
    if (!st || m.base !== st.seq) {
      // 접속 응답보다 먼저 왔거나 하나를 놓쳤다 → 보관하고 전체 상태를 다시 받는다
      if (this._pending.length < MAX_PENDING_PATCHES) this._pending.push(m);
      if (st) this._resync();
      return;
    }
    if (!this._applyPatchMessage(m)) { this._resync(); return; }
    this.stateAt = performance.now();
    this._emit('state', this.state);
  }

  _applyPatchMessage(m) {
    try {
      const next = applyPatch(this.state, m.p);
      next.seq = m.seq;
      next.remaining_ms = m.remaining_ms;
      this.state = next;
      return true;
    } catch {
      return false;
    }
  }

  _drainPending() {
    const pending = this._pending.sort((a, b) => a.seq - b.seq);
    this._pending = [];
    for (const m of pending) {
      if (m.seq <= this.state.seq) continue;
      if (m.base !== this.state.seq || !this._applyPatchMessage(m)) break;   // 이어지지 않으면 다음 patch 에서 다시 맞춘다
    }
  }

  async _resync() {
    if (this._syncing) return;
    this._syncing = true;
    const res = await this.request('sync');
    this._syncing = false;
    if (res.ok) this._applyState(res.snapshot, true);
  }

  _onTimer(t) {
    if (!this.state) return;
    if (t.rev !== this.state.rev) {
      this._resync();        // 상태 변경을 놓쳤다 → 전체 재동기화
      return;
    }
    this.state.remaining_ms = t.remaining_ms;
    this.state.paused = t.paused;
    this.stateAt = performance.now();
  }

  // 지금 시점의 남은 시간(ms)
  remainingMs() {
    const st = this.state;
    if (!st || !st.lot) return 0;
    if (st.paused) return st.remaining_ms;
    return Math.max(0, st.remaining_ms - (performance.now() - this.stateAt));
  }
}
