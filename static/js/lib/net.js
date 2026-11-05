// Socket.IO 연결 관리: 자동 재접속 + 재접속 시 자동 재인증 + 상태 버전(rev) 검사 + 타이머 보정.
//
// 끊김 없는 동기화의 핵심
//  1) 연결이 끊기면 socket.io 가 자동으로 재연결하고, 'connect' 마다 join 을 다시 보내
//     서버가 현재 전체 상태(스냅샷)를 돌려준다 → 끊겨 있던 동안의 변화를 즉시 따라잡는다.
//  2) 서버가 1초마다 보내는 timer 메시지의 rev 가 내가 가진 상태의 rev 와 다르면
//     (= 상태 이벤트를 놓쳤다는 뜻) 즉시 sync 를 요청한다.
//  3) 남은 시간은 "수신 시각 + 남은 ms" 로 로컬에서 계산하고, 서버 timer 로 계속 보정한다.

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
    s.on('disconnect', () => { this.joined = false; this._emit('status', 'offline'); });
    s.on('connect_error', () => this._emit('status', 'offline'));

    s.on('state', (state) => this._applyState(state, false));
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

  _applyState(state, force) {
    if (!force && this.state && state.rev < this.state.rev) return;   // 오래된 메시지 무시
    this.state = state;
    this.stateAt = performance.now();
    this._emit('state', state);
  }

  async _onTimer(t) {
    if (!this.state) return;
    if (t.rev !== this.state.rev) {
      // 상태 이벤트를 놓쳤다 → 전체 재동기화
      if (this._syncing) return;
      this._syncing = true;
      const res = await this.request('sync');
      this._syncing = false;
      if (res.ok) this._applyState(res.snapshot, true);
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
