/* Gauge: user classification form ("내 양식"). Upload an Excel form, apply it to the collected products, see the bound table.
   Loaded before app.js; app.js calls GaugeTemplate.mount(body, job, groups) once per rendered result.
   CSP-safe: no inline script, every server string is inserted with textContent (el() never uses innerHTML). */
(function () {
  'use strict';
  const MAX_BYTES = 5 * 1024 * 1024;
  const KEY = 'gauge.tpl';
  const STATUS = { found: '찾음', derived: '계산', absent: '없음', unknown: '정보 없음' };
  const st = { id: null, summary: null, jobId: null, bound: null, view: 'base', busy: false, nodes: {} };

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
      else if (k === 'class') n.className = v;
      else n.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const nz = (...a) => a.flat().filter((x) => x != null && x !== false);   // DOM append/replaceChildren would print the text 'null'
  const store = {
    get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
    set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) { /* private mode: fine */ } },
  };

  async function api(path, opts) {
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw new Error('서버에 연결하지 못했습니다.'); }
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const d = body && body.detail;
      throw Object.assign(new Error(typeof d === 'string' ? d : '요청을 처리하지 못했습니다.'), { status: res.status });
    }
    return body;
  }

  function say(msg, bad) {
    const n = st.nodes.status;
    if (!n) return;
    n.textContent = msg || '';
    n.classList.toggle('bad', !!bad);
  }

  /* ---------------------------------------------------------------- upload */
  async function upload(file) {
    if (!file) return;
    if (!/\.xlsx$/i.test(file.name)) { say('엑셀 .xlsx 파일만 올릴 수 있습니다. (.xls, .xlsm은 지원하지 않습니다)', true); return; }
    if (file.size > MAX_BYTES) { say('파일이 너무 큽니다. (최대 5 MB)', true); return; }
    st.busy = true; render();
    say('양식을 읽는 중…');
    try {
      const r = await api('/api/template', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) }, body: file });
      st.id = r.template_id; st.summary = r.summary; st.bound = null; st.view = 'base';
      store.set(st.id);
      say(`양식을 읽었습니다: 항목 ${r.summary.item_count}개`);
    } catch (e) { say(e.message, true); }
    st.busy = false; render();
  }

  async function drop() {
    if (!st.id) return;
    try { await api('/api/template/' + st.id, { method: 'DELETE' }); } catch (e) { /* already gone */ }
    st.id = st.summary = st.bound = null; st.view = 'base'; store.set(null);
    say('양식을 삭제했습니다.');
    render();
  }

  async function apply() {
    if (!st.id || !st.jobId || st.busy) return;
    st.busy = true; render();
    say('양식 기준으로 정리하는 중… (로컬 모델을 쓰면 시간이 걸릴 수 있습니다)');
    try {
      st.bound = await api(`/api/template/${st.id}/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: st.jobId }) });
      st.view = 'tpl';
      const c = st.bound.counts;
      say(`적용 완료: 찾음 ${c.found} · 계산 ${c.derived} · 없음 ${c.absent} · 정보 없음 ${c.unknown} · 검토 ${c.review}`);
    } catch (e) {
      say(e.message, true);
      if (e.status === 404 && /양식/.test(e.message)) { st.id = st.summary = null; store.set(null); }
    }
    st.busy = false; render();
  }

  /* ---------------------------------------------------------------- pieces */
  function uploadZone() {
    const input = el('input', { type: 'file', id: 'tpl-file', class: 'vh', accept: '.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'aria-describedby': 'tpl-hint',
      onchange: (e) => { upload(e.target.files[0]); e.target.value = ''; } });
    const zone = el('div', { class: 'tpl-drop', 'aria-busy': st.busy ? 'true' : null },
      input,
      el('label', { class: 'btn' + (st.id ? '' : ' primary'), for: 'tpl-file' }, st.id ? '다른 양식 올리기' : '양식 파일 선택'),
      el('span', { class: 'tpl-or' }, '또는 여기로 끌어다 놓기'));
    ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add('drag'); }));
    ['dragleave', 'dragend'].forEach((t) => zone.addEventListener(t, () => zone.classList.remove('drag')));
    zone.addEventListener('drop', (e) => { e.preventDefault(); zone.classList.remove('drag'); upload(e.dataTransfer && e.dataTransfer.files[0]); });
    return el('div', {},
      zone,
      el('p', { class: 'hint', id: 'tpl-hint' }, '.xlsx만, 5 MB·3000행 이하. 열: 구분 | 항목(KO) | Item(EN) | 동의어 | 단위 | 유형 | 비고 (머리글 이름은 한글·영문 모두 인식). 수식은 계산하지 않습니다. 샘플: ',
        el('a', { href: '/api/template/sample?group=cooking', download: '' }, '조리기기 양식'), ' · ',
        el('a', { href: '/api/template/sample?group=refrigerator', download: '' }, '냉장고 양식')));
  }

  function summaryBlock(s) {
    const cats = el('ul', { class: 'tpl-cats', 'aria-label': '양식의 구분' }, s.categories.map((c) => el('li', { class: 'tpl-chip' }, c.name, el('b', {}, String(c.items)))));
    const warn = s.warnings.length ? el('div', { class: 'banner', role: 'status' }, el('div', {}, '양식을 읽으며 확인한 점', el('ul', {}, s.warnings.map((w) => el('li', {}, w))))) : null;
    const head = el('tr', {}, ['구분', '항목', '단위', '유형'].map((h) => el('th', { scope: 'col' }, h)));
    const body = s.preview.map((i) => el('tr', {}, el('td', {}, i.category), el('th', { scope: 'row' }, i.label_ko || i.label_en, i.label_ko && i.label_en ? el('small', {}, i.label_en) : null),
      el('td', {}, i.unit), el('td', {}, ({ number: '숫자', flag: '예/아니오', text: '텍스트', list: '목록' })[i.type] || i.type, i.type_inferred ? el('small', {}, '추론') : null)));
    return el('div', { class: 'tpl-sum' },
      el('p', { class: 'tpl-file' }, el('b', {}, s.filename), ` · 항목 ${s.item_count}개 · 시트 ${s.sheets.map((x) => x.name + (x.group_ko ? ` (${x.group_ko})` : '')).join(', ')}`),
      cats, warn,
      el('details', { class: 'tpl-prev' }, el('summary', {}, `미리보기 (처음 ${s.preview.length}개)`),
        el('div', { class: 'tbl-wrap' }, el('table', { class: 'cmp tpl-mini' }, el('caption', { class: 'vh' }, '양식 항목 미리보기'), el('thead', {}, head), el('tbody', {}, body)))));
  }

  function coverageBlock(b) {
    const c = b.counts, pct = (n) => (c.total ? Math.round((100 * n) / c.total) : 0);
    const chips = ['found', 'derived', 'absent', 'unknown'].map((k) => el('li', { class: 'tpl-stat st-' + k }, el('span', { class: 'n' }, String(c[k])), el('span', {}, STATUS[k], ` ${pct(c[k])}%`)));
    chips.push(el('li', { class: 'tpl-stat st-review' }, el('span', { class: 'n' }, String(c.review)), el('span', {}, '검토 필요')));
    const rows = b.coverage.map((x) => {
      const bar = el('span', { class: 'tpl-bar', role: 'img', 'aria-label': `${x.category}: 찾음 ${x.found}, 계산 ${x.derived}, 없음 ${x.absent}, 정보 없음 ${x.unknown}` });
      for (const k of ['found', 'derived', 'absent', 'unknown']) { const seg = el('i', { class: 'st-' + k }); seg.style.width = (x.total ? (100 * x[k]) / x.total : 0) + '%'; bar.append(seg); }
      return el('li', {}, el('span', { class: 'tpl-cn' }, x.category), bar, el('span', { class: 'tpl-cp mono' }, `${x.found + x.derived}/${x.total} · ${x.pct_found + x.pct_derived}%`), x.review ? el('span', { class: 'tpl-rv' }, `검토 ${x.review}`) : null);
    });
    return el('div', { class: 'tpl-cov' }, el('h4', {}, '채워진 정도'), el('ul', { class: 'tpl-stats' }, chips), el('ul', { class: 'tpl-covrows' }, rows),
      el('p', { class: 'hint' }, '‘정보 없음’은 수집한 사양에서 찾지 못한 것이고, ‘없음’은 사양에 없다고 적혀 있는 경우입니다.'));
  }

  const num = (v) => (typeof v === 'number' ? String(Math.round(v * 100) / 100) : String(v));
  function cellNode(c, unit) {
    const td = el('td', { class: 'tpl-c st-' + c.status });
    if (c.status === 'unknown') td.append(el('span', { class: 'tpl-unk' }, '정보 없음'));
    else if (c.status === 'absent') td.append(el('span', { class: 'tpl-no', 'aria-hidden': 'true' }, '–'), el('span', { class: 'vh' }, '없음 (사양에 없다고 명시됨)'));
    else {
      const v = c.value;
      if (v === true) td.append(...nz(el('span', { class: 'tpl-yes', 'aria-hidden': 'true' }, '✓'), el('span', { class: 'vh' }, '있음'), c.note ? el('small', { class: 'tpl-n' }, c.note) : null));
      else if (Array.isArray(v)) td.append(el('span', { class: 'tpl-list' }, v.join(', ')));
      else if (typeof v === 'number') td.append(...nz(el('span', { class: 'mono' }, num(v)), c.unit && c.unit !== unit ? el('span', { class: 'tpl-u' }, ' ' + c.unit) : null));
      else td.append(el('span', {}, String(v == null ? c.display : v)));
      if (c.status === 'derived') td.append(el('span', { class: 'tpl-tag', title: '품목 목록에서 계산한 값' }, '계산'));
      if (c.needs_review) td.append(el('span', { class: 'tpl-rv', title: '매칭을 확인하세요' }, '검토'));
      if (c.source_label) td.append(el('small', { class: 'tpl-src' }, c.source_label));
    }
    const tip = (c.sources || []).map((s) => `${s.label} = ${s.value}`);
    if (c.method) tip.push(`매칭: ${c.method} ${c.score}`);
    if (c.note) tip.push(c.note);
    if (tip.length) td.title = tip.join('\n');
    return td;
  }

  function boundTable(sheet) {
    const head = el('tr', {}, el('th', { scope: 'col', class: 'lab' }, '항목'), el('th', { scope: 'col', class: 'tpl-h-unit' }, '단위'),
      sheet.products.map((p) => el('th', { scope: 'col', class: 'tpl-h-p' }, el('span', { class: 'ph-brand' }, p.brand), el('span', { class: 'ph-model' }, p.model))));
    const body = [];
    let cat = null;
    for (const r of sheet.rows) {
      if (r.item.category !== cat) {
        cat = r.item.category;
        body.push(el('tr', { class: 'tpl-cat group' }, el('th', { scope: 'colgroup', colspan: String(2 + sheet.products.length) }, cat || '(구분 없음)')));
      }
      const unk = r.cells.every((c) => c.status === 'unknown');
      const label = r.item.label_ko || r.item.label_en;
      body.push(el('tr', { class: (r.needs_review ? 'rv ' : '') + (unk ? 'allunk' : '') },
        el('th', { scope: 'row', class: 'lab' }, label, r.item.label_ko && r.item.label_en ? el('small', {}, r.item.label_en) : null,
          r.method && r.method !== 'none' ? el('small', { class: 'tpl-src', title: `${r.method} ${r.score}` }, `→ ${r.matched_label || r.canon_id}`) : null),
        el('td', { class: 'tpl-unit' }, r.item.unit), r.cells.map((c) => cellNode(c, r.item.unit))));
    }
    return el('div', { class: 'tbl-wrap' }, el('table', { class: 'cmp tpl-table' }, el('caption', { class: 'vh' }, `${sheet.name} — 내 양식 기준 비교표`), el('thead', {}, head), el('tbody', {}, body)));
  }

  function boundView(b) {
    const wrap = el('div', { class: 'tpl-bound' });
    const tbls = [];
    const opt = (label, cls) => el('label', { class: 'tpl-opt' }, el('input', { type: 'checkbox', onchange: (e) => tbls.forEach((t) => t.classList.toggle(cls, e.target.checked)) }), el('span', {}, label));
    wrap.append(el('div', { class: 'tpl-opts' }, opt('원문 항목명 보기', 'show-src'), opt('검토 필요한 행만', 'only-rv'), opt('모두 정보 없음인 행 숨기기', 'hide-unk')));
    for (const sh of b.sheets) {
      const box = boundTable(sh);
      tbls.push(box.firstChild);
      wrap.append(el('h4', { class: 'tpl-sh' }, sh.name, el('span', { class: 'tpl-shs' }, ` · 제품 ${sh.products.length}개 · 항목 ${sh.row_count}개`)), box);
      if (sh.row_count > sh.rows.length) wrap.append(el('p', { class: 'hint' }, `화면에는 처음 ${sh.rows.length}행만 보여줍니다. 전체는 엑셀로 내려받아 확인하세요.`));
      if (sh.uncovered_count) {
        const head = el('tr', {}, el('th', { scope: 'col' }, '제안 구분'), el('th', { scope: 'col' }, '경쟁사 항목'), sh.products.map((p) => el('th', { scope: 'col' }, p.model)));
        const rows = sh.uncovered.map((u) => el('tr', {}, el('td', {}, u.suggested_category || '—'), el('th', { scope: 'row' }, u.key_ko || u.key_en, u.key_ko && u.key_en ? el('small', {}, u.key_en) : null), u.values.map((v) => el('td', {}, v))));
        wrap.append(el('details', { class: 'tpl-extra' }, el('summary', {}, `양식 외 항목 ${sh.uncovered_count}개 (양식에 추가할 후보)`),
          el('div', { class: 'tbl-wrap' }, el('table', { class: 'cmp tpl-mini' }, el('caption', { class: 'vh' }, '양식에 없는 경쟁사 항목'), el('thead', {}, head), el('tbody', {}, rows))),
          sh.uncovered_count > sh.uncovered.length ? el('p', { class: 'hint' }, `처음 ${sh.uncovered.length}개만 표시합니다. 전체는 엑셀의 ‘양식 외 항목’ 시트에 있습니다.`) : null));
      }
    }
    if (b.warnings && b.warnings.length) wrap.append(el('div', { class: 'banner' }, el('div', {}, b.warnings.join(' '))));
    return wrap;
  }

  /* ---------------------------------------------------------------- panel */
  function setView(v) {
    st.view = v; applyView();
    const sw = st.nodes.switch;
    if (sw) sw.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === st.view)));
  }
  function applyView() {
    const host = st.nodes.host;
    if (!host || !host.parentNode) return;
    const tpl = st.view === 'tpl' && st.bound;
    host.parentNode.querySelectorAll(':scope > .tabset, :scope > .catview').forEach((n) => { n.hidden = !!tpl; });
    if (st.nodes.view) st.nodes.view.hidden = !tpl;
  }

  function render() {
    const host = st.nodes.host;
    if (!host) return;
    const body = st.nodes.body;
    const open = st.nodes.toggle.getAttribute('aria-expanded') === 'true';
    body.hidden = !open;
    const keep = st.nodes.status;
    const sw = el('div', { class: 'tpl-switch', role: 'group', 'aria-label': '비교표 기준' },
      el('button', { type: 'button', 'data-v': 'base', 'aria-pressed': String(st.view !== 'tpl' || !st.bound), onclick: () => setView('base') }, 'Gauge 기본 비교표'),
      el('button', { type: 'button', 'data-v': 'tpl', 'aria-pressed': String(st.view === 'tpl' && !!st.bound), 'aria-disabled': st.bound ? null : 'true',
        title: st.bound ? null : '양식을 올리고 ‘이 제품들에 적용’을 누르면 사용할 수 있습니다', onclick: () => { if (st.bound) setView('tpl'); else { st.nodes.toggle.setAttribute('aria-expanded', 'true'); render(); say('먼저 양식을 올리고 적용하세요.'); } } }, '내 양식 기준'));
    st.nodes.switch = sw;
    const actions = st.id ? el('div', { class: 'tpl-acts' },
      el('button', { class: 'btn primary', type: 'button', disabled: st.busy || !st.jobId ? '' : null, onclick: apply }, st.busy ? '처리 중…' : '이 제품들에 적용'),
      st.bound ? el('a', { class: 'btn', href: safeUrl(st.bound.download_url), download: '' }, '채워진 엑셀 내려받기') : null,
      el('button', { class: 'btn ghost', type: 'button', disabled: st.busy ? '' : null, onclick: drop }, '양식 삭제')) : null;
    body.replaceChildren(...nz(uploadZone(), keep, st.summary ? summaryBlock(st.summary) : null, actions, st.bound ? coverageBlock(st.bound) : null));
    st.nodes.head.replaceChildren(sw);
    if (st.nodes.view) st.nodes.view.replaceChildren(st.bound ? boundView(st.bound) : '');
    st.nodes.toggle.firstChild.textContent = st.summary ? `내 분류양식 · ${st.summary.filename}` : '내 분류양식 (엑셀로 항목 기준 정리)';
    applyView();
  }
  const safeUrl = (u) => (typeof u === 'string' && /^\/api\/template\/[0-9a-f]{16}\/download\?job_id=[0-9a-f]{12}$/.test(u) ? u : '#');

  function mount(body, job, groups) {
    if (!body || !job) return;
    const old = body.querySelector(':scope > .tpl');
    if (old) old.remove();
    st.jobId = job.id; st.bound = null; st.view = 'base';
    const status = el('p', { class: 'tpl-status', role: 'status', 'aria-live': 'polite' });
    const toggle = el('button', { class: 'tpl-tg', type: 'button', 'aria-expanded': 'false', 'aria-controls': 'tpl-body',
      onclick: () => { toggle.setAttribute('aria-expanded', String(toggle.getAttribute('aria-expanded') !== 'true')); render(); } }, el('span', {}), el('span', { class: 'tpl-chev', 'aria-hidden': 'true' }, '▾'));
    const head = el('div', { class: 'tpl-sw' });
    const pbody = el('div', { class: 'tpl-body', id: 'tpl-body' });
    const view = el('div', { class: 'tpl-view', hidden: '' });
    const host = el('section', { class: 'tpl', 'aria-label': '내 분류양식' }, el('div', { class: 'tpl-top' }, toggle, head), pbody);
    Object.assign(st.nodes, { host, body: pbody, status, toggle, head, view });
    const first = body.querySelector(':scope > .tabset, :scope > .catview');
    body.insertBefore(host, first);
    body.insertBefore(view, first);
    render();
    const saved = st.id || store.get();
    if (saved && !st.summary) {
      api('/api/template/' + saved).then((r) => { st.id = r.template_id; st.summary = r.summary; render(); }).catch(() => { store.set(null); });
    }
  }

  window.GaugeTemplate = { mount };
})();
