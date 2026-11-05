// 홈: 방 만들기. 만들어진 접속 링크는 이 화면에서 "한 번만" 보여준다.
import { h, replace, toast, copyText, $ } from './lib/dom.js';

const form = $('#create-form');
const resultBox = $('#result');
const keyInput = $('#admin-key');
const baseInput = $('#base-url');
const baseList = $('#base-urls');
const secureNote = $('#secure-note');

// 서버 콘솔이 열어 준 주소(#admin=키)로 들어오면 키를 채우고 주소창에서 즉시 지운다.
const hashParams = new URLSearchParams(location.hash.slice(1));
if (hashParams.get('admin')) {
  keyInput.value = hashParams.get('admin');
  history.replaceState(null, '', location.pathname);
}

baseInput.value = location.origin;
fetch('/api/server-info').then((r) => r.json()).then((info) => {
  if (!info.ok) return;
  for (const u of info.urls) baseList.appendChild(h('option', { value: u.url, label: u.label }));
  if (info.lan_enabled && info.urls.length) baseInput.value = info.urls[0].url;
  if (!info.secure && info.lan_enabled) {
    secureNote.hidden = false;
  }
}).catch(() => {});

let created = null;

function origin() {
  try {
    const u = new URL(baseInput.value.trim());
    if (u.protocol === 'http:' || u.protocol === 'https:') return u.origin;
  } catch { /* 아래 기본값 */ }
  return location.origin;
}

function linkFor(token) {
  return `${origin()}/r/${created.room_id}#t=${token}`;
}

function rows() {
  const t = created.tokens;
  return [
    { label: '주최자 (비공개 · 본인만)', link: linkFor(t.host), host: true },
    { label: '관전자 공용', link: linkFor(t.observer) },
    ...t.leaders.map((tok, i) => ({ label: `팀 ${i + 1} 팀장 전용`, link: linkFor(tok) })),
  ];
}

function renderResult() {
  if (!created) return;
  const list = rows();
  replace(resultBox,
    h('h2', { text: '방이 만들어졌습니다' }),
    h('p', { class: 'warn-box', text: '아래 링크는 이 화면에서만 볼 수 있습니다. 링크를 가진 사람은 누구나 해당 역할로 접속할 수 있으니 필요한 사람에게만 따로 전달하세요. (팀장 링크는 팀장마다 다릅니다.)' }),
    h('div', { class: 'link-list' }, list.map((r) => h('div', { class: 'link-row' },
      h('strong', { text: r.label }),
      h('input', { type: 'text', readOnly: true, value: r.link, onFocus: (e) => e.target.select() }),
      h('button', { type: 'button', class: 'btn btn-sm', text: '복사',
        onClick: async () => toast((await copyText(r.link)) ? '복사했습니다.' : '복사하지 못했습니다. 직접 선택해 복사해 주세요.', 'ok') }),
      r.host ? h('a', { class: 'btn btn-primary btn-sm', href: r.link, target: '_blank', rel: 'noopener noreferrer', text: '주최자 화면 열기' }) : null,
    ))),
    h('div', { class: 'row' },
      h('button', { type: 'button', class: 'btn', text: '모든 링크 파일로 저장', onClick: download }),
      h('span', { class: 'sub', text: '저장한 파일에는 접속 정보가 들어 있으니 보관에 주의하세요.' })),
  );
  resultBox.hidden = false;
}

function download() {
  const text = rows().map((r) => `${r.label}\n${r.link}\n`).join('\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const a = h('a', { href: url, download: `auction-${created.room_id}-links.txt` });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

baseInput.addEventListener('input', renderResult);

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = {
    admin_key: keyInput.value,
    title: $('#title').value,
    num_teams: Number($('#num-teams').value),
    members_per_team: Number($('#members').value),
  };
  const button = form.querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const resp = await fetch('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      toast(data.message || '방을 만들지 못했습니다.', 'error');
      return;
    }
    created = data;
    renderResult();
    resultBox.scrollIntoView({ behavior: 'smooth' });
  } catch {
    toast('서버에 연결하지 못했습니다.', 'error');
  } finally {
    button.disabled = false;
  }
});
