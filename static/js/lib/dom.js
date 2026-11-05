// DOM 헬퍼 — innerHTML 을 쓰지 않고 항상 textContent / createTextNode 로 값을 넣는다.
// 선수 이름·소개·채팅 같은 사용자 입력이 HTML 로 해석될 길이 구조적으로 없다 (XSS 차단).

const SAFE_URL = /^(https?:\/\/|\/|#)/i;
const BOOL_PROPS = new Set(['disabled', 'checked', 'hidden', 'selected', 'required', 'readOnly', 'multiple']);

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') {
      el.className = value;
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (key === 'dataset') {
      Object.assign(el.dataset, value);
    } else if (key.startsWith('on')) {
      // 문자열 핸들러는 허용하지 않는다 (함수만)
      if (typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'href' || key === 'src') {
      if (SAFE_URL.test(String(value))) el.setAttribute(key, String(value));
    } else if (key === 'style') {
      continue; // CSP 가 인라인 스타일을 막는다. 클래스를 사용할 것.
    } else if (BOOL_PROPS.has(key)) {
      el[key] = Boolean(value);
    } else if (key === 'value') {
      el.value = String(value);
    } else {
      el.setAttribute(key, String(value));
    }
  }
  append(el, children);
  return el;
}

function append(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(parent, child);
    else if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

export const $ = (selector, root = document) => root.querySelector(selector);

let toastBox = null;
export function toast(message, kind = 'info') {
  toastBox = toastBox || document.getElementById('toasts');
  if (!toastBox) return;
  const el = h('div', { class: `toast toast-${kind}`, text: message });
  toastBox.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 5000 : 3000);
  while (toastBox.children.length > 5) toastBox.firstChild.remove();
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // clipboard API 가 막힌 환경(HTTP 등)용 대체 수단
    const ta = h('textarea', { class: 'offscreen', readOnly: true });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}
