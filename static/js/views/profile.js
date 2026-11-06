// 선수 프로필 조각: 포지션 아이콘, 배틀태그, 영웅 칩, 이미지 갤러리.
// 경매 화면(모든 역할)과 주최자 등록 화면이 같이 쓴다. 모든 문자열은 textContent 로 들어간다.
import { h } from '../lib/dom.js';
import { ROLE_NAME, heroName, rolesOf, rolesText } from '../lib/meta.js';
import { imageUrl } from '../lib/images.js';

export function roleIcon(role, { lit = true, small = false } = {}) {
  if (!ROLE_NAME[role]) return null;
  return h('img', {
    class: `role-icon role-${role}${lit ? ' lit' : ''}${small ? ' role-icon-sm' : ''}`,
    src: new URL(`../../img/role-${role}.svg`, import.meta.url).href, alt: ROLE_NAME[role], title: ROLE_NAME[role],
  });
}

// 포지션 아이콘들 (여러 개를 고른 선수는 아이콘이 모두 켜진다)
export function roleIcons(p, opts = { small: true }) {
  return rolesOf(p).map((r) => roleIcon(r, opts));
}

export function heroChips(ids) {
  return (ids || []).map((id) => h('span', { class: 'chip chip-hero', text: heroName(id) }));
}

// 경매 중인 매물 카드에 들어가는 프로필 줄
export function profileLine(p) {
  return h('div', { class: 'profile-line' },
    rolesOf(p).length ? h('span', { class: 'role-tag' }, roleIcons(p, {}), h('span', { text: rolesText(p) })) : null,
    p.battletag ? h('span', { class: 'chip chip-tag', text: p.battletag }) : null,
    p.elo !== null && p.elo !== undefined ? h('span', { class: 'chip', text: `Elo ${p.elo}` }) : null,
  );
}

// 이미지 0~2장. 0장이면 "이미지 없음"을 보여준다. 눌러서 크게 볼 수 있다.
export function gallery(conn, ids) {
  if (!ids || ids.length === 0) {
    return h('div', { class: 'gallery gallery-empty' }, h('div', { class: 'no-image', text: '이미지 없음' }));
  }
  const figs = ids.map((id) => {
    const img = h('img', { class: 'player-img', alt: '선수 이미지' });
    const note = h('span', { class: 'sub img-note' });
    const fig = h('figure', { class: 'player-fig loading', tabindex: 0, role: 'button', 'aria-label': '이미지 크게 보기' }, img, note);
    const load = () => {
      fig.classList.add('loading');
      fig.classList.remove('failed');
      note.textContent = '';
      imageUrl(conn, id)
        .then((url) => { img.src = url; fig.classList.remove('loading'); })
        .catch(() => { fig.classList.remove('loading'); fig.classList.add('failed'); note.textContent = '이미지를 불러오지 못했습니다. 눌러서 다시 시도'; });
    };
    const activate = () => {
      if (fig.classList.contains('failed')) load();
      else if (!fig.classList.contains('loading')) fig.classList.toggle('zoomed');
    };
    fig.addEventListener('click', activate);
    fig.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
      if (e.key === 'Escape') fig.classList.remove('zoomed');
    });
    load();
    return fig;
  });
  return h('div', { class: `gallery gallery-${ids.length}` }, figs);
}
