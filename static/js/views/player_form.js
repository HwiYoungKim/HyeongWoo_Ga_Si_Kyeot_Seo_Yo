// 주최자 화면의 선수 한 명 입력 카드.
//  - 이름 / 배틀태그 / Elo / 소개
//  - 포지션: 탱커·딜러·힐러 아이콘 3개 중 하나만 켜진다
//  - 영웅: 최대 5개 (검색 + 역할별 목록)
//  - 이미지: 0~2장 (0장이면 "이미지 없음" 표시)
import { h, replace, toast } from '../lib/dom.js';
import { ROLE_NAME, ROLES, heroList, playerLimits, rolesOf } from '../lib/meta.js';
import { imageUrl, uploadImage } from '../lib/images.js';
import { roleIcon } from './profile.js';

export function createPlayerRow({ data = {}, conn, onChange, onRemove }) {
  const limits = playerLimits();
  const state = {
    uid: data.uid ?? '',
    roles: rolesOf(data),
    heroes: [...(data.heroes ?? [])],
    images: [...(data.images ?? [])],
  };

  const name = h('input', { type: 'text', maxlength: 20, placeholder: '이름(닉네임)', value: data.name ?? '', 'aria-label': '이름' });
  const battletag = h('input', { type: 'text', maxlength: 21, placeholder: '배틀태그 예) Player#1234', value: data.battletag ?? '', autocomplete: 'off', spellcheck: 'false', 'aria-label': '배틀태그' });
  const elo = h('input', { type: 'number', step: 1, min: 0, max: 9999, placeholder: 'Elo', value: data.elo ?? '', 'aria-label': 'Elo' });
  const intro = h('input', { type: 'text', maxlength: 128, placeholder: '소개(선택)', value: data.intro ?? '', 'aria-label': '소개' });

  const changed = () => onChange && onChange();

  // ------------------------------------------------------------ 포지션 (눌러서 켜고 끔 · 여러 개 선택 가능 · 최소 1개)
  const roleButtons = ROLES.map((role) => h('button', {
    type: 'button', class: `role-btn role-btn-${role}`, 'aria-pressed': 'false', title: `${ROLE_NAME[role]} (다시 누르면 해제)`,
    onClick: () => {
      state.roles = state.roles.includes(role) ? state.roles.filter((r) => r !== role) : ROLES.filter((r) => r === role || state.roles.includes(r));
      paintRole(); changed();
    },
  }, roleIcon(role, { lit: false }), h('span', { text: ROLE_NAME[role] })));
  const roleBox = h('div', { class: 'role-select', role: 'group', 'aria-label': '포지션' }, roleButtons);
  function paintRole() {
    ROLES.forEach((role, i) => {
      const on = state.roles.includes(role);
      roleButtons[i].classList.toggle('lit', on);
      roleButtons[i].setAttribute('aria-pressed', String(on));
      roleButtons[i].querySelector('img').classList.toggle('lit', on);
    });
    roleBox.classList.toggle('unset', state.roles.length === 0);
  }

  // ------------------------------------------------------------ 영웅 (최대 5개)
  const heroCount = h('strong', { class: 'hero-count' });
  const selectedBox = h('div', { class: 'hero-selected' });
  const search = h('input', { type: 'search', placeholder: '영웅 검색', 'aria-label': '영웅 검색', autocomplete: 'off' });
  const chipBox = h('div', { class: 'hero-grid' });
  const chips = new Map();   // heroId -> { el, name }
  function buildHeroChips() {
    replace(chipBox);
    chips.clear();
    const heroes = heroList();
    for (const role of ROLES) {
      const group = h('div', { class: 'hero-group' }, h('div', { class: 'hero-group-title' }, roleIcon(role, { small: true }), h('span', { text: ROLE_NAME[role] })));
      const list = h('div', { class: 'hero-chips' });
      for (const hero of heroes.filter((x) => x.role === role)) {
        const el = h('button', { type: 'button', class: 'hero-chip', text: hero.name, 'aria-pressed': 'false',
          onClick: () => toggleHero(hero.id) });
        chips.set(hero.id, { el, name: hero.name.toLowerCase(), role });
        list.appendChild(el);
      }
      group.appendChild(list);
      chipBox.appendChild(group);
    }
    paintHeroes();
  }
  function toggleHero(id) {
    const at = state.heroes.indexOf(id);
    if (at >= 0) state.heroes.splice(at, 1);
    else if (state.heroes.length >= limits.max_heroes) { toast(`영웅은 최대 ${limits.max_heroes}개까지 고를 수 있습니다.`, 'error'); return; }
    else state.heroes.push(id);
    paintHeroes();
    changed();
  }
  function paintHeroes() {
    const full = state.heroes.length >= limits.max_heroes;
    heroCount.textContent = `영웅 ${state.heroes.length}/${limits.max_heroes}`;
    for (const [id, c] of chips) {
      const on = state.heroes.includes(id);
      c.el.classList.toggle('on', on);
      c.el.setAttribute('aria-pressed', String(on));
      c.el.disabled = full && !on;
    }
    replace(selectedBox, state.heroes.length
      ? state.heroes.map((id) => {
          const c = chips.get(id);
          return h('button', { type: 'button', class: 'chip chip-hero chip-removable', title: '눌러서 제거', onClick: () => toggleHero(id),
            text: `${c ? c.el.textContent : id} ✕` });
        })
      : h('span', { class: 'sub', text: '선택한 영웅이 없습니다.' }));
  }
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    for (const c of chips.values()) c.el.hidden = q !== '' && !c.name.includes(q);
  });
  const heroPanel = h('details', { class: 'hero-panel' },
    h('summary', {}, heroCount, h('span', { class: 'sub', text: '  눌러서 영웅 고르기' })),
    search, chipBox);

  // ------------------------------------------------------------ 이미지 (0~2장)
  const imageBox = h('div', { class: 'image-slots' });
  const fileInput = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp,image/gif', class: 'file-input', 'aria-label': '이미지 추가' });
  const addBtn = h('button', { type: 'button', class: 'btn btn-sm', text: '이미지 추가', onClick: () => fileInput.click() });
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!file || state.images.length >= limits.max_images) return;
    addBtn.disabled = true;
    addBtn.textContent = '업로드 중…';
    const res = await uploadImage(conn, file, limits.max_upload_bytes);
    addBtn.textContent = '이미지 추가';
    if (!res.ok) { toast(res.message, 'error'); paintImages(); return; }
    state.images.push(res.id);
    paintImages();
    changed();
  });
  function paintImages() {
    const slots = state.images.map((id, i) => {
      const img = h('img', { class: 'thumb', alt: `이미지 ${i + 1}` });
      imageUrl(conn, id).then((url) => { img.src = url; }).catch(() => { img.alt = '불러오지 못함'; });
      return h('div', { class: 'thumb-box' }, img,
        h('button', { type: 'button', class: 'btn btn-sm thumb-del', text: '제거', onClick: () => {
          state.images.splice(state.images.indexOf(id), 1);
          paintImages();
          changed();
        } }));
    });
    replace(imageBox,
      slots,
      state.images.length === 0 ? h('div', { class: 'no-image no-image-sm', text: '이미지 없음' }) : null,
      state.images.length < limits.max_images ? addBtn : null,
      h('span', { class: 'sub', text: `이미지 ${state.images.length}/${limits.max_images}` }),
      fileInput);
    addBtn.disabled = false;
  }

  // ------------------------------------------------------------ 조립
  const removeBtn = h('button', { type: 'button', class: 'btn btn-sm', text: '삭제', onClick: () => onRemove && onRemove() });
  const el = h('div', { class: 'player-card' },
    h('div', { class: 'pc-top' }, name, battletag, elo, removeBtn),
    h('div', { class: 'pc-mid' }, roleBox, intro),
    selectedBox, heroPanel, imageBox);

  buildHeroChips();
  paintRole();
  paintImages();

  return {
    el,
    nameInput: name,
    get uid() { return state.uid; },
    hasName: () => name.value.trim() !== '',
    hasRole: () => state.roles.length > 0,
    refreshHeroes: buildHeroChips,       // 영웅 목록을 나중에 불러온 경우 다시 그린다
    getValue: () => ({
      uid: state.uid,
      name: name.value,
      battletag: battletag.value,
      roles: [...state.roles],
      heroes: [...state.heroes],
      images: [...state.images],
      elo: elo.value === '' ? null : Number(elo.value),
      intro: intro.value,
    }),
  };
}
