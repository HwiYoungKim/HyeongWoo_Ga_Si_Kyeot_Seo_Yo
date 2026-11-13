// 기기 감지: <html data-device="mobile|desktop" data-orient="portrait|landscape"> 를 설정한다.
// 화면이 그려지기 전에 실행되어야 해서 <head> 에서 읽는 일반 스크립트(모듈 아님)로 둔다.
// 모양은 모두 css/mobile.css 가 이 속성을 보고 정한다. (CSP 때문에 인라인 스크립트는 쓰지 않는다.)
//
// 강제 전환: 주소 뒤에 ?ui=mobile / ?ui=desktop / ?ui=auto (탭을 닫기 전까지 기억한다)
(function () {
  'use strict';
  var root = document.documentElement;
  var KEY = 'auction.ui';
  var MOBILE_MAX_WIDTH = 600;      // 이보다 좁은 창은 PC 브라우저여도 모바일 화면으로 본다
  var PHONE_MAX_SHORT_SIDE = 600;  // 터치 기기에서 짧은 변이 이보다 작으면 폰 (태블릿은 PC 화면)

  function storage() {
    try { return window.sessionStorage; } catch (e) { return null; }
  }

  function forcedMode() {
    var store = storage();
    var q = null;
    try { q = new URLSearchParams(location.search).get('ui'); } catch (e) { /* 오래된 브라우저 */ }
    if (q === 'auto' && store) { try { store.removeItem(KEY); } catch (e) { /* 무시 */ } return null; }
    if ((q === 'mobile' || q === 'desktop') && store) { try { store.setItem(KEY, q); } catch (e) { /* 무시 */ } return q; }
    if (q === 'mobile' || q === 'desktop') return q;
    var saved = null;
    if (store) { try { saved = store.getItem(KEY); } catch (e) { /* 무시 */ } }
    return saved === 'mobile' || saved === 'desktop' ? saved : null;
  }

  function uaIsPhone() {
    var uad = navigator.userAgentData;
    if (uad && typeof uad.mobile === 'boolean' && uad.mobile) return true;
    return /Android.+Mobile|iPhone|iPod|Windows Phone|webOS|BlackBerry|Opera Mini|IEMobile/i.test(navigator.userAgent || '');
  }

  function coarseSmallScreen() {
    var coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
    var s = window.screen || {};
    var shortSide = Math.min(s.width || 9999, s.height || 9999);
    return coarse && shortSide < PHONE_MAX_SHORT_SIDE;
  }

  var forced = forcedMode();
  var phone = uaIsPhone() || coarseSmallScreen();   // 기기 자체는 실행 중에 바뀌지 않는다

  function isLandscape() {
    // 키보드가 올라오면 창 높이가 줄어드니, 창 크기가 아니라 기기의 회전 상태를 본다.
    var so = window.screen && window.screen.orientation;
    if (so && typeof so.type === 'string') return so.type.indexOf('landscape') === 0;
    if (typeof window.orientation === 'number') return Math.abs(window.orientation) === 90;
    return window.innerWidth > window.innerHeight;
  }

  function apply() {
    var mobile = forced ? forced === 'mobile' : (phone || window.innerWidth <= MOBILE_MAX_WIDTH);
    root.setAttribute('data-device', mobile ? 'mobile' : 'desktop');
    root.setAttribute('data-orient', isLandscape() ? 'landscape' : 'portrait');
  }

  apply();
  var pending = false;
  function schedule() {
    if (pending) return;
    pending = true;
    (window.requestAnimationFrame || window.setTimeout)(function () { pending = false; apply(); });
  }
  window.addEventListener('resize', schedule);
  window.addEventListener('orientationchange', schedule);
})();
