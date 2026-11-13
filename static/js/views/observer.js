// 관전자 화면: 읽기 전용 + 채팅 닉네임 설정.
import { h, replace, toast } from '../lib/dom.js';

const KEY = 'auction.nickname';
export const NICK_MAX = 16;

export function savedNickname() {
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
}

export function saveNickname(nick) {
  try { sessionStorage.setItem(KEY, nick); } catch { /* 저장 불가 환경은 무시 */ }
}

export function mountObserver(root, ctx) {
  const { conn } = ctx;
  const input = h('input', { type: 'text', maxlength: NICK_MAX, placeholder: '채팅 닉네임', value: conn.nickname });
  const form = h('form', { class: 'row', onSubmit: async (ev) => {
    ev.preventDefault();
    const nick = input.value.trim().slice(0, NICK_MAX);
    if (!nick) { toast('닉네임을 입력해 주세요.', 'error'); input.value = conn.nickname; return; }
    const before = conn.nickname;
    saveNickname(nick);
    await conn.rejoin(nick);
    if (conn.nickname !== before) toast(`닉네임을 '${conn.nickname}'(으)로 바꿨습니다.`, 'ok');
    input.value = conn.nickname;
  } }, input, h('button', { type: 'submit', class: 'btn', text: '닉네임 변경' }));
  replace(root,
    h('h2', { text: '관전 중' }),
    h('p', { class: 'sub', text: '관전자는 경매 현황을 보고 채팅만 할 수 있습니다.' }),
    form,
  );
  return { update() {} };
}
