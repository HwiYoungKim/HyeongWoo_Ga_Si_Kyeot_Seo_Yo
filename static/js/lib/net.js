// 링크에 쓸 서버 주소 판단 도우미.

// 이 PC 에서만 열리는 주소(127.0.0.1, localhost, ::1 등)인가? 이런 주소로 만든 링크는 다른 사람이 열 수 없다.
export function isLoopbackUrl(value) {
  try {
    const host = new URL(String(value).trim()).hostname.replace(/^\[|\]$/g, '');
    return host === 'localhost' || host.endsWith('.localhost') || host === '::1' || /^127\./.test(host) || host === '0.0.0.0';
  } catch {
    return false;
  }
}

// 링크 기본 주소: 외부 공개 주소가 있으면 그것을, 없으면 지금 접속한 주소를 쓴다.
export function defaultShareBase(info) {
  if (info && info.share_url) return info.share_url;
  if (info && info.lan_enabled && info.urls && info.urls.length) return info.urls[0].url;
  return location.origin;
}

export const LOOPBACK_WARNING =
  '이 주소는 이 PC 에서만 열립니다. 다른 사람이 접속하려면 외부 공개 주소(터널 주소)나 같은 와이파이 주소를 써야 합니다. ' +
  '서버를 `python app.py --tunnel` 로 실행하면 공개 주소가 자동으로 채워집니다.';
