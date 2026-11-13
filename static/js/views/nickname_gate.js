// 관전자 입장 창: 닉네임을 정해야 관전 화면으로 넘어간다.
// 서버도 닉네임 없는 관전자에게는 방의 상태를 내려주지 않으므로, 이 창을 지워도 화면에 보이는 것은 없다.
import { h } from '../lib/dom.js';
import { saveNickname, NICK_MAX } from './observer.js';

export function createNicknameGate(conn, initial = '') {
  let el = null;
  let busy = false;
  const input = h('input', { type: 'text', maxlength: NICK_MAX, placeholder: '닉네임 (최대 16자)', value: initial,
    autocomplete: 'nickname', 'aria-label': '닉네임' });
  const error = h('p', { class: 'gate-error', role: 'alert' });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary', text: '입장하기' });
  const form = h('form', { class: 'gate-box', novalidate: '', onSubmit: async (ev) => {
    ev.preventDefault();
    if (busy) return;
    const nick = input.value.trim().slice(0, NICK_MAX);
    if (!nick) { error.textContent = '닉네임을 입력해 주세요.'; input.focus(); return; }
    busy = true;
    submit.disabled = true;
    submit.textContent = '입장하는 중…';
    error.textContent = '';
    saveNickname(nick);
    await conn.rejoin(nick);          // 성공하면 'joined', 실패하면 'need_nickname' 이 다시 온다
    busy = false;
    submit.disabled = false;
    submit.textContent = '입장하기';
  } },
    h('h2', { text: '관전자 입장' }),
    h('p', { class: 'sub', text: '채팅에 표시될 닉네임을 정해 주세요. 닉네임을 정해야 관전 화면으로 넘어갑니다.' }),
    input, error, submit,
    h('p', { class: 'sub', text: '닉네임은 들어간 뒤에도 바꿀 수 있습니다.' }));

  return {
    show(message) {
      if (!el) {
        el = h('div', { class: 'gate', role: 'dialog', 'aria-modal': 'true', 'aria-label': '관전자 닉네임 설정' }, form);
        document.body.appendChild(el);
        document.body.classList.add('gated');
        input.focus();
      }
      if (message) error.textContent = message;
    },
    hide() {
      if (!el) return;
      el.remove();
      el = null;
      document.body.classList.remove('gated');
    },
    get open() { return el !== null; },
  };
}
