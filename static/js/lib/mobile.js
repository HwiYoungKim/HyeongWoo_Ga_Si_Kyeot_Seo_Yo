// 모바일 화면 보조: 화면 아래에 고정된 입찰 바의 높이를 CSS 변수(--bar-h)로 알려 준다.
// 본문이 바에 가려지지 않게 여백을 주고, 토스트가 바 위에 뜨게 하는 데 쓴다.
// (인라인 style 속성은 CSP 가 막지만 CSSOM 의 setProperty 는 허용된다.)

export function trackFixedBar(el) {
  const root = document.documentElement;
  const apply = () => {
    const fixed = getComputedStyle(el).position === 'fixed';
    root.style.setProperty('--bar-h', fixed ? `${Math.ceil(el.getBoundingClientRect().height)}px` : '0px');
  };
  if (typeof ResizeObserver === 'function') new ResizeObserver(apply).observe(el);
  window.addEventListener('resize', apply);
  window.addEventListener('orientationchange', apply);
  apply();
}

// 상단 바의 작은 타이머: 아래로 스크롤해도 남은 시간이 보이게 무대 타이머의 글자를 복사한다.
export function syncMiniTimer(mini, timerEl) {
  if (!mini) return;
  const hidden = timerEl.classList.contains('timer-hidden');
  mini.hidden = hidden;
  if (hidden) return;
  mini.textContent = timerEl.textContent;
  mini.className = 'mini-timer'
    + (timerEl.classList.contains('timer-danger') ? ' timer-danger' : '')
    + (timerEl.classList.contains('timer-paused') ? ' timer-paused' : '');
}
