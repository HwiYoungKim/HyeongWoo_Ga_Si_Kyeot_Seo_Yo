// 선수 이미지 가져오기/올리기.
// 이미지는 접속 토큰이 필요한 API 로만 받을 수 있어서 <img src> 로 직접 부를 수 없다.
// fetch 로 (Authorization 헤더와 함께) 받아 blob URL 로 만든 뒤 화면에 넣는다.
// 토큰은 URL 이 아니라 헤더로만 보낸다.
//
// 데이터 절약: 화면에는 서버가 줄여 준 축소본('preview')을 쓰고, 원본('full')은 눌러서 크게 볼 때만 받는다.
const RETRY_DELAYS_MS = [400, 1200, 3000];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// 일시적인 실패(요청이 몰려 429, 서버 오류, 네트워크 끊김)는 잠깐 기다렸다가 다시 받는다.
// 권한이 없거나(403) 없는 이미지(404)는 다시 해도 같으므로 바로 끝낸다.
async function fetchWithRetry(url, options) {
  for (let attempt = 0; ; attempt++) {
    let res = null;
    try { res = await fetch(url, options); } catch { /* 네트워크 오류: 아래에서 재시도 */ }
    const temporary = res === null || res.status === 429 || res.status >= 500;
    if (!temporary || attempt >= RETRY_DELAYS_MS.length) {
      if (res === null) throw new Error('network');
      return res;
    }
    await sleep(RETRY_DELAYS_MS[attempt] + Math.random() * 300);
  }
}

const cache = new Map();   // "방/이미지id/종류" -> Promise<blobURL>
const ready = new Map();   // "방/이미지id/종류" -> blobURL (이미 받은 것)

export function imageUrl(conn, id, variant = 'preview') {
  const key = `${conn.roomId}/${id}/${variant}`;
  if (!cache.has(key)) {
    const query = variant === 'preview' ? '?v=preview' : '';
    const p = fetchWithRetry(`/api/rooms/${encodeURIComponent(conn.roomId)}/images/${encodeURIComponent(id)}${query}`, {
      headers: { Authorization: `Bearer ${conn.token}` },
      cache: 'no-store',
    })
      .then((r) => {
        if (!r.ok) throw new Error('image');
        // 원본이 이미 작아서 축소본이 원본과 같으면, 크게 볼 때 다시 받지 않도록 같은 것으로 기억해 둔다.
        if (variant === 'preview' && r.headers.get('X-Image-Original') === '1') {
          cache.set(`${conn.roomId}/${id}/full`, p);
        }
        return r.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        ready.set(key, url);
        return url;
      });
    p.catch(() => {                          // 실패는 캐시하지 않는다 (다시 시도 가능)
      for (const [k, v] of cache) if (v === p) cache.delete(k);
    });
    cache.set(key, p);
  }
  return cache.get(key);
}

// <img> 에 이미지를 넣는다. 이미 받은 이미지는 즉시 넣어서, 화면을 다시 그릴 때 깜빡이지 않게 한다.
export function setImage(img, conn, id, variant = 'preview') {
  const url = ready.get(`${conn.roomId}/${id}/${variant}`);
  if (url) { img.src = url; return; }
  imageUrl(conn, id, variant).then((u) => { img.src = u; }).catch(() => { img.hidden = true; });
}

export async function uploadImage(conn, file, maxBytes) {
  if (file.size > maxBytes) {
    return { ok: false, message: `이미지는 ${Math.floor(maxBytes / 1024 / 1024)}MB 이하여야 합니다.` };
  }
  try {
    const r = await fetch(`/api/rooms/${encodeURIComponent(conn.roomId)}/images`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${conn.token}`, 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || !data.ok) return { ok: false, message: data.message || '업로드하지 못했습니다.' };
    return { ok: true, id: data.id };
  } catch {
    return { ok: false, message: '서버와 연결할 수 없습니다.' };
  }
}
