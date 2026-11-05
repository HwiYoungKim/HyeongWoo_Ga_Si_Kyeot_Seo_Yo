// 실시간 채팅 패널. 메시지는 textContent 로만 삽입된다.
import { h, replace, toast } from '../lib/dom.js';
import { clockTime, ROLE_LABEL } from '../lib/format.js';

const MAX_ROWS = 200;

export function mountChat(box, conn, history) {
  const log = h('ul', { class: 'chat-log', 'aria-live': 'polite' });
  const input = h('input', { type: 'text', maxlength: 200, placeholder: '메시지 입력', autocomplete: 'off' });
  const form = h('form', { class: 'chat-form', onSubmit: async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    const res = await conn.request('chat', { text });
    if (!res.ok) toast(res.message || '전송하지 못했습니다.', 'error');
  } }, input, h('button', { type: 'submit', class: 'btn', text: '전송' }));

  replace(box, h('h2', { text: '채팅' }), log, form);

  function add(entry) {
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
    log.appendChild(h('li', { class: `chat-${entry.role}` },
      h('span', { class: 'chat-time', text: clockTime(entry.ts) }),
      h('span', { class: 'chat-name', text: entry.name }),
      // 역할 표시는 서버가 정한 값(entry.role)이라 닉네임으로 사칭할 수 없다.
      h('span', { class: 'chip chip-sm', text: ROLE_LABEL[entry.role] || '' }),
      h('span', { class: 'chat-text', text: entry.text }),
    ));
    while (log.children.length > MAX_ROWS) log.firstChild.remove();
    if (stick) log.scrollTop = log.scrollHeight;
  }

  for (const entry of history || []) add(entry);
  log.scrollTop = log.scrollHeight;
  conn.on('chat', add);

  return {
    reset(entries) {
      replace(log);
      for (const e of entries || []) add(e);
    },
  };
}
