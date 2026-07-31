/* ==========================================================================
   KINGKONG HYPER-GLASS — immersive.js
   --------------------------------------------------------------------------
   화면을 "평면"이 아니라 "공간"처럼 만들어주는 3D 레이어.
   기존 기능/이벤트에는 손대지 않고, CSS 변수만 채워 넣는 방식으로 동작한다.
     1) 포인터 광원  : 카드/버튼에 --px, --py (마우스 위치 %)
     2) 3D 틸트      : 카드에 --tilt-x, --tilt-y (최대 ±6deg)
     3) 배경 패럴랙스: .bg-stage 에 --mx, --my (최대 ±14px)
     4) 등장 스태거  : .content 직계 자식이 순서대로 떠오른다
   접근성/성능 규칙
     - prefers-reduced-motion: reduce → 틸트·패럴랙스·등장연출 모두 끈다
     - 터치 기기 / 768px 이하 → 틸트·패럴랙스 끈다 (배터리·스크롤 비용)
     - 모든 갱신은 requestAnimationFrame 으로 1프레임에 1회만 반영한다
   ========================================================================== */
(function () {
  'use strict';

  var TILT_MAX = 6;        // deg
  var PARALLAX_MAX = 14;   // px

  var reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function isCompact() {
    // 터치 전용 기기이거나 모바일 폭이면 3D 연산을 하지 않는다.
    var coarse = window.matchMedia && window.matchMedia('(hover: none)').matches;
    return coarse || window.innerWidth <= 768;
  }

  /* ---------------------------------------------------------------
     1 + 2. 포인터 광원 & 3D 틸트
     --------------------------------------------------------------- */
  // 광원만 받는 요소(버튼류)와 틸트까지 받는 요소(카드류)를 구분한다.
  var GLOW_SELECTOR = '.card, .summary-card, .product-card, button.primary, .qio-card, .keypad-btn';
  var TILT_SELECTOR = '.card, .summary-card, .product-card';

  var pending = null;   // { el, x, y, tiltX, tiltY }
  var frame = 0;

  function flush() {
    frame = 0;
    if (!pending) return;
    var p = pending;
    pending = null;
    var s = p.el.style;
    s.setProperty('--px', p.x + '%');
    s.setProperty('--py', p.y + '%');
    if (p.tiltX !== null) {
      s.setProperty('--tilt-x', p.tiltX + 'deg');
      s.setProperty('--tilt-y', p.tiltY + 'deg');
    }
  }

  function onPointerMove(e) {
    if (e.pointerType === 'touch') return;

    var el = e.target && e.target.closest ? e.target.closest(GLOW_SELECTOR) : null;
    if (!el) return;
    // 입력 폼 행으로 쓰이는 카드는 움직이지 않는다(입력 중 흔들림 방지).
    if (el.classList.contains('product-row')) return;

    var r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;

    var rx = (e.clientX - r.left) / r.width;    // 0~1
    var ry = (e.clientY - r.top) / r.height;    // 0~1

    var tiltX = null, tiltY = null;
    if (!reduceMotion && !isCompact() && el.matches(TILT_SELECTOR)) {
      // 위쪽을 누르면 위로 젖혀지도록 부호를 맞춘다.
      tiltX = ((0.5 - ry) * TILT_MAX).toFixed(2);
      tiltY = ((rx - 0.5) * TILT_MAX).toFixed(2);
    }

    pending = {
      el: el,
      x: (rx * 100).toFixed(1),
      y: (ry * 100).toFixed(1),
      tiltX: tiltX,
      tiltY: tiltY
    };
    if (!frame) frame = requestAnimationFrame(flush);
  }

  function onPointerOut(e) {
    var el = e.target && e.target.closest ? e.target.closest(GLOW_SELECTOR) : null;
    if (!el) return;
    // 자식 요소로 이동한 경우는 무시한다.
    if (e.relatedTarget && el.contains(e.relatedTarget)) return;
    el.style.removeProperty('--tilt-x');
    el.style.removeProperty('--tilt-y');
  }

  /* ---------------------------------------------------------------
     3. 배경 스테이지 패럴랙스
     --------------------------------------------------------------- */
  var stage = null;
  var stagePending = null;
  var stageFrame = 0;

  function flushStage() {
    stageFrame = 0;
    if (!stage || !stagePending) return;
    stage.style.setProperty('--mx', stagePending.x + 'px');
    stage.style.setProperty('--my', stagePending.y + 'px');
    stagePending = null;
  }

  function onStageMove(e) {
    if (!stage || reduceMotion || isCompact()) return;
    if (e.pointerType === 'touch') return;
    var nx = (e.clientX / window.innerWidth) - 0.5;
    var ny = (e.clientY / window.innerHeight) - 0.5;
    stagePending = {
      x: (-nx * PARALLAX_MAX).toFixed(1),
      y: (-ny * PARALLAX_MAX).toFixed(1)
    };
    if (!stageFrame) stageFrame = requestAnimationFrame(flushStage);
  }

  /* ---------------------------------------------------------------
     4. 등장 스태거
     스크롤 관찰 방식은 "탭에 숨겨진 카드가 영원히 안 보이는" 사고가 날 수 있어
     쓰지 않는다. 로드 직후 순서대로 한 번만 떠오르게 하고, 실패해도
     failsafe 타이머가 무조건 원상복구한다.
     --------------------------------------------------------------- */
  function playEntrance() {
    if (reduceMotion) return;
    var content = document.querySelector('.content');
    if (!content) return;

    var items = [];
    var children = content.children;
    for (var i = 0; i < children.length && items.length < 10; i++) {
      var el = children[i];
      var tag = el.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEMPLATE') continue;
      items.push(el);
    }

    items.forEach(function (el, idx) {
      el.classList.add('reveal-init');
      setTimeout(function () {
        el.classList.add('reveal-in');
      }, 40 + idx * 55);
    });

    // 어떤 이유로든 전환이 끝나지 않아도 1.6초 뒤에는 반드시 정상 상태로 되돌린다.
    setTimeout(function () {
      items.forEach(function (el) {
        el.classList.remove('reveal-init');
        el.classList.remove('reveal-in');
      });
    }, 1600);
  }

  /* --------------------------------------------------------------- */
  function init() {
    stage = document.querySelector('.bg-stage');

    document.addEventListener('pointermove', onPointerMove, { passive: true });
    document.addEventListener('pointerout', onPointerOut, { passive: true });
    document.addEventListener('pointermove', onStageMove, { passive: true });

    playEntrance();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
