/* 어드민 매장 리포트 공용 도구 (admin_reports.html / admin_report_store.html) */
(function () {
  const AR = {};
  window.AR = AR;

  AR.esc = function (v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  };
  AR.num = function (n) { return n == null ? '-' : Number(n).toLocaleString('ko-KR'); };
  AR.won = function (n) { return n == null ? '-' : Number(n).toLocaleString('ko-KR') + '원'; };
  AR.pct = function (n) { return n == null ? '-' : n + '%'; };
  AR.short = function (n) {
    if (n == null) return '-';
    const a = Math.abs(n);
    if (a >= 100000000) return (n / 100000000).toFixed(1) + '억';
    if (a >= 10000) return Math.round(n / 10000).toLocaleString('ko-KR') + '만';
    return Number(n).toLocaleString('ko-KR');
  };

  // 증감률 배지 (지난 기간 대비). 지난 기간이 0이면 '-'
  AR.delta = function (v) {
    if (v == null) return '<span class="ar-delta flat">-</span>';
    const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
    const arrow = v > 0 ? '▲' : v < 0 ? '▼' : '–';
    return `<span class="ar-delta ${cls}">${arrow} ${Math.abs(v)}%</span>`;
  };

  AR.statusBadge = function (s) {
    const map = { '품절': 'badge-danger', '부족': 'badge-warning', '정상': 'badge-success', '과다': 'badge-accent', '부진': 'badge-neutral' };
    return `<span class="badge ${map[s] || 'badge-neutral'}">${AR.esc(s)}</span>`;
  };

  AR.api = async function (url, params) {
    const q = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v !== '' && v != null) q.set(k, v); });
    const res = await fetch(url + (q.toString() ? '?' + q : ''), { credentials: 'same-origin' });
    let data = null;
    try { data = await res.json(); } catch (e) { /* JSON 아님 */ }
    if (!res.ok) throw new Error((data && data.error) || '불러오지 못했습니다. (' + res.status + ')');
    return data;
  };

  // ---- 기간 선택 ----
  AR.period = { period: '7d', start_date: '', end_date: '' };
  (function readUrl() {
    const q = new URLSearchParams(location.search);
    const p = q.get('period');
    if (['today', '7d', '30d', 'month', 'custom'].includes(p)) AR.period.period = p;
    if (p === 'custom') { AR.period.start_date = q.get('start_date') || ''; AR.period.end_date = q.get('end_date') || ''; }
  })();

  AR.periodQuery = function () {
    const o = { period: AR.period.period };
    if (AR.period.period === 'custom') { o.start_date = AR.period.start_date; o.end_date = AR.period.end_date; }
    return o;
  };
  AR.periodString = function () { return new URLSearchParams(AR.periodQuery()).toString(); };

  AR.renderPeriodBar = function (el, onChange) {
    const opts = [['today', '오늘'], ['7d', '7일'], ['30d', '30일'], ['month', '이번 달'], ['custom', '직접 지정']];
    el.innerHTML = '<div class="ar-seg" role="group" aria-label="기간 선택">' +
      opts.map(([k, l]) => `<button type="button" data-k="${k}" class="${AR.period.period === k ? 'active' : ''}">${l}</button>`).join('') +
      '</div><span class="ar-custom" style="display:' + (AR.period.period === 'custom' ? 'inline-flex' : 'none') + '">' +
      `<input type="date" id="arStart" value="${AR.esc(AR.period.start_date)}"> ~ <input type="date" id="arEnd" value="${AR.esc(AR.period.end_date)}">` +
      '<button type="button" class="ar-btn" id="arApply">적용</button></span>';
    const custom = el.querySelector('.ar-custom');
    el.querySelectorAll('.ar-seg button').forEach(b => b.addEventListener('click', () => {
      AR.period.period = b.dataset.k;
      el.querySelectorAll('.ar-seg button').forEach(x => x.classList.toggle('active', x === b));
      custom.style.display = b.dataset.k === 'custom' ? 'inline-flex' : 'none';
      if (b.dataset.k !== 'custom') { AR.syncUrl(); onChange(); }
    }));
    el.querySelector('#arApply').addEventListener('click', () => {
      const s = el.querySelector('#arStart').value, e = el.querySelector('#arEnd').value;
      if (!s || !e) { alert('시작일과 종료일을 모두 선택하세요.'); return; }
      if (s > e) { alert('시작일이 종료일보다 늦습니다.'); return; }
      AR.period.start_date = s; AR.period.end_date = e;
      AR.syncUrl(); onChange();
    });
  };

  AR.syncUrl = function () {
    try { history.replaceState(null, '', location.pathname + '?' + AR.periodString() + (location.hash || '')); } catch (e) { /* 무시 */ }
  };

  // ---- 차트 ----
  AR.charts = {};
  AR.chart = function (id, config) {
    if (AR.charts[id]) AR.charts[id].destroy();
    const el = document.getElementById(id);
    if (!el || typeof Chart === 'undefined') return null;
    config.options = Object.assign({ responsive: true, maintainAspectRatio: false, animation: false }, config.options || {});
    AR.charts[id] = new Chart(el.getContext('2d'), config);
    return AR.charts[id];
  };
  // 블루 포인트 1색 + 중립 톤 (매장 구분은 명도/점선으로)
  AR.palette = ['#3B6FE0', '#8FB0F0', '#6B6F76', '#B4B7BE', '#1F3F8F', '#C9D8F7'];
  AR.dashes = [[], [], [6, 4], [2, 3], [], [6, 4]];

  // ---- 표 ----
  AR.table = function (cols, rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return `<div class="ar-empty">${AR.esc(opts.empty || '데이터가 없습니다.')}</div>`;
    const head = cols.map(c => `<th class="${c.num ? 'num' : ''}">${AR.esc(c.label)}</th>`).join('');
    const body = rows.map(r => '<tr>' + cols.map(c => {
      const raw = c.get ? c.get(r) : r[c.key];
      const html = c.html ? raw : AR.esc(raw == null ? '-' : raw);
      return `<td class="${c.num ? 'num' : ''}">${html}</td>`;
    }).join('') + '</tr>').join('');
    return `<div class="table-wrap"><table class="ar-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  };

  // ---- 화면 저장 ----
  AR.saveImage = async function (targetId, filename) {
    const el = document.getElementById(targetId);
    if (!window.html2canvas) { alert('이미지 저장 도구를 불러오지 못했습니다. 인쇄(PDF 저장)를 이용해 주세요.'); return; }
    const bg = getComputedStyle(document.body).backgroundColor;
    const canvas = await html2canvas(el, { backgroundColor: bg, scale: 2 });
    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/png');
    a.download = filename || 'report.png';
    a.click();
  };

  // ---- 어드민 전용 공유 링크 (로그인한 어드민만 열 수 있음, 7일 유효) ----
  AR.shareLink = async function () {
    try {
      const res = await fetch('/api/admin/reports/share', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: location.pathname + location.search + location.hash }),
      });
      const j = await res.json();
      if (!res.ok) throw new Error(j.error || '링크를 만들지 못했습니다.');
      try { await navigator.clipboard.writeText(j.url); alert('링크를 복사했습니다. 어드민 로그인 상태에서만 열리며 ' + j.expires_days + '일간 유효합니다.\n' + j.url); }
      catch (e) { window.prompt('아래 링크를 복사하세요 (어드민 로그인 필요, ' + j.expires_days + '일 유효)', j.url); }
    } catch (e) { alert(e.message || '링크를 만들지 못했습니다.'); }
  };

  // ---- 재고 이동 화면 연결 (출발·도착 매장, 제품, 수량 미리 채움) ----
  AR.transferUrl = function (r) {
    const q = new URLSearchParams({ from: r.from_store_id, to: r.to_store_id, pid: r.product_id, qty: r.suggest_qty, name: r.name || r.product || '' });
    return '/transfer?' + q.toString();
  };

  AR.today = function () {
    const d = new Date(Date.now() + 9 * 3600 * 1000); // KST
    return d.toISOString().slice(0, 10);
  };
})();
