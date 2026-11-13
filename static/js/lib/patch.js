// 서버가 보낸 변경분(패치)을 현재 상태에 적용한다. 서버 쪽 규칙은 server/statediff.py 와 같다.
//
//   ["=", 값]                통째로 교체
//   ["o", {키: 패치}, [키]]   객체: 바뀐 키만. 세 번째 원소는 지워진 키 목록 (없을 수 있음)
//   ["l", {"번호": 패치}]     길이가 같은 목록: 바뀐 칸만
//   ["s", k, [값...]]         목록: 앞에서 k개를 버리고 뒤에 값들을 붙인다
//
// 원본은 바꾸지 않고 새 값을 돌려준다. 형식이 맞지 않으면 예외를 던진다 (호출한 쪽이 전체 재동기화한다).

const BLOCKED_KEYS = new Set(['__proto__', 'constructor', 'prototype']);

export function applyPatch(base, patch) {
  if (!Array.isArray(patch) || patch.length < 2) throw new Error('bad patch');
  const kind = patch[0];
  if (kind === '=') return patch[1];
  if (kind === 'o') {
    if (base === null || typeof base !== 'object' || Array.isArray(base)) throw new Error('object patch on non-object');
    const out = { ...base };
    for (const key of Object.keys(patch[1])) {
      if (BLOCKED_KEYS.has(key)) throw new Error('bad key');
      out[key] = applyPatch(Object.hasOwn(base, key) ? base[key] : null, patch[1][key]);
    }
    for (const key of patch[2] || []) delete out[key];
    return out;
  }
  if (kind === 'l') {
    if (!Array.isArray(base)) throw new Error('list patch on non-list');
    const out = base.slice();
    for (const key of Object.keys(patch[1])) {
      const i = Number(key);
      if (!Number.isInteger(i) || i < 0 || i >= out.length) throw new Error('index out of range');
      out[i] = applyPatch(out[i], patch[1][key]);
    }
    return out;
  }
  if (kind === 's') {
    const k = patch[1];
    if (!Array.isArray(base) || !Number.isInteger(k) || k < 0 || k > base.length || !Array.isArray(patch[2])) {
      throw new Error('bad splice');
    }
    return base.slice(k).concat(patch[2]);
  }
  throw new Error('unknown patch kind');
}
