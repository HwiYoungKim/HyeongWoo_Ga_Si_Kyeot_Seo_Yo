// 선수 이미지 가져오기/올리기.
// 이미지는 접속 토큰이 필요한 API 로만 받을 수 있어서 <img src> 로 직접 부를 수 없다.
// fetch 로 (Authorization 헤더와 함께) 받아 blob URL 로 만든 뒤 화면에 넣는다.
// 토큰은 URL 이 아니라 헤더로만 보낸다.
const cache = new Map();   // "방/이미지id" -> Promise<blobURL>

export function imageUrl(conn, id) {
  const key = `${conn.roomId}/${id}`;
  if (!cache.has(key)) {
    const p = fetch(`/api/rooms/${encodeURIComponent(conn.roomId)}/images/${encodeURIComponent(id)}`, {
      headers: { Authorization: `Bearer ${conn.token}` },
      cache: 'no-store',
    })
      .then((r) => {
        if (!r.ok) throw new Error('image');
        return r.blob();
      })
      .then((blob) => URL.createObjectURL(blob));
    p.catch(() => cache.delete(key));       // 실패는 캐시하지 않는다 (다시 시도 가능)
    cache.set(key, p);
  }
  return cache.get(key);
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
