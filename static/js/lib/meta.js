// 서버가 알려주는 공통 정보 (영웅 목록, 제한값). 한 번만 불러와 모든 화면이 공유한다.
let promise = null;
let meta = null;
let heroMap = new Map();

export const ROLE_NAME = { tank: '탱커', damage: '딜러', support: '힐러' };
export const ROLES = ['tank', 'damage', 'support'];

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
