// 서버가 알려주는 공통 정보 (영웅 목록, 제한값). 한 번만 불러와 모든 화면이 공유한다.
let promise = null;
let meta = null;
let heroMap = new Map();

export const ROLE_NAME = { tank: '탱커', damage: '딜러', support: '힐러' };
export const ROLES = ['tank', 'damage', 'support'];

// 선수 객체에서 포지션 목록을 꺼낸다 (탱커→딜러→힐러 순). 옛 데이터(role 문자열)도 읽는다.
export function rolesOf(p) {
  const raw = Array.isArray(p && p.roles) ? p.roles : (p && p.role ? [p.role] : []);
  return ROLES.filter((r) => raw.includes(r));
}
export const rolesText = (p, sep = '/') => rolesOf(p).map((r) => ROLE_NAME[r]).join(sep);

export function loadMeta() {
  promise ||= fetch('/api/server-info', { cache: 'no-store' })
    .then((r) => r.json())
    .then((j) => {
      if (j && j.ok) {
        meta = j;
        heroMap = new Map((j.heroes || []).map((hero) => [hero.id, hero]));
      }
      return meta;
    })
    .catch(() => null);
  return promise;
}

export const getMeta = () => meta;
export const heroName = (id) => heroMap.get(id)?.name ?? id;
export const heroList = () => (meta ? meta.heroes : []);
export const playerLimits = () => meta?.player_limits ?? { max_heroes: 5, max_images: 2, max_upload_bytes: 6 * 1024 * 1024 };
