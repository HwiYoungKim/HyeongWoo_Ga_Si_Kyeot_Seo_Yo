// 관전자 화면: 읽기 전용 + 채팅 닉네임 설정.
import { h, replace } from '../lib/dom.js';

const KEY = 'auction.nickname';

export function savedNickname() {
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
}

export function mountObserver(root, ctx) {
  const { conn } = ctx;
  const input = h('input', { type: 'text', maxlength: 16, placeholder: '채팅 닉네임', value: conn.nickname === '관전자' ? savedNickname() : conn.nickname });
  const form = h('form', { class: 'row', onSubmit: async (ev) => {
    ev.preventDefault();
    const nick = input.value.trim().slice(0, 16);
    try { sessionStorage.setItem(KEY, nick); } catch { /* 저장 불가 환경은 무시 */ }
    await conn.rejoin(nick);
  } }, input, h('button', { type: 'submit', class: 'btn', text: '닉네임 변경' }));
  replace(root,
    h('h2', { text: '관전 중' }),
    h('p', { class: 'sub', text: '관전자는 경매 현황을 보고 채팅만 할 수 있습니다.' }),
    form,
  );
  return { update() {} };
}
