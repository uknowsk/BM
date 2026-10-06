/* Gauge UI — vanilla JS, no build step. All server data is inserted with textContent (never innerHTML). */
(() => {
'use strict';

const SECONDS_PER_PRODUCT = 25;      // honest rough estimate: page load + polite delay + PDFs
const SECONDS_PER_MODES = 150;       // local LLM mode extraction, per product
const MODES_CATEGORY = 'refrigerator'; // operating-mode extraction exists for refrigerators only
const BANDS = [
  { key: 'budget',  title: 'Budget',  ko: '저가',  color: 'var(--b-budget)' },
  { key: 'mid',     title: 'Mid',     ko: '중간',  color: 'var(--b-mid)' },
  { key: 'premium', title: 'Premium', ko: '고가',  color: 'var(--b-premium)' },
  { key: 'unknown', title: '가격 미확인', ko: '', color: 'var(--b-unknown)' },
];

/* ---------- tiny helpers ---------- */
const $ = (s, r = document) => r.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  const add = (c) => { if (c == null || c === false) return; if (Array.isArray(c)) c.forEach(add); else n.append(c.nodeType ? c : document.createTextNode(String(c))); };
  kids.forEach(add);
  return n;
}
function icon(d, cls) {
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
  if (cls) s.setAttribute('class', cls);
  const p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); s.append(p);
  return s;
}
const I = {
  ext: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
  check: 'M5 12.5l4.5 4.5L19 7.5', x: 'M6 6l12 12M18 6L6 18', search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  warn: 'M12 3l10 18H2L12 3zM12 10v5M12 18v.5', box: 'M3 7l9-4 9 4v10l-9 4-9-4V7zM3 7l9 4 9-4M12 11v10',
  refrigerator: 'M7 3h10a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM6 10h12M9 6v2M9 13v3',
  washer: 'M6 3h12a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM5 8h14M12 11.5a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM8 5.5h.01',
  cooking: 'M4 4h16v16H4zM4 10h16M8 7h.01M12 7h.01M16 7h.01M8 13.5h8V17H8z',
};
const usd = (n) => '$' + Math.round(n).toLocaleString('en-US');
const num1 = (n) => (Math.round(n * 10) / 10).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 1 });
/* mock mode (server FRIDGE_MOCK=1 + ?mock=1) keeps its own storage keys so sample data never mixes with real history */
const LS = window.__GAUGE_MOCK__ ? 'gauge.mock.' : 'gauge.';
/* defense in depth: only https: URLs from scraped data may become link targets */
const httpsUrl = (u) => { try { return new URL(String(u)).protocol === 'https:' ? String(u) : null; } catch { return null; } };
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } };
const norm = (s) => String(s || '').toLowerCase();
let toastTimer;
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 4200); }

async function api(path, opts) {
  let r;
  try { r = await fetch(path, opts); } catch { throw new Error('서버에 연결할 수 없습니다. python server.py 가 실행 중인지 확인하세요.'); }
  let body = null; try { body = await r.json(); } catch { /* not json */ }
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join(', ') : `요청 실패 (${r.status})`);
  }
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });

function poll(id, onTick, ctl) {
  return new Promise((resolve, reject) => {
    let fails = 0;
    const tick = async () => {
      if (ctl.stop) return;
      try {
        const j = await api(`/api/jobs/${id}`); fails = 0; onTick(j);
        if (['done', 'error', 'cancelled'].includes(j.status)) return resolve(j);
      } catch (e) { if (++fails >= 5) return reject(e); }
      setTimeout(tick, 650);
    };
    tick();
  });
}

/* ---------- state ----------
   brands:  [{name, enabled, note, cats:Set|null}]   cats = sub-category keys the brand supports (null = unknown -> all)
   cats:    [{key, label_ko, enabled, children:[{key,label_ko}]}]   major categories (대분류) and their sub-categories (소분류)
   picks:   Map "brand|majorKey" -> candidate   (ONE model per brand per major category; the server enforces it too) */
const S = {
  brands: [], cats: [], subs: new Map(), legacy: false,
  selBrands: new Set(), selMajors: new Set(), selSubs: new Set(), bandMode: 'auto', browser: 'auto',
  result: null, groups: [], notes: [], searched: [], picks: new Map(), filter: '', ctl: null, jobId: null, running: false,
  /* v2 shell: regionList [{key,label_ko,enabled,note,brands}], regions = selected region keys, hasRegions = /api/regions exists */
  v2: true, maxSel: 12, maxCombos: 120, brandQ: '', regionList: [], regions: new Set(['na']), hasRegions: false, hasFilters: false,
  byUrl: new Map(), fgroups: [], fsel: new Map(), funk: new Set(), searchKey: '', openMajor: null,
};
const keyOf = (x) => (typeof x === 'string' ? x : x && (x.key || x.subcategory || x.id));
const catLabel = (key) => (S.cats.find((c) => c.key === key) || {}).label_ko || key || '';
const subLabel = (c) => ((S.subs.get(c.subcategory) || {}).label_ko) || c.subcategory_ko || c.subcategory || '';
const brandByName = (n) => S.brands.find((b) => b.name === n);
/* a brand is usable when it is ready, supports the sub-category and (when /api/regions lists brands) sells in a selected region */
const regionOk = (b) => { const rs = S.regionList.filter((r) => S.regions.has(r.key) && Array.isArray(r.brands)); return !rs.length || rs.some((r) => r.brands.includes(b.name)); };
const supports = (b, sub) => !!b && b.enabled && regionOk(b) && (b.cats == null || b.cats.has(sub));
/* brands used to judge support: the selected ones, or every ready brand while none is selected */
const viewBrands = () => (S.selBrands.size ? S.brands.filter((b) => S.selBrands.has(b.name)) : S.brands.filter((b) => b.enabled));
const supporters = (sub) => viewBrands().filter((b) => supports(b, sub));

/* ---------- theme ---------- */
$('#theme').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('gauge.theme', next); } catch { /* ignore */ }
});

/* ---------- stages ---------- */
const STAGES = ['setup', 'candidates', 'collect', 'results'];
function showStage(name, scroll = true) {
  const idx = STAGES.indexOf(name);
  STAGES.forEach((s, i) => {
    if (s === 'setup' && S.v2) $('#shell').hidden = idx > 1;
    else if (s === 'candidates' && S.v2) $('#candidates').hidden = idx !== 1;
    else if (i > 0) $('#' + s).hidden = i > idx;
    const a = $(`.stages a[data-stage="${s}"]`);
    if (i > idx) a.setAttribute('aria-disabled', 'true'); else a.removeAttribute('aria-disabled');
    if (i === idx) a.setAttribute('aria-current', 'step'); else a.removeAttribute('aria-current');
    a.classList.toggle('seen', i < idx);
  });
  $('#bar').hidden = name !== 'candidates' || !S.result;
  document.body.classList.toggle('has-bar', !$('#bar').hidden);
  if (scroll && idx > 0) requestAnimationFrame(() => $('#' + name).scrollIntoView({ block: 'start' }));
  if (S.v2) { $('#v2-empty').hidden = !$('#candidates').hidden; $('#subbar').hidden = idx > 1; if (idx <= 1) closeSheets(); }
}

/* ---------- data normalisation ---------- */
function loadCatalog(brands, cats) {
  S.legacy = false;
  S.brands = brands.map((b) => ({ name: b.name, enabled: !!b.enabled, note: b.note || '', cats: Array.isArray(b.categories) ? new Set(b.categories.map(keyOf)) : null,
    group: b.group || '', countries: Array.isArray(b.countries) ? b.countries : [], delay: Number(b.delay_s) || 0 }));
  S.cats = cats.map((c) => {
    let kids = (c.children || c.subcategories || []).map((k) => ({ key: keyOf(k), label_ko: typeof k === 'string' ? k : (k.label_ko || k.label || keyOf(k)) }));
    if (!kids.length) { S.legacy = true; kids = [{ key: c.key, label_ko: c.label_ko }]; }   // older server: the major itself is the only "sub-category"
    return { key: c.key, label_ko: c.label_ko, enabled: c.enabled !== false, children: kids };
  });
  S.subs = new Map(S.cats.flatMap((c) => c.children.map((k) => [k.key, { label_ko: k.label_ko, major: c.key }])));
}

/* keep keyboard focus on the same control when a group is re-rendered */
function keepFocus(fn) {
  const a = document.activeElement, fk = a && a.dataset ? a.dataset.fk : null;
  fn();
  if (fk) { const n = [...document.querySelectorAll('[data-fk]')].find((x) => x.dataset.fk === fk); if (n) n.focus(); }
}
/* roving tabindex over the buttons of a group: arrows / Home / End move, one tab stop per group */
function roving(box) {
  const items = () => [...box.querySelectorAll('button[data-fk]')];
  const setCur = (cur) => items().forEach((b) => { b.tabIndex = b === cur ? 0 : -1; });
  setCur(items().find((b) => b.getAttribute('aria-pressed') === 'true') || items()[0]);
  if (box._rv) return; box._rv = true;                 // listeners are bound once per container (#cats is re-filled, not replaced)
  box.addEventListener('keydown', (e) => {
    const it = items(), i = it.indexOf(document.activeElement); if (i < 0) return;
    const n = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: it.length - 1 }[e.key];
    if (n == null) return;
    e.preventDefault(); const t = it[(n + it.length) % it.length]; setCur(t); t.focus();
  });
  box.addEventListener('focusin', (e) => { if (e.target.matches('button[data-fk]')) setCur(e.target); });
}

/* ---------- setup 1: brands ---------- */
function choice(type, name, id, value, title, sub, tag, checked, disabled, onchange) {
  const input = el('input', { type, name, id, value, checked, disabled, onchange });
  return el('div', { class: 'choice' + (type === 'radio' ? ' radio' : '') }, input,
    el('label', { for: id }, el('span', { class: 'n' }, title), sub ? el('span', { class: 's' }, sub) : null, tag ? el('span', { class: 'tag' }, tag) : null));
}
function renderBrands() {
  const box = $('#brands'); box.replaceChildren(el('legend', { class: 'vh' }, '브랜드 선택'));
  S.brands.forEach((b, i) => box.append(choice('checkbox', 'brand', 'b' + i, b.name, b.name, b.enabled || b.note === '준비 중' ? '' : (b.note || ''), b.enabled ? '' : '준비 중',
    b.enabled && S.selBrands.has(b.name), !b.enabled, (e) => { e.target.checked ? S.selBrands.add(b.name) : S.selBrands.delete(b.name); onSelectionChange(true); })));
}

/* ---------- setup 2: product groups (대분류) ---------- */
function majorState(c) {
  if (!c.enabled) return { ok: false, why: '준비 중인 제품군입니다.' };
  const kids = c.children.filter((k) => supporters(k.key).length);
  if (!kids.length) return { ok: false, why: `선택한 브랜드(${viewBrands().map((b) => b.name).join(', ') || '없음'}) 중 ${c.label_ko} 제품군을 지원하는 곳이 없습니다.` };
  const brands = new Set(kids.flatMap((k) => supporters(k.key).map((b) => b.name)));
  return { ok: true, kids: kids.length, brands: brands.size };
}
function renderMajors() {
  const box = $('#cats');
  box.replaceChildren(...S.cats.map((c) => {
    const st = majorState(c), on = st.ok && S.selMajors.has(c.key);
    return el('button', { class: 'tile', type: 'button', 'aria-pressed': String(on), 'aria-disabled': st.ok ? null : 'true', 'data-fk': 'major-' + c.key, title: st.ok ? null : st.why,
      onclick: () => {
        if (!st.ok) return toast(st.why);
        if (S.selMajors.has(c.key)) { S.selMajors.delete(c.key); c.children.forEach((k) => S.selSubs.delete(k.key)); } else S.selMajors.add(c.key);
        onSelectionChange(false);
      } },
      icon(I[c.key] || I.box, 'tile-i'), el('span', { class: 'tn' }, c.label_ko),
      el('span', { class: 'ts' }, st.ok ? `${st.brands}개 브랜드 · 소분류 ${st.kids}개` : (c.enabled ? '선택한 브랜드 미지원' : '준비 중')),
      el('span', { class: 'tick-c', 'aria-hidden': 'true' }, icon(I.check)), st.ok ? null : el('span', { class: 'vh' }, st.why));
  }));
  roving(box);
}

/* ---------- setup 3: sub-categories (소분류) ---------- */
function subChip(k, vb) {
  const sup = vb.filter((b) => supports(b, k.key)), ok = sup.length > 0, on = ok && S.selSubs.has(k.key);
  const miss = vb.filter((b) => !sup.includes(b));
  const why = ok ? `${sup.map((b) => b.name).join(', ')} 지원${miss.length ? ` · ${miss.map((b) => b.name).join(', ')} 미지원` : ''}`
    : `선택한 브랜드(${vb.map((b) => b.name).join(', ') || '없음'}) 중 지원하는 곳이 없어 선택할 수 없습니다.`;
  return el('button', { class: 'subc' + (ok ? '' : ' off'), type: 'button', 'aria-pressed': String(on), 'aria-disabled': ok ? null : 'true', 'data-fk': 'sub-' + k.key, title: why,
    onclick: () => { if (!ok) return toast(why); on ? S.selSubs.delete(k.key) : S.selSubs.add(k.key); onSubsChange(); } },
    el('span', { class: 'sl' }, k.label_ko),
    el('span', { class: 'sd' }, ok ? [el('span', { class: 'dots', 'aria-hidden': 'true' }, vb.map((b) => el('i', { class: supports(b, k.key) ? 'on' : '', title: b.name }, b.name[0]))),
      el('span', { class: 'cn', 'aria-hidden': 'true' }, `${sup.length}/${vb.length}`)] : el('span', { class: 'cn', 'aria-hidden': 'true' }, '미지원')),
    el('span', { class: 'vh' }, why));
}
function renderSubs() {
  const box = $('#subs'), majors = S.cats.filter((c) => S.selMajors.has(c.key)), vb = viewBrands();
  if (!majors.length) { box.replaceChildren(el('p', { class: 'hint', style: 'margin-top:0' }, '제품군을 선택하면 소분류를 고를 수 있습니다.')); return; }
  box.replaceChildren(...majors.map((c) => {
    const sup = c.children.filter((k) => supporters(k.key).length), selN = c.children.filter((k) => S.selSubs.has(k.key)).length;
    const allOn = sup.length > 0 && sup.every((k) => S.selSubs.has(k.key));
    const chips = el('div', { class: 'subs', role: 'group', 'aria-labelledby': 'sg-' + c.key }, c.children.map((k) => subChip(k, vb)));
    const g = el('div', { class: 'subgroup' },
      el('div', { class: 'sg-h' }, el('h3', { id: 'sg-' + c.key }, c.label_ko, el('span', { class: 'count' }, `${selN} / ${sup.length}개 선택`)),
        el('button', { class: 'link-btn', type: 'button', disabled: !sup.length, 'data-fk': 'all-' + c.key,
          onclick: () => { sup.forEach((k) => (allOn ? S.selSubs.delete(k.key) : S.selSubs.add(k.key))); onSubsChange(); } }, allOn ? '전체 해제' : '지원하는 항목 모두 선택')),
      chips);
    roving(chips); return g;
  }));
}
/* brand x sub-category coverage, read from /api/brands[].categories */
function renderMatrix() {
  const ready = S.brands.filter((b) => b.enabled), box = $('#matrix-body');
  if (S.legacy || !ready.length) { $('#matrix').hidden = true; return; }
  $('#matrix').hidden = false;
  const soon = S.brands.filter((b) => !b.enabled).map((b) => b.name);
  const cell = (b, k) => (supports(b, k.key) ? el('td', { class: 'v yes' }, icon(I.check), el('span', { class: 'vh' }, '지원')) : el('td', { class: 'v na' }, '–', el('span', { class: 'vh' }, '미지원')));
  const table = (c) => el('div', { class: 'tbl-wrap' }, el('table', { class: 'cmp mx-t' }, el('caption', {}, c.label_ko),
    el('thead', {}, el('tr', {}, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '브랜드')), c.children.map((k) => el('th', { scope: 'col' }, k.label_ko)))),
    el('tbody', {}, ready.map((b) => el('tr', {}, el('th', { class: 'lab', scope: 'row' }, b.name), c.children.map((k) => cell(b, k)))))));
  box.replaceChildren(...S.cats.map((c) => el('div', { class: 'mx' }, table(c))), ...(soon.length ? [el('p', { class: 'hint' }, `${soon.join(' · ')}: 준비 중`)] : []));
}

/* ---------- selection pruning, plan, summary, persistence ---------- */
function prune() {
  let dropped = 0;
  S.cats.forEach((c) => { if (S.selMajors.has(c.key) && !majorState(c).ok) { S.selMajors.delete(c.key); dropped++; } });
  S.selSubs.forEach((s) => { const m = (S.subs.get(s) || {}).major; if (!S.selMajors.has(m) || !supporters(s).length) { S.selSubs.delete(s); dropped++; } });
  return dropped;
}
function onSelectionChange(brandsChanged) {
  const dropped = brandsChanged ? prune() : 0;
  keepFocus(() => { renderMajors(); renderSubs(); if (S.v2) { renderBrandPop(); renderRail(); } });
  if (dropped) toast('선택한 브랜드가 지원하지 않는 제품군·소분류 선택을 해제했습니다.');
  updateSummary(); saveSel();
}
function onSubsChange() { keepFocus(() => { renderSubs(); if (S.v2) renderRail(); }); updateSummary(); saveSel(); }

/* which (brand, sub-category) pairs can actually be searched */
function plan() {
  const subs = [...S.selSubs], sel = S.brands.filter((b) => S.selBrands.has(b.name));
  const brands = sel.filter((b) => subs.some((s) => supports(b, s)));
  const lines = [];
  sel.filter((b) => !brands.includes(b) && subs.length).forEach((b) => lines.push(`${b.name}: 선택한 소분류를 지원하지 않아 검색에서 제외됩니다.`));
  brands.forEach((b) => { const miss = subs.filter((s) => !supports(b, s)); if (miss.length) lines.push(`${b.name}: ${miss.map((s) => (S.subs.get(s) || {}).label_ko || s).join(', ')} 미지원 — 이 조합은 건너뜁니다.`); });
  return { subs, brands, lines };
}
/* brand x country x sub-category listings a search would run (an estimate; the server enforces MAX_SEARCH_COMBOS) */
const CC_REGION = { us: 'na', ca: 'na', kr: 'kr', de: 'eu', uk: 'eu', fr: 'eu' };
function comboEstimate(p) {
  return p.brands.reduce((n, b) => n + p.subs.filter((s) => supports(b, s)).length * Math.max(1, b.countries.filter((c) => S.regions.has(CC_REGION[c])).length), 0);
}
const brandsLabel = (list) => (list.length > 6 ? `${list.slice(0, 5).map((b) => b.name).join(' · ')} 외 ${list.length - 5}개` : list.map((b) => b.name).join(' · '));
function thresholds() { return [parseFloat($('#thr-lo').value), parseFloat($('#thr-hi').value)]; }
function thresholdError() {
  if (S.bandMode !== 'custom') return '';
  const [lo, hi] = thresholds();
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return '두 경계 가격을 모두 숫자로 입력하세요.';
  if (lo < 0 || hi > 1e6) return '0 이상, 1,000,000 이하의 값을 입력하세요.';
  if (lo >= hi) return '앞의 경계가 뒤의 경계보다 작아야 합니다.';
  return '';
}
function updateSummary() {
  const err = thresholdError(), p = plan();
  $('#thr-err').hidden = !err; $('#thr-err').textContent = err;
  const band = S.bandMode === 'auto' ? '자동 3분위' : (err ? '경계 가격 확인 필요' : `${usd(thresholds()[0])} / ${usd(thresholds()[1])} 기준`);
  const perMajor = S.cats.filter((c) => S.selMajors.has(c.key)).map((c) => `${c.label_ko} ${c.children.filter((k) => S.selSubs.has(k.key)).length}`).filter((t) => !/ 0$/.test(t));
  const sum = $('#search-summary');
  sum.replaceChildren(...(!S.selBrands.size ? ['브랜드를 하나 이상 선택하세요.'] : !S.selMajors.size ? ['제품군을 선택하세요.'] : !p.subs.length ? ['소분류를 하나 이상 선택하세요.']
    : !p.brands.length ? ['선택한 소분류를 지원하는 브랜드가 없습니다.'] : [el('b', {}, brandsLabel(p.brands)), ` · ${perMajor.join(' · ')} · ${band}`]));
  const lines = p.lines.slice(), est = p.brands.length && p.subs.length ? comboEstimate(p) : 0;
  p.brands.filter((b) => b.delay).forEach((b) => lines.push(`${b.name}: 사이트 정책(robots.txt)으로 요청 간격이 ${b.delay}초라 조회가 오래 걸립니다 (검색은 소분류 1개당 약 10~50초, 제품 수집은 1개당 약 15초 더 걸립니다). 검색은 브랜드·소분류 순서대로 진행되므로 전체 시간도 그만큼 늘어납니다.`));
  if (est > S.maxCombos) lines.push(`선택한 조합이 약 ${est}개로 한 번에 검색할 수 있는 ${S.maxCombos}개를 넘을 수 있습니다. 브랜드나 소분류를 줄이세요.`);
  const note = $('#combo-note'); note.hidden = !lines.length;
  note.replaceChildren(icon(I.warn, 'glyph'), el('span', {}, lines.map((l, i) => [i ? el('br') : null, l])));
  $('#go-search').disabled = !p.brands.length || !p.subs.length || !!err || S.running;
  if (S.v2) syncShell();
  $('#band-hint').replaceChildren(...(S.bandMode === 'auto'
    ? ['제품군마다 검색 결과의 가격 분포를 3등분해 자동으로 나눕니다. 가격을 확인할 수 없는 제품은 ', el('b', {}, '가격 미확인'), ' 밴드에 따로 모입니다.']
    : [`입력한 경계 가격으로 나눕니다. ${err ? '' : `${usd(thresholds()[0])} 미만은 Budget, ${usd(thresholds()[1])} 이상은 Premium. `}같은 경계가 모든 제품군에 적용되므로 제품군별 가격 차이가 크면 자동 분류를 권장합니다.`]));
}
document.querySelectorAll('input[name="bandmode"]').forEach((r) => r.addEventListener('change', () => {
  S.bandMode = r.value; $('#thr').hidden = r.value !== 'custom'; updateSummary(); saveSel();
}));
['#thr-lo', '#thr-hi'].forEach((s) => $(s).addEventListener('input', () => { updateSummary(); saveSel(); }));
$('#limit').addEventListener('change', saveSel);

function saveSel() {
  lsSet(LS + 'sel', { regions: [...S.regions], brands: [...S.selBrands], majors: [...S.selMajors], subs: [...S.selSubs], band_mode: S.bandMode, thr: thresholds().map((x) => (Number.isFinite(x) ? x : 0)), limit: +$('#limit').value });
}
function applySel(r, keepUnset) {
  const ready = new Set(S.brands.filter((b) => b.enabled).map((b) => b.name));
  if (Array.isArray(r.regions) && r.regions.length) S.regions = new Set(r.regions.filter((k) => !S.regionList.length || S.regionList.some((x) => x.key === k && x.enabled)));
  S.selBrands = new Set((r.brands || []).filter((b) => ready.has(b)));
  S.selSubs = new Set((r.subs || []).filter((s) => S.subs.has(s)));
  S.selMajors = new Set([...(r.majors || []), ...[...S.selSubs].map((s) => S.subs.get(s).major)].filter((m) => S.cats.some((c) => c.key === m)));
  if (r.band_mode === 'auto' || r.band_mode === 'custom') S.bandMode = r.band_mode;
  if (Array.isArray(r.thr)) { $('#thr-lo').value = r.thr[0]; $('#thr-hi').value = r.thr[1]; }
  if (r.limit && [...$('#limit').options].some((o) => +o.value === +r.limit)) $('#limit').value = String(r.limit);
  document.querySelector(`input[name="bandmode"][value="${S.bandMode}"]`).checked = true; $('#thr').hidden = S.bandMode !== 'custom';
  if (!keepUnset) prune();
}

/* ---------- recent searches ---------- */
function recentLabel(r) { return `${r.brands.join(' · ')} · 소분류 ${r.subs.length}개 · ${r.band_mode === 'auto' ? '자동' : `${usd(r.thr[0])}/${usd(r.thr[1])}`}`; }
function renderRecent() {
  const list = lsGet(LS + 'recent', []).filter((r) => Array.isArray(r.subs) && Array.isArray(r.brands));
  $('#recent').hidden = !list.length;
  $('#recent-list').replaceChildren(...list.map((r) => el('li', {}, el('button', { type: 'button', title: '이 조건으로 다시 검색', onclick: () => applyRecent(r) }, recentLabel(r)))));
}
function saveRecent(r) {
  const key = (x) => JSON.stringify([x.brands, x.subs, x.band_mode, x.band_mode === 'custom' ? x.thr : 0, x.limit]);
  const list = [r, ...lsGet(LS + 'recent', []).filter((x) => Array.isArray(x.subs) && key(x) !== key(r))].slice(0, 6);
  lsSet(LS + 'recent', list); renderRecent();
}
function applyRecent(r) {
  applySel({ ...r, majors: [] }); renderAllSetup(); updateSummary(); saveSel();
  if (!$('#go-search').disabled) runSearch();
}
function renderAllSetup() { renderBrands(); renderMajors(); renderSubs(); if (S.v2) renderShell(); }

/* ---------- search ---------- */
function progressList(items) {
  const labels = { pending: '대기', running: '진행 중', done: '완료', failed: '실패', cancelled: '취소됨' };
  return el('ul', { class: 'prog-list' }, items.map((it) => el('li', { class: `prog-item s-${it.status}` },
    el('span', { class: 'dot' }), el('span', { class: 'lbl' }, it.label), el('span', { class: 'msg' }, it.message && it.status === 'failed' ? it.message : labels[it.status] || ''))));
}
function setBusy(on) { S.running = on; $('#cand-body').setAttribute('aria-busy', String(on)); updateSummary(); }

async function runSearch() {
  if (S.running || $('#go-search').disabled) return;
  const p = plan(), names = p.brands.map((b) => b.name);
  const req = { brands: names, subcategories: p.subs, limit: +$('#limit').value, band_mode: S.bandMode,
    thresholds: S.bandMode === 'custom' ? thresholds() : null, browser_mode: S.browser };
  if (S.hasRegions) req.regions = [...S.regions];
  if (S.legacy) req.category = [...S.selMajors][0];
  S.result = null; S.groups = []; S.picks.clear(); S.filter = ''; S.notes = p.lines; S.byUrl = new Map(); S.fsel.clear(); S.funk.clear();
  $('#cand-sub').textContent = '';
  $('#cand-body').replaceChildren(el('div', { class: 'state' }, el('h3', {}, '후보를 찾는 중입니다'),
    el('p', {}, '브랜드 사이트의 제품 목록을 읽고 있어요. 사이트 응답에 따라 수십 초가 걸릴 수 있습니다.'),
    progressList(names.map((b) => ({ label: b, status: 'pending' })))));
  showStage('candidates'); setBusy(true);
  let id;
  try { ({ job_id: id } = await post('/api/search', req)); }
  catch (e) { setBusy(false); return searchError(e.message); }
  saveRecent({ regions: [...S.regions], brands: names, subs: p.subs, band_mode: req.band_mode, thr: thresholds().map((x) => (Number.isFinite(x) ? x : 0)), limit: req.limit });
  const ctl = S.ctl = { stop: false }; S.jobId = id;
  const onTick = (j) => {
    const st = $('#cand-body .state'); if (!st || j.status !== 'running' && j.status !== 'queued') return;
    st.querySelector('.prog-list').replaceWith(progressList(j.progress.items.length ? j.progress.items : names.map((b) => ({ label: b, status: 'pending' }))));
  };
  const stop = el('button', { class: 'btn ghost', type: 'button', onclick: async (e) => { e.target.disabled = true; e.target.textContent = '취소 중…'; try { await post(`/api/jobs/${id}/cancel`); } catch { /* ignore */ } } }, '검색 취소');
  $('#cand-body .state').append(el('div', { class: 'acts' }, stop));
  try {
    const j = await poll(id, onTick, ctl);
    setBusy(false);
    if (j.status === 'cancelled') return searchError('검색을 취소했습니다.', true);
    if (j.status === 'error') return searchError(j.error || '검색 중 오류가 발생했습니다.');
    S.searched = names; S.searchKey = searchKey(); S.result = j.result; S.result.logs = j.log; S.groups = normGroups(j.result); renderCandidates(); if (S.v2) { updateStale(); renderFilters(); }
  } catch (e) { setBusy(false); searchError(e.message); }
}
function searchError(msg, soft) {
  $('#cand-body').replaceChildren(el('div', { class: 'state' + (soft ? '' : ' err-s') },
    icon(I.warn, 'glyph'), el('h3', {}, soft ? '검색이 취소되었습니다' : '후보를 가져오지 못했습니다'), el('p', {}, msg),
    el('div', { class: 'acts' }, el('button', { class: 'btn primary', type: 'button', onclick: runSearch }, '다시 검색'),
      el('button', { class: 'btn', type: 'button', onclick: () => { showStage('setup'); window.scrollTo({ top: 0 }); } }, '조건 바꾸기'))));
  $('#bar').hidden = true;
}
/* response: {groups:[{category,label_ko,bands,thresholds}]}; legacy {bands,thresholds} becomes one group */
function normGroups(r) {
  const mk = (cat, label, bands, thr) => {
    const g = { category: cat, label_ko: label || catLabel(cat), thresholds: Array.isArray(thr) ? thr : null, bands: {} };
    BANDS.forEach((b) => { g.bands[b.key] = ((bands || {})[b.key] || []).map((c) => ({ ...c, category: c.category || cat, band: b.key })); });
    return g;
  };
  if (r.groups && r.groups.length) return r.groups.map((g) => mk(g.category, g.label_ko, g.bands, g.thresholds));
  if (r.bands) return [mk([...S.selMajors][0] || 'refrigerator', '', r.bands, r.thresholds)];
  return [];
}

/* ---------- candidates: one section per major category ---------- */
const gCands = (g) => BANDS.flatMap((b) => g.bands[b.key]);
const allCands = () => S.groups.flatMap(gCands);
function bandRange(key, thr) {
  if (!thr) return '';
  const [lo, hi] = thr;
  return { budget: `< ${usd(lo)}`, mid: `${usd(lo)} – ${usd(hi)}`, premium: `≥ ${usd(hi)}`, unknown: '' }[key];
}
function renderCandidates() {
  const r = S.result, total = allCands().length, multi = S.groups.length > 1;
  $('#cand-sub').textContent = `${total}개 후보${multi ? ` · 제품군 ${S.groups.length}개` : ''} · 비교할 모델을 최대 ${S.maxSel}개까지 고르세요`;
  const body = $('#cand-body'); body.replaceChildren();
  if (r.failed_brands && r.failed_brands.length) {
    body.append(el('div', { class: 'banner bad', role: 'alert' }, icon(I.warn, 'glyph'), el('div', {}, `${r.failed_brands.join(', ')} 검색에 실패했습니다. 나머지 브랜드 결과만 표시합니다.`,
      r.logs && r.logs.length ? el('ul', {}, r.logs.slice(-6).map((l) => el('li', {}, l))) : null)));
  }
  if (S.notes.length) body.append(el('div', { class: 'banner' }, icon(I.warn, 'glyph'), el('div', {}, '지원하지 않는 조합은 검색에서 제외했습니다.', el('ul', {}, S.notes.map((l) => el('li', {}, l))))));
  if (!total) {
    body.append(el('div', { class: 'state' }, icon(I.box, 'glyph'), el('h3', {}, '검색 결과가 없습니다'),
      el('p', {}, '선택한 브랜드·소분류에서 후보를 찾지 못했습니다. 후보 수를 늘리거나 다른 소분류를 선택해 보세요.'),
      el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: () => showStage('setup') }, '조건 바꾸기'))));
    $('#bar').hidden = true; return;
  }
  body.append(el('div', { class: 'cand-tools' },
    el('label', { class: 'search-in' }, icon(I.search), el('span', { class: 'vh' }, '후보 필터'),
      el('input', { type: 'search', id: 'filter', placeholder: '모델 번호, 이름, 브랜드로 필터', autocomplete: 'off', oninput: (e) => { S.filter = e.target.value.trim().toLowerCase(); applyFilter(); } })),
    el('div', { class: 'legend' }, BANDS.filter((b) => allCands().some((c) => c.band === b.key)).map((b) => el('span', {}, el('i', { style: `background:${b.color}` }), b.title)))));
  S.groups.forEach((g) => body.append(groupSection(g, multi)));
  refreshSelection(); applyFilter(); showStage('candidates');
}
function groupSection(g, multi) {
  const n = gCands(g).length, cat = g.category;
  const sec = el('section', { class: 'cat-group', 'data-cat': cat, 'aria-labelledby': 'ch-' + cat },
    el('div', { class: 'cat-h' }, icon(I[cat] || I.box, 'tile-i'), el('h3', { id: 'ch-' + cat }, g.label_ko), el('span', { class: 'count', 'data-gcount': cat }, `${n}개 후보`)),
    el('div', { class: 'strip', 'data-strip': cat }));
  if (!n) sec.append(el('p', { class: 'empty-band' }, '이 제품군에서 선택한 브랜드·소분류의 후보를 찾지 못했습니다.'));
  else BANDS.forEach((b) => sec.append(bandSection(g, b)));
  return sec;
}
function bandSection(g, b) {
  const list = g.bands[b.key], id = `bh-${g.category}-${b.key}`;
  const sec = el('section', { class: 'band', 'data-band': b.key, 'aria-labelledby': id });
  const range = bandRange(b.key, g.thresholds);
  sec.append(el('div', { class: 'band-h' },
    el('h4', { id, style: `--c:${b.color}` }, el('i'), b.title, b.ko ? el('span', { class: 'count' }, b.ko) : null),
    range ? el('span', { class: 'range' }, range) : null,
    el('span', { class: 'count', 'data-count': b.key }, `${list.length}개`),
    el('div', { class: 'band-acts' },
      el('button', { class: 'link-btn', type: 'button', disabled: !list.length, onclick: () => autoPick(g, b.key), title: `이 밴드에서 브랜드별로 가장 저렴한 ${g.label_ko} 모델을 선택` }, '브랜드별 최저가 선택'),
      el('button', { class: 'link-btn', type: 'button', disabled: !list.length, onclick: () => clearBand(g, b.key) }, '해제'))));
  const rows = el('div', { class: 'rows' });
  if (!list.length) rows.append(el('p', { class: 'empty-band' }, b.key === 'unknown' ? '가격을 확인할 수 없는 제품이 없습니다.' : '이 가격대에 해당하는 후보가 없습니다.'));
  list.forEach((c) => rows.append(candRow(c)));
  rows.append(el('p', { class: 'empty-band', hidden: true, 'data-nomatch': b.key }, '필터와 일치하는 후보가 없습니다.'));
  sec.append(rows); return sec;
}
function candRow(c) {
  const sl = subLabel(c);
  S.byUrl.set(c.url, c);
  const pick = el('button', { class: 'pick', type: 'button', 'aria-pressed': 'false', 'aria-label': `${c.brand} ${c.model_number} 선택`, onclick: (e) => { e.stopPropagation(); toggle(c); } });
  const row = el('div', { class: 'row', 'data-url': c.url, 'data-q': `${c.brand} ${c.model_number} ${c.name} ${sl}`.toLowerCase(), onclick: () => toggle(c) },
    pick, el('div', { class: 'r-model' }, c.model_number),
    el('div', { class: 'r-name' }, el('span', { class: 'brandtag' }, c.brand), S.regions.size > 1 && c.region ? el('span', { class: 'subtag' }, regionLabel(c.region)) : null, sl ? el('span', { class: 'subtag' }, sl) : null, el('span', { class: 't', title: c.name }, c.name)),
    c.price_usd != null ? el('div', { class: 'r-price' }, usd(c.price_usd)) : c.price_local != null ? el('div', { class: 'r-price' }, `${Math.round(c.price_local).toLocaleString('en-US')} ${c.currency || ''}`.trim()) : el('div', { class: 'r-price none' }, '가격 미확인'),
    httpsUrl(c.url) && el('a', { class: 'ext', href: httpsUrl(c.url), target: '_blank', rel: 'noopener noreferrer', title: '제품 페이지 열기', 'aria-label': `${c.model_number} 제품 페이지 (새 탭)`, onclick: (e) => e.stopPropagation() }, icon(I.ext)));
  return row;
}
/* one model per (brand, major category): picking another model replaces the previous pick */
function toggle(c) {
  if (S.picks.has(c.url)) S.picks.delete(c.url);
  else if (S.picks.size >= S.maxSel) return toast(`한 번에 최대 ${S.maxSel}개까지 선택할 수 있습니다.`);
  else S.picks.set(c.url, c);
  refreshSelection();
}
/* cheapest model per brand inside a band (rows are price-sorted), skipping filtered-out rows, up to the server max */
function autoPick(g, key) {
  const seen = new Set(); let capped = false;
  g.bands[key].filter(passes).forEach((c) => {
    if (seen.has(c.brand)) return; seen.add(c.brand);
    if (S.picks.has(c.url)) return;
    if (S.picks.size >= S.maxSel) { capped = true; return; }
    S.picks.set(c.url, c);
  });
  if (capped) toast(`최대 ${S.maxSel}개까지만 선택됩니다.`);
  refreshSelection();
}
function clearBand(g, key) { g.bands[key].forEach((c) => S.picks.delete(c.url)); refreshSelection(); }
function applyFilter() {
  document.querySelectorAll('#cand-body .row').forEach((r) => { r.hidden = !passes(S.byUrl.get(r.dataset.url)); });
  document.querySelectorAll('.band').forEach((sec) => {
    const rows = [...sec.querySelectorAll('.row')], vis = rows.filter((r) => !r.hidden).length;
    sec.querySelector('[data-count]').textContent = vis !== rows.length ? `${vis} / ${rows.length}개` : `${rows.length}개`;
    const nm = sec.querySelector('[data-nomatch]'); nm.hidden = !(rows.length && !vis);
  });
  renderStrips(); if (S.v2) renderFilters();
}
function refreshSelection() {
  const urls = new Set(S.picks.keys());
  document.querySelectorAll('#cand-body .row').forEach((r) => {
    const on = urls.has(r.dataset.url); r.classList.toggle('sel', on);
    r.querySelector('.pick').setAttribute('aria-pressed', String(on));
  });
  renderBar(); renderStrips();
}
/* a price strip per major category: washer prices never share an axis with refrigerator prices */
function renderStrips() {
  const picked = new Set(S.picks.keys());
  const hidden = new Set([...document.querySelectorAll('#cand-body .row[hidden]')].map((r) => r.dataset.url));
  S.groups.forEach((g) => {
    const box = [...document.querySelectorAll('[data-strip]')].find((x) => x.dataset.strip === g.category); if (!box) return;
    const all = gCands(g), cs = all.filter((c) => c.price_usd != null);
    if (!cs.length) { box.replaceChildren(); return; }
    const lo = Math.min(...cs.map((c) => c.price_usd)), hi = Math.max(...cs.map((c) => c.price_usd)), span = Math.max(hi - lo, 1);
    const pos = (p) => 2 + ((p - lo) / span) * 96;
    const axis = el('div', { class: 'strip-axis', role: 'img', 'aria-label': `${g.label_ko} 가격 분포 ${usd(lo)}에서 ${usd(hi)}, 후보 ${cs.length}개` });
    (g.thresholds || []).forEach((t) => { if (t > lo && t < hi) axis.append(el('div', { class: 'cut', style: `left:${pos(t)}%` }, el('span', {}, usd(t)))); });
    cs.forEach((c) => axis.append(el('i', { class: 'tick' + (picked.has(c.url) ? ' sel' : '') + (hidden.has(c.url) ? ' dim' : ''), style: `left:${pos(c.price_usd)}%;--c:${BANDS.find((b) => b.key === c.band).color}`, title: `${c.brand} ${c.model_number} ${usd(c.price_usd)}` })));
    const unk = all.length - cs.length;
    box.replaceChildren(axis, el('div', { class: 'strip-lbl' }, el('span', {}, usd(lo)), unk ? el('span', {}, `가격 미확인 ${unk}개는 표시되지 않음`) : null, el('span', {}, usd(hi))));
  });
}
function estimate(n) {
  const sec = n * (SECONDS_PER_PRODUCT + ($('#opt-modes').checked ? SECONDS_PER_MODES : 0));
  return sec < 60 ? `약 ${Math.max(sec, 5)}초` : `약 ${Math.round(sec / 60)}분`;
}
function renderBar() {
  const picks = [...S.picks.values()].sort((a, b) => S.cats.findIndex((c) => c.key === a.category) - S.cats.findIndex((c) => c.key === b.category) || a.brand.localeCompare(b.brand) || a.model_number.localeCompare(b.model_number));
  const n = picks.length, sel = $('#bar-sel'), sep = () => el('i', { class: 'sep', 'aria-hidden': 'true' }, '·');
  sel.replaceChildren(el('span', { class: 'cnt', title: '선택한 모델 수 / 한 번에 수집 가능한 최대 수' }, `${n} / ${S.maxSel}`),
    ...(n ? picks.map((c) => el('span', { class: 'chip' }, el('b', {}, c.brand), sep(), el('span', { class: 'cs' }, subLabel(c) || catLabel(c.category)),
      sep(), el('span', { class: 'cm' }, c.model_number), sep(), el('span', { class: 'cs' }, regionLabel(c.region)),
      el('button', { type: 'button', 'aria-label': `${c.brand} ${subLabel(c)} ${c.model_number} 선택 해제`, onclick: () => { S.picks.delete(c.url); refreshSelection(); } }, icon(I.x))))
      : [el('span', { class: 'none' }, '비교할 모델을 고르세요 (브랜드당 여러 모델 가능)')]));
  $('#bar-est').replaceChildren('예상 소요', el('br'), el('b', {}, n ? estimate(n) : '—'));
  $('#go-collect').disabled = !n || S.running;
}
$('#opt-modes').addEventListener('change', renderBar);
$('#opt-browser').addEventListener('change', (e) => {
  S.browser = e.target.value;
  $('#browser-hint').textContent = S.browser === 'visible' ? '주의: 이 PC에 브라우저 창이 실제로 열립니다. 수집이 끝날 때까지 창을 닫지 마세요.'
    : S.browser === 'headless' ? '창을 열지 않고 수집합니다. 일부 사이트가 차단하면 실패할 수 있습니다.' : '숨김 모드로 먼저 시도합니다. 차단되면 창 표시 방식으로 넘어갈 수 있으며, 이때 이 PC에 브라우저 창이 열립니다.';
});

/* ---------- collect ---------- */
async function runCollect() {
  if (S.running || !S.picks.size) return;
  const picks = [...S.picks.values()];
  const withModes = $('#opt-modes').checked;
  $('#coll-sub').textContent = `${picks.length}개 제품 · ${estimate(picks.length)} 예상`;
  const cancel = $('#cancel'); cancel.disabled = false; cancel.textContent = '취소'; cancel.hidden = false;
  const body = $('#coll-body');
  body.replaceChildren(el('div', { class: 'meter' }, el('div', { class: 'meter-top' }, el('span', {}, '진행'), el('b', { id: 'm-num' }, `0 / ${picks.length}`)),
    el('div', { class: 'meter-track', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(picks.length), 'aria-valuenow': '0', 'aria-label': '수집 진행률' }, el('div', { class: 'meter-fill', id: 'm-fill' }))),
    el('div', { id: 'm-items' }, progressList(picks.map((c) => ({ label: `${c.brand} ${c.model_number}`, status: 'pending' })))),
    el('div', { class: 'logbox', id: 'm-log', role: 'log', 'aria-label': '수집 로그', tabindex: '0' }, '…'));
  showStage('collect'); setBusy(true); $('#bar').hidden = true;
  let id;
  const urls = picks.map((c) => ({ brand: c.brand, url: c.url, model_number: c.model_number, name: c.name, price_usd: c.price_usd, category: c.category, subcategory: c.subcategory,
    ...(c.region ? { region: c.region } : {}), ...(c.country ? { country: c.country } : {}) }));
  try { ({ job_id: id } = await post('/api/collect', { urls, with_modes: withModes, browser_mode: S.browser })); }
  catch (e) { setBusy(false); return collectError(e.message); }
  S.jobId = id; const ctl = S.ctl = { stop: false };
  cancel.onclick = async () => { cancel.disabled = true; cancel.textContent = '취소 중…'; try { await post(`/api/jobs/${id}/cancel`); } catch { /* ignore */ } };
  let lastLog = -1;
  const onTick = (j) => {
    const { done, total, items } = j.progress;
    $('#m-num').textContent = `${done} / ${total || picks.length}`;
    $('#m-fill').style.width = `${total ? Math.max(done / total, j.status === 'running' ? 0.03 : 0) * 100 : 0}%`;
    $('.meter-track').setAttribute('aria-valuenow', String(done));
    $('#m-items').replaceChildren(progressList(items));
    if (j.log.length !== lastLog) {
      lastLog = j.log.length; const lg = $('#m-log'), stick = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 24;
      lg.replaceChildren(...j.log.map((l) => el('div', {}, l))); if (stick) lg.scrollTop = lg.scrollHeight;
    }
  };
  try {
    const j = await poll(id, onTick, ctl);
    setBusy(false); cancel.hidden = true; collapseCollect();
    if (j.status === 'error') return collectError(j.error || '수집 중 오류가 발생했습니다.');
    renderResults(j, picks);
  } catch (e) { setBusy(false); collectError(e.message); }
}
function collapseCollect() {
  const items = $('#m-items'), log = $('#m-log'); if (!items || !log) return;
  $('#coll-body').append(el('details', { class: 'rlog' }, el('summary', {}, '진행 내역 보기'), items, log));
}
function collectError(msg) {
  $('#cancel').hidden = true;
  $('#coll-body').prepend(el('div', { class: 'banner bad', role: 'alert' }, icon(I.warn, 'glyph'), el('div', {}, msg)),
    el('div', { class: 'acts', style: 'margin-bottom:16px;display:flex;gap:10px' }, el('button', { class: 'btn primary', type: 'button', onclick: runCollect }, '다시 수집'),
      el('button', { class: 'btn', type: 'button', onclick: () => showStage('candidates') }, '후보로 돌아가기')));
}

/* ---------- results ---------- */
const SPEC_GROUPS = [
  { g: '가격 · 용량', rows: [
    { k: 'price_usd', l: '가격', fmt: usd, best: 'min' },
    { k: 'capacity_total_cuft', l: '총 용량', unit: 'cu ft', best: 'max' }, { k: 'capacity_fridge_cuft', l: '냉장실', unit: 'cu ft' }, { k: 'capacity_freezer_cuft', l: '냉동실', unit: 'cu ft' }] },
  { g: '규격', rows: [
    { k: 'dims', l: '크기 W × H × D', unit: 'in', get: (p) => (p.width_in && p.height_in && p.depth_in ? `${num1(p.width_in)} × ${num1(p.height_in)} × ${num1(p.depth_in)}` : null), text: true },
    { k: 'weight_lb', l: '무게', unit: 'lb' }, { k: 'door_style', l: '도어 스타일', text: true }, { k: 'finish_color', l: '마감', text: true }] },
  { g: '에너지 · 전기', rows: [
    { k: 'energy_kwh_year', l: '연간 소비전력', unit: 'kWh/yr', best: 'min', fmt: (n) => Math.round(n).toLocaleString('en-US') },
    { k: 'energy_star', l: 'ENERGY STAR', bool: true }, { k: 'voltage_v', l: '전압', text: true }, { k: 'frequency_hz', l: '주파수', unit: 'Hz' }] },
  { g: '기능', rows: [
    { k: 'wifi_supported', l: 'Wi-Fi', bool: true }, { k: 'ice_maker', l: '제빙기', bool: true }, { k: 'water_dispenser', l: '정수 디스펜서', bool: true },
    { k: 'fridge_temp_range_f', l: '냉장 설정 온도', text: true }, { k: 'freezer_temp_range_f', l: '냉동 설정 온도', text: true }] },
];
const SRC = { web: ['웹', '제품 페이지에서 확인'], modes: ['모드', '매뉴얼의 동작 모드에서 확인'], both: ['웹+모드', '제품 페이지와 매뉴얼 모두에서 확인'], 'spec field': ['사양', '사양 항목에서 확인'] };
const tag = (cls, t, title) => el('span', { class: cls, title }, t);

/* group collected products by major category; each group carries its own POD rows */
function resultGroups(res, picks) {
  const P = res.products, first = (S.groups[0] || {}).category || MODES_CATEGORY;
  const lookup = new Map(picks.map((c) => [norm(c.brand) + '|' + norm(c.model_number), c.category]));
  const catOf = (p) => p.category || lookup.get(norm(p.brand) + '|' + norm(p.model_number)) || first;
  const idxOf = (list) => list.map((p) => P.findIndex((q) => norm(q.brand) === norm(p.brand) && norm(q.model_number) === norm(p.model_number)));
  let groups;
  if (res.groups && res.groups.length) groups = res.groups.map((g) => ({ category: g.category, label_ko: g.label_ko, products: g.products || P.filter((p) => catOf(p) === g.category), compare: g.compare || null, canon_stats: g.canon_stats || null, pod: g.pod || (g.pod_rows ? { rows: g.pod_rows } : null) }));
  else {
    const cats = []; P.forEach((p) => { const c = catOf(p); if (!cats.includes(c)) cats.push(c); });
    groups = cats.map((c) => ({ category: c, products: P.filter((p) => catOf(p) === c), pod: null }));
  }
  return groups.filter((g) => g.products.length).map((g) => {
    let rows = g.pod ? g.pod.rows : null;
    if (!rows) {                                         // legacy: one matrix over all products -> slice this group's columns
      const ix = idxOf(g.products), whole = ix.length === P.length;
      rows = ((res.pod && res.pod.rows) || []).map((r) => (whole ? r : { ...r, present: ix.map((i) => r.present[i]), source: ix.map((i) => (r.source || [])[i]), wording: ix.map((i) => (r.wording || [])[i]) }))
        .filter((r) => r.present.some(Boolean));
    }
    return { category: g.category, label_ko: g.label_ko || catLabel(g.category), products: g.products, podRows: rows, compare: g.compare, canonStats: g.canon_stats || null };
  });
}

/* ARIA tabs with arrow-key navigation; panels are built lazily and cached */
function tabset(prefix, label, defs, cls) {
  const tabs = el('div', { class: 'tabs' + (cls ? ' ' + cls : ''), role: 'tablist', 'aria-label': label });
  const panel = el('div', { id: prefix + '-panel', class: 'tabpanel', role: 'tabpanel', tabindex: '0' });
  const cache = {};
  const select = (k, focus) => {
    tabs.querySelectorAll('button').forEach((b) => { const on = b.dataset.k === k; b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; if (on && focus) b.focus(); });
    panel.setAttribute('aria-labelledby', `${prefix}-tab-${k}`);
    panel.replaceChildren(cache[k] || (cache[k] = defs.find((d) => d[0] === k)[3]()));
  };
  defs.forEach(([k, l, n]) => tabs.append(el('button', { role: 'tab', id: `${prefix}-tab-${k}`, 'data-k': k, type: 'button', 'aria-controls': prefix + '-panel', onclick: () => select(k) }, l, n != null ? el('span', { class: 'c' }, String(n)) : null)));
  tabs.addEventListener('keydown', (e) => {
    const ks = defs.map((d) => d[0]), cur = ks.indexOf(tabs.querySelector('[aria-selected="true"]').dataset.k);
    const nxt = { ArrowRight: (cur + 1) % ks.length, ArrowLeft: (cur + ks.length - 1) % ks.length, Home: 0, End: ks.length - 1 }[e.key];
    if (nxt != null) { e.preventDefault(); select(ks[nxt], true); }
  });
  select(defs[0][0]);
  return el('div', { class: 'tabset' }, tabs, panel);
}

function renderResults(job, picks) {
  const res = job.result || { products: [], documents: [], modes: [], pod: { rows: [] }, run_log: [] };
  res.pod = res.pod || { rows: [] }; res.modes = res.modes || []; res.documents = res.documents || []; res.run_log = res.run_log || [];
  const P = res.products, cancelled = job.status === 'cancelled';
  const issues = res.run_log.filter((r) => r[1] === 'failed' || r[1] === 'partial');
  const groups = resultGroups(res, picks);
  $('#res-sub').textContent = `${P.length}개 제품${groups.length > 1 ? ` · 제품군 ${groups.length}개` : ''}${issues.length ? ` · 문제 ${issues.length}건` : ''}${cancelled ? ' · 취소됨' : ''}`;
  const x = $('#xlsx'); x.hidden = !P.length || !job.has_excel; x.href = `/api/jobs/${job.id}/excel`;
  const body = $('#res-body'); body.replaceChildren();
  if (cancelled) body.append(el('div', { class: 'banner' }, icon(I.warn, 'glyph'), el('div', {}, `수집을 취소했습니다. 완료된 ${P.length}개 제품만 표시합니다.`)));
  if (issues.length) body.append(el('div', { class: 'banner bad', role: 'alert' }, icon(I.warn, 'glyph'), el('div', {}, `${issues.length}건의 수집 문제가 있었습니다. 나머지 제품은 정상 수집되었습니다.`, el('ul', {}, issues.map((r) => el('li', {}, `${r[0]} — ${r[2]}`))))));
  if (!P.length) {
    body.append(el('div', { class: 'state err-s' }, icon(I.box, 'glyph'), el('h3', {}, '수집된 제품이 없습니다'), el('p', {}, '선택한 제품 페이지에서 사양을 읽지 못했습니다. 다른 모델을 선택하거나 브라우저 모드를 바꿔 다시 시도해 보세요.'),
      el('div', { class: 'acts' }, el('button', { class: 'btn primary', type: 'button', onclick: () => showStage('candidates') }, '후보로 돌아가기'))));
    showStage('results'); return;
  }
  const view = (g) => () => categoryView(g, res);
  if (groups.length > 1) body.append(tabset('cat', '제품군 선택', groups.map((g) => [g.category, g.label_ko, g.products.length, view(g)]), 'tabs-major'));
  else body.append(categoryView(groups[0], res));
  if (window.GaugeTemplate) window.GaugeTemplate.mount(body, job, groups);   // "내 분류양식" panel + 비교표 기준 toggle (web/js/template.js)
  if (res.run_log.length) body.append(el('details', { class: 'rlog' }, el('summary', {}, `수집 로그 (${res.run_log.length})`),
    el('div', { class: 'logbox' }, res.run_log.map((r) => el('div', { class: r[1] === 'failed' ? 'f' : '' }, `[${r[1]}] ${r[2]}`)))));
  showStage('results');
}
/* spec / POD / modes / documents for one major category. Operating modes exist for refrigerators only. */
function categoryView(g, res) {
  const P = g.products, c = g.category, hasModes = c === MODES_CATEGORY;
  const mineOf = (list) => list.filter((m) => P.some((p) => norm(p.brand) === norm(m.brand) && norm(p.model_number) === norm(m.model_number)));
  const modes = mineOf(res.modes), docs = mineOf(res.documents);
  const defs = [g.compare && g.compare.length ? ['spec', '비교', P.length, () => comparePanel(P, g.compare, g.canonStats)] : ['spec', '사양 비교', P.length, () => specPanel(P)], ['pod', 'POD 매트릭스', g.podRows.length, () => podPanel(P, g.podRows)]];
  if (hasModes) defs.push(['modes', '동작 모드', modes.length, () => modesPanel(P, modes)]);
  defs.push(['docs', '문서', docs.length, () => docsPanel(P, docs)]);
  return el('div', { class: 'catview' }, tabset('t-' + c, `${g.label_ko} 결과 보기`, defs),
    hasModes ? null : el('p', { class: 'hint note' }, `동작 모드 추출은 현재 냉장고만 지원해 ${g.label_ko}에서는 표시하지 않습니다.`));
}

function prodHead(p, withLink = true) {
  const sl = subLabel(p);
  return [el('span', { class: 'ph-brand' }, p.brand, sl ? ` · ${sl}` : ''), el('span', { class: 'ph-model' }, p.model_number), el('span', { class: 'ph-name', title: p.product_name }, p.product_name),
    withLink && httpsUrl(p.product_url) ? el('a', { class: 'ph-link', href: httpsUrl(p.product_url), target: '_blank', rel: 'noopener noreferrer' }, '제품 페이지', icon(I.ext, 'x')) : null];
}
const yesNo = (v) => v === true ? el('span', { class: 'yes' }, icon(I.check), el('span', { class: 'vh' }, '있음')) : v === false ? el('span', { class: 'no' }, '없음') : null;

/* ---------- full specification table ----------
   Every row of a brand's "Specs & Details" arrives in product.extra_specs as 'Section > Label' (or just 'Label'),
   multi-value rows joined with ' | '. The comparison shows the curated common rows first, then all of them. */
const SECTION_SEP = ' > ';
const SECTION_HEUR = [   // sections for keys that carry none, first match wins
  [/width|height|depth|weight|dimension|cutout|clearance/i, 'Dimensions'],
  [/capacity|volume|cu\.? ?ft|liter/i, 'Capacity'],
  [/rack|shelf|shelves|drawer|light|lamp/i, 'Racks & lighting'],
  [/wi-?fi|smart|remote|connect/i, 'Smart & connectivity'],
  [/clean/i, 'Cleaning'],
  [/mode|cycle|cook|bake|broil|convection|air fry|steam|sabbath|probe|proof/i, 'Cooking & cycles'],
  [/volt|amp|watt|hz|btu|power|energy|kwh/i, 'Power & energy'],
  [/warrant/i, 'Warranty'],
];
const VISIBLE_ITEMS = 4;     // multi-value cells show this many bullets before 'show more'
let specSeq = 0;
function splitSpecKey(key) {
  const i = key.indexOf(SECTION_SEP);
  if (i > 0) return [key.slice(0, i).trim(), key.slice(i + SECTION_SEP.length).trim() || key];
  const h = SECTION_HEUR.find(([re]) => re.test(key));
  return [h ? h[1] : 'General', key];
}
const splitItems = (v) => String(v).split(' | ').map((x) => x.trim()).filter(Boolean);
const itemKey = (x) => x.toLowerCase().replace(/\s+/g, ' ');
const specNorm = (v) => (v == null ? null : splitItems(v).map(itemKey).sort().join('|'));
/* drop a flat label whose value equals a sectioned ('Section > Label') value: adapters keep both for some brands */
function dedupeSpecs(x) {
  const n = (v) => String(v).toLowerCase().replace(/\s+/g, ' ').trim(), sec = [];
  Object.entries(x || {}).forEach(([k, v]) => { if (k.includes(SECTION_SEP)) sec.push([k.split(SECTION_SEP).pop().trim().toLowerCase(), n(v)]); });
  const vals = new Set(sec.map((e) => e[1])), pairs = new Set(sec.map((e) => e.join('##')));
  const dup = (k, v) => (['yes', 'no', 'true', 'false'].includes(n(v)) ? pairs.has(k.trim().toLowerCase() + '##' + n(v)) : vals.has(n(v)));
  return Object.fromEntries(Object.entries(x || {}).filter(([k, v]) => k.includes(SECTION_SEP) || !dup(k, v)));
}
/* [{title, rows:[{label, vals:[string|null], diff}]}] — union of extra_specs keys, sections and keys in first-seen order */
function fullSpecs(P) {
  const secs = new Map(), X = P.map((p) => dedupeSpecs(p.extra_specs));
  P.forEach((p, i) => Object.entries(X[i]).forEach(([k, v]) => {
    if (v == null || String(v).trim() === '') return;
    const [s, l] = splitSpecKey(k);
    if (!secs.has(s)) secs.set(s, new Map());
    if (!secs.get(s).has(k)) secs.get(s).set(k, l);
  }));
  return [...secs].map(([title, keys]) => ({ title, rows: [...keys].map(([k, label]) => {
    const vals = X.map((x) => { const v = x[k]; return v == null || String(v).trim() === '' ? null : String(v); });
    return { label, vals, diff: P.length > 1 && new Set(vals.map(specNorm)).size > 1, q: `${title} ${label} ${vals.join(' ')}`.toLowerCase() };
  }) }));
}
/* one value cell: plain text, or a compact bullet list for ' | ' values (items missing elsewhere get a subtle mark) */
function specValue(v, vals) {
  if (v == null) return el('td', { class: 'v na', title: '데이터 없음' }, '—');
  const items = splitItems(v);
  if (items.length < 2) return el('td', { class: 'v txt wrapv' }, v);
  const others = vals.filter((x) => x != null && x !== v).map((x) => new Set(splitItems(x).map(itemKey)));
  const list = el('ul', { class: 'vlist' }, items.map((it, i) => el('li', { class: (others.some((s) => !s.has(itemKey(it))) ? 'uniq ' : '') + (i >= VISIBLE_ITEMS ? 'extra' : ''), hidden: i >= VISIBLE_ITEMS }, it)));
  const td = el('td', { class: 'v txt wrapv' }, list);
  if (items.length > VISIBLE_ITEMS) {
    const hidden = items.length - VISIBLE_ITEMS;
    const btn = el('button', { class: 'more', type: 'button', 'aria-expanded': 'false', onclick: () => {
      const open = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', String(open));
      list.querySelectorAll('li.extra').forEach((li) => { li.hidden = !open; });
      btn.textContent = open ? '접기' : `+${hidden}개 더보기`;
    } }, `+${hidden}개 더보기`);
    td.append(btn);
  }
  return td;
}
/* the picture at the top of a product column: aspect-boxed, contain-fit, placeholder when absent or broken */
const imgSrc = (p) => (typeof p.image_src === 'string' && p.image_src.startsWith('/api/img/') ? p.image_src : null);
function prodImage(p) {
  const label = `${p.brand} ${p.model_number}`;
  const empty = () => el('div', { class: 'ph-fig ph-empty', role: 'img', 'aria-label': `${label} 이미지 없음` }, icon(I[p.category] || I.box), el('span', {}, '이미지 없음'));
  const src = imgSrc(p);
  if (!src) return empty();
  const fig = el('div', { class: 'ph-fig' });
  fig.append(el('img', { src, alt: `${label} 제품 이미지`, loading: 'lazy', decoding: 'async', onerror: () => fig.replaceWith(empty()) }));
  return fig;
}

function specPanel(P) {
  const uid = ++specSeq, st = { onlyDiff: false, q: '' };
  const all = fullSpecs(P);
  const nVals = (r) => P.map((p) => (r.get ? r.get(p) : p[r.k]));
  const wrap = el('div', { class: 'specpanel' });
  const status = el('p', { class: 'spec-count', role: 'status', 'aria-live': 'polite' });
  const index = el('nav', { class: 'spec-index', 'aria-label': '사양 섹션 목차' });
  const table = el('table', { class: 'cmp' });
  const tbody = el('tbody');
  const colspan = String(P.length + 1);

  const commonRows = () => {            // curated rows: numeric formatting, best-value marks, yes/no icons
    const out = [];
    SPEC_GROUPS.forEach((grp) => {
      const rows = [];
      grp.rows.forEach((r) => {
        const vals = nVals(r);
        if (vals.every((v) => v == null)) return;
        const diff = P.length > 1 && new Set(vals.map((v) => (v == null ? '∅' : String(v)))).size > 1;
        const q = `${grp.g} ${r.l} ${vals.join(' ')}`.toLowerCase();
        if ((st.onlyDiff && !diff) || (st.q && !q.includes(st.q))) return;
        let bestVal = null;
        const nums = vals.filter((v) => typeof v === 'number');
        if (r.best && P.length > 1 && diff && nums.length > 1) bestVal = r.best === 'min' ? Math.min(...nums) : Math.max(...nums);
        rows.push(el('tr', { class: diff ? 'diff' : '' }, el('th', { class: 'lab', scope: 'row' }, r.l, r.unit ? el('span', { class: 'unit' }, r.unit) : null),
          vals.map((v) => {
            if (v == null) return el('td', { class: 'v na', title: '데이터 없음' }, '—');
            const isBest = bestVal != null && v === bestVal;
            const content = r.bool ? yesNo(v) : (r.fmt ? r.fmt(v) : typeof v === 'number' ? num1(v) : String(v));
            return el('td', { class: 'v' + (r.text ? ' txt' : '') + (isBest ? ' best' : ''), title: isBest ? (r.best === 'min' ? '비교 대상 중 가장 낮음' : '비교 대상 중 가장 큼') : null }, content, isBest ? el('span', { class: 'vh' }, ' (최선)') : null);
          })));
      });
      if (rows.length) out.push({ sub: grp.g, rows });
    });
    return out;
  };

  const draw = () => {
    tbody.replaceChildren(); index.replaceChildren(el('h4', {}, '섹션'));
    let shown = 0, total = 0, n = 0;
    const addGroup = (title, count, diffs, cls) => {
      const id = `sec-${uid}-${n++}`;
      tbody.append(el('tr', { class: 'group' + (cls ? ' ' + cls : '') }, el('th', { colspan: colspan, id, tabindex: '-1', scope: 'colgroup' }, title)));
      index.append(el('button', { type: 'button', class: 'idx', onclick: () => { const t = document.getElementById(id); t.scrollIntoView({ block: 'start' }); t.focus({ preventScroll: true }); } },
        el('span', { class: 'it' }, title), el('span', { class: 'ic' }, diffs ? `${diffs}/${count}` : String(count))));
    };
    const common = commonRows();
    total += SPEC_GROUPS.reduce((a, g) => a + g.rows.filter((r) => !nVals(r).every((v) => v == null)).length, 0);
    const cc = common.reduce((a, g) => a + g.rows.length, 0);
    if (cc) {
      addGroup('공통 항목', cc, common.reduce((a, g) => a + g.rows.filter((r) => r.classList.contains('diff')).length, 0), 'major');
      common.forEach((g) => { tbody.append(el('tr', { class: 'subgroup' }, el('th', { colspan: colspan }, g.sub)), ...g.rows); });
      shown += cc;
    }
    const full = all.map((s) => ({ s, rows: s.rows.filter((r) => !(st.onlyDiff && !r.diff) && !(st.q && !r.q.includes(st.q))) }));
    total += all.reduce((a, s) => a + s.rows.length, 0);
    const fullShown = full.reduce((a, f) => a + f.rows.length, 0);
    if (all.length && fullShown) {
      tbody.append(el('tr', { class: 'group major all-h' }, el('th', { colspan: colspan }, `전체 사양 · All specifications (${fullShown})`)));
    }
    full.forEach(({ s, rows }) => {
      if (!rows.length) return;
      addGroup(s.title, rows.length, rows.filter((r) => r.diff).length);
      rows.forEach((r) => tbody.append(el('tr', { class: r.diff ? 'diff' : '' }, el('th', { class: 'lab', scope: 'row' }, r.label), r.vals.map((v) => specValue(v, r.vals)))));
      shown += rows.length;
    });
    if (!shown) tbody.append(el('tr', {}, el('td', { colspan: colspan, class: 'v txt na' }, st.q ? '필터와 일치하는 항목이 없습니다.' : '차이가 있는 항목이 없습니다.')));
    status.textContent = `전체 ${total}개 항목 중 ${shown}개 표시${st.onlyDiff ? ' · 다른 값만' : ''}${st.q ? ` · “${st.q}”` : ''}`;
    index.hidden = !index.querySelector('.idx');
  };

  table.append(el('caption', { class: 'vh' }, '제품 사양 비교'),
    el('thead', {},
      el('tr', { class: 'imgrow' }, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '제품 이미지')), P.map((p) => el('th', { scope: 'col', class: 'pimg' }, prodImage(p)))),
      el('tr', {}, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '항목')), P.map((p) => el('th', { scope: 'col' }, prodHead(p))))),
    tbody);
  const filter = el('label', { class: 'spec-filter' }, icon(I.search), el('span', { class: 'fl' }, '필터'),
    el('input', { type: 'search', id: `sf-${uid}`, placeholder: '항목명 또는 값으로 찾기 (예: air fry, rack)', autocomplete: 'off', oninput: (e) => { st.q = e.target.value.trim().toLowerCase(); draw(); } }));
  const diffTog = P.length > 1 ? el('label', { class: 'toggle' }, el('input', { type: 'checkbox', onchange: (e) => { st.onlyDiff = e.target.checked; draw(); } }), el('span', { class: 'sw', 'aria-hidden': 'true' }), el('span', {}, '다른 값만 보기')) : null;
  wrap.append(
    el('div', { class: 'panel-tools' },
      el('div', { class: 'key' }, el('span', {}, el('i', { class: 'k-diff' }), '값이 다른 항목'), el('span', {}, el('i', { class: 'k-best' }), '가장 유리한 값 (가격·소비전력 낮음, 용량 큼)'),
        el('span', {}, el('i', { class: 'k-uniq' }), '일부 제품에만 있는 항목(목록)')),
      el('div', { class: 'key' }, filter, diffTog)),
    status,
    el('div', { class: 'spec-layout' }, index, el('div', { class: 'tbl-wrap' }, table)));
  draw();
  return wrap;
}

/* ---------- transposed comparison (server `compare` rows: products = columns, CANONICAL attributes = rows) ----------
   Row: {id, section, group, key_en, key_ko, unit, values[], kind:'value'|'flag', differs, same, notes[], core, uncertain,
   method, sources:[[{label, value, method, score, via?}] per product]}. The Excel 'Compare' sheet is built from the very same
   rows. Default view = core rows; '전체 항목 보기' adds the long tail (collapsed per section). Basics shown in the column
   header (image/brand/model/name) are not repeated. */
const CMP_SECTIONS = ['기본정보', '치수·무게', '용량', '전기·에너지', '성능', '기능', '디자인', '연결성', '조리(오븐·쿡탑)', '세탁·건조', '냉각·신선', '보증·기타', '액세서리·옵션'];
const CMP_HEADER_IDS = ['image', 'brand', 'model', 'name'];
const CMP_BEST = { price: 'min', 'energy-annual': 'min', 'capacity-total': 'max', 'oven-capacity': 'max', 'washer-capacity': 'max' };
const METHOD_KO = { override: '사용자 지정', seed: '표준 사전', exact: '이전 매핑', registry: '이전 매핑', embed: '유사도', llm: 'AI 판단', fallback: '규칙 유사도', new: '신규 항목', rule: '규칙(액세서리)', derived: '목록에서 계산' };
const STAGE_KO = { used: '사용됨', unavailable: '사용 불가 · 결정적 폴백', offline: '사용 불가(오프라인) · 결정적 폴백', not_needed: '호출 불필요' };
function cmpCell(r, v, note, best, nVals, srcs, showOrig) {
  const orig = showOrig && srcs && srcs.length ? el('div', { class: 'wording orig' }, srcs.slice(0, 2).map((s) => s.label).join(' · ')) : null;
  if (r.kind === 'flag') {
    return v ? el('td', { class: 'v flagc' }, el('div', { class: 'pod-cell' }, yesNo(true), note ? el('span', { class: 'wording' }, note) : null, orig))
      : el('td', { class: 'v na', title: '확인되지 않음' }, '—');
  }
  if (v == null || v === '') return el('td', { class: 'v na', title: '데이터 없음' }, '—');
  if (r.id === 'url') return el('td', { class: 'v txt' }, httpsUrl(v) ? el('a', { class: 'ph-link', href: httpsUrl(v), target: '_blank', rel: 'noopener noreferrer' }, '제품 페이지', icon(I.ext, 'x')) : '—');
  const isBest = best && typeof v === 'number' && nVals.length > 1 && v === (best === 'min' ? Math.min(...nVals) : Math.max(...nVals));
  const text = typeof v === 'number' ? (r.unit === 'USD' ? usd(v) : num1(v)) : String(v);
  return el('td', { class: 'v' + (typeof v === 'string' ? ' txt wrapv' : '') + (isBest ? ' best' : ''), title: isBest ? (best === 'min' ? '비교 대상 중 가장 낮음' : '비교 대상 중 가장 큼') : null }, text, isBest ? el('span', { class: 'vh' }, ' (최선)') : null,
    note ? el('div', { class: 'wording' }, note) : null, orig);  // e.g. 'derived from item list: 1 X Rack + 2 Y Racks'
}
function cmpTip(r, P) {  // hover / long-press text: each product's original wording
  const lines = P.map((p, i) => ((r.sources || [])[i] || []).map((s) => `${p.brand} ${p.model_number}: ${s.label} = ${s.value}`)).flat();
  return lines.length ? `원문 항목명\n${lines.join('\n')}` : null;
}
function comparePanel(P, rows, canonStats) {
  const uid = ++specSeq, st = { onlyDiff: false, q: '', all: false, en: false, orig: false, open: new Set(), shut: new Set(), tip: new Set() }, colspan = String(P.length + 1);
  const data = rows.filter((r) => !CMP_HEADER_IDS.includes(r.id)).map((r) => ({
    ...r, core: r.core !== false,
    q: `${r.section} ${r.group} ${r.key_ko} ${r.key_en} ${r.values.join(' ')} ${(r.notes || []).join(' ')} ${(r.sources || []).flat().map((s) => s.label).join(' ')}`.toLowerCase() }));
  const known = CMP_SECTIONS.filter((s) => data.some((r) => r.section === s));
  const sections = known.concat([...new Set(data.map((r) => r.section))].filter((s) => !known.includes(s)));
  const nTail = data.filter((r) => !r.core).length;
  const wrap = el('div', { class: 'specpanel' }), status = el('p', { class: 'spec-count', role: 'status', 'aria-live': 'polite' });
  const index = el('nav', { class: 'spec-index', 'aria-label': '비교 항목 구분 목차' }), tbody = el('tbody');
  const cs = canonStats || null;   // server `canon_stats`: a silent degrade of the embedding stage must be visible
  const stageNote = cs ? el('p', { class: 'embed-stage' + (cs.embed_stage === 'used' || cs.embed_stage === 'not_needed' ? '' : ' warn'), role: 'status', title: cs.line_ko || null },
    `임베딩 단계: ${STAGE_KO[cs.embed_stage] || cs.embed_stage}`, cs.labels ? ` · 항목 ${cs.labels}개 매핑` : '') : null;
  const rowEl = (r) => {
    const nums = r.values.filter((v) => typeof v === 'number'), best = r.differs ? CMP_BEST[r.id.split(':')[0]] : null;
    const main = st.en ? r.key_en : r.key_ko, alt = st.en ? r.key_ko : r.key_en;
    const tip = cmpTip(r, P);
    const lab = el('th', { class: 'lab', scope: 'row', title: tip || r.key_en, tabindex: tip ? '0' : null, 'data-id': r.id }, main,
      r.unit ? el('span', { class: 'unit' }, r.unit) : null,
      r.uncertain ? el('span', { class: 'badge-unsure', title: `매핑이 불확실합니다 (${METHOD_KO[r.method] || r.method}). Excel의 Mapping 시트 또는 data/canon_overrides.json에서 확인·수정하세요.` }, '검토') : null,
      alt && alt !== main ? el('div', { class: 'wording' }, alt) : null);
    const toggleTip = () => { if (st.tip.has(r.id)) st.tip.delete(r.id); else st.tip.add(r.id); draw(); };
    if (tip) { lab.addEventListener('click', toggleTip); lab.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleTip(); } }); }
    const tr = el('tr', { class: (r.differs ? 'diff' : '') + (r.same ? ' same' : '') + (r.core ? '' : ' tail') }, lab,
      r.values.map((v, i) => cmpCell(r, v, (r.notes || [])[i], best, nums, (r.sources || [])[i], st.orig)));
    if (!st.tip.has(r.id)) return [tr];
    const detail = el('tr', { class: 'srcrow' }, el('td', { colspan }, el('div', { class: 'srcbox' }, el('b', {}, '원문 항목명 → 값 (매핑 방법)'),
      P.map((p, i) => ((r.sources || [])[i] || []).length ? el('div', { class: 'srcline' }, el('span', { class: 'who' }, `${p.brand} ${p.model_number}`),
        ((r.sources || [])[i] || []).map((s) => el('span', { class: 'src' }, `${s.via ? s.via + ' › ' : ''}${s.label} = ${s.value}`, el('i', {}, ` ${METHOD_KO[s.method] || s.method}${s.method === 'seed' || s.method === 'override' ? '' : ' ' + Number(s.score).toFixed(2)}`)))) : null))));
    return [tr, detail];
  };
  const draw = () => {
    tbody.replaceChildren(); index.replaceChildren(el('h4', {}, '구분'));
    let shown = 0;
    const pass = (r) => !(st.onlyDiff && !r.differs) && !(st.q && !r.q.includes(st.q));
    sections.forEach((s, si) => {
      const inSec = data.filter((r) => r.section === s && pass(r)), core = inSec.filter((r) => r.core), tail = inSec.filter((r) => !r.core);
      const vis = st.all ? core.length + tail.length : core.length;
      if (!vis) return;
      const id = `cs-${uid}-${si}`, nd = (st.all ? inSec : core).filter((r) => r.differs).length;
      tbody.append(el('tr', { class: 'group major' }, el('th', { colspan, id, tabindex: '-1', scope: 'colgroup' }, `${s} (${vis})`)));
      index.append(el('button', { type: 'button', class: 'idx', onclick: () => { const t = document.getElementById(id); t.scrollIntoView({ block: 'start' }); t.focus({ preventScroll: true }); } },
        el('span', { class: 'it' }, s), el('span', { class: 'ic' }, nd ? `${nd}/${vis}` : String(vis))));
      core.forEach((r) => { rowEl(r).forEach((n) => tbody.append(n)); shown++; });
      if (st.all && tail.length) {
        const open = st.open.has(s) || !!st.q;
        tbody.append(el('tr', { class: 'tailrow' }, el('th', { colspan }, el('button', { type: 'button', class: 'tailbtn', 'aria-expanded': String(open), onclick: () => { if (st.open.has(s)) st.open.delete(s); else st.open.add(s); draw(); } },
          `${open ? '▾' : '▸'} 롱테일 항목 ${tail.length}개`, tail.some((r) => r.differs) ? el('span', { class: 'ic' }, ` · 다른 값 ${tail.filter((r) => r.differs).length}`) : null))));
        if (open) {
          let g = null;   // list values keep their parent: one collapsible sub-header per group, children right below it
          tail.forEach((r) => {
            const gk = r.group ? `${s}|${r.group}` : null, shut = !!gk && st.shut.has(gk) && !st.q;
            if (r.group && r.group !== g) {
              const kids = tail.filter((x) => x.group === r.group).length;
              tbody.append(el('tr', { class: 'subgroup grp' }, el('th', { colspan }, el('button', { type: 'button', class: 'tailbtn', 'aria-expanded': String(!shut), onclick: () => { if (st.shut.has(gk)) st.shut.delete(gk); else st.shut.add(gk); draw(); } },
                `${shut ? '▸' : '▾'} ${st.en ? r.group : (r.group_ko || r.group)} (${kids})`))));
            }
            g = r.group || null;
            if (shut) return;
            rowEl(r).forEach((n) => tbody.append(n)); shown++;
          });
        }
      }
    });
    if (!shown) tbody.append(el('tr', {}, el('td', { colspan, class: 'v txt na' }, st.q ? '필터와 일치하는 항목이 없습니다.' : st.onlyDiff ? '차이가 있는 항목이 없습니다.' : '표시할 항목이 없습니다.')));
    const hiddenTail = st.all ? 0 : data.filter((r) => !r.core && pass(r)).length;
    status.textContent = `전체 ${data.length}개 항목 중 ${shown}개 표시${st.all ? ' · 전체 항목' : ` · 핵심 항목 (롱테일 ${hiddenTail || nTail}개 숨김)`}${st.onlyDiff ? ' · 다른 값만' : ''}${st.q ? ` · “${st.q}”` : ''}`;
    index.hidden = !index.querySelector('.idx');
  };
  const table = el('table', { class: 'cmp' }, el('caption', { class: 'vh' }, '제품 비교 (제품 = 열, 항목 = 행)'),
    el('thead', {},
      el('tr', { class: 'imgrow' }, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '제품 이미지')), P.map((p) => el('th', { scope: 'col', class: 'pimg' }, prodImage(p)))),
      el('tr', {}, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '항목')), P.map((p) => el('th', { scope: 'col' }, prodHead(p))))), tbody);
  const filter = el('label', { class: 'spec-filter' }, icon(I.search), el('span', { class: 'fl' }, '필터'),
    el('input', { type: 'search', id: `sf-${uid}`, placeholder: '항목명 또는 값으로 찾기 (예: air fry, rack)', autocomplete: 'off', oninput: (e) => { st.q = e.target.value.trim().toLowerCase(); draw(); } }));
  const tog = (label, fn, hint) => el('label', { class: 'toggle', title: hint || null }, el('input', { type: 'checkbox', onchange: (e) => { fn(e.target.checked); draw(); } }), el('span', { class: 'sw', 'aria-hidden': 'true' }), el('span', {}, label));
  const diffTog = P.length > 1 ? tog('다른 값만 보기', (v) => { st.onlyDiff = v; }) : null;
  const allTog = nTail ? tog('전체 항목 보기', (v) => { st.all = v; }, `기본은 핵심 항목만 표시합니다. 켜면 롱테일 ${nTail}개 항목이 구분별로 접혀서 추가됩니다.`) : null;
  wrap.append(el('div', { class: 'panel-tools' },
    el('div', { class: 'key' }, el('span', {}, el('i', { class: 'k-diff' }), '값이 다른 항목'), el('span', {}, el('i', { class: 'k-best' }), '가장 유리한 값 (가격·소비전력 낮음, 용량 큼)'),
      el('span', {}, el('span', { class: 'badge-unsure' }, '검토'), ' 매핑이 불확실한 항목')),
    el('div', { class: 'key' }, filter, allTog, diffTog, tog('English 항목명', (v) => { st.en = v; }, '항목명을 영어(기본: 한국어)로 표시'),
      tog('원문 항목명 보기', (v) => { st.orig = v; }, '각 제품 원문 항목명을 값 아래에 표시 (항목명을 누르면 원문 상세)'))), status, ...(stageNote ? [stageNote] : []),  // native append() would print a null as the text 'null'
    el('div', { class: 'spec-layout' }, index, el('div', { class: 'tbl-wrap' }, table)));
  draw();
  return wrap;
}

function podPanel(P, rows) {
  const wrap = el('div'); let onlyDiff = false, words = false;
  if (!rows.length) return el('div', { class: 'state' }, el('h3', {}, 'POD 항목이 없습니다'), el('p', {}, '제품 페이지에서 확인된 주요 특징이 없어 매트릭스를 만들 수 없습니다.'));
  const draw = () => {
    const tb = el('tbody'); let cat = null, shown = 0;
    rows.forEach((r) => {
      const diff = !r.present.every(Boolean) && P.length > 1;
      if (onlyDiff && !diff) return;
      if (r.category !== cat) { cat = r.category; tb.append(el('tr', { class: 'group' }, el('th', { colspan: String(P.length + 1) }, cat))); }
      shown++;
      tb.append(el('tr', { class: diff ? 'diff' : '' }, el('th', { class: 'lab', scope: 'row' }, r.ko, el('div', { class: 'wording' }, r.en)),
        r.present.map((on, i) => on
          ? el('td', { class: 'v' }, el('div', { class: 'pod-cell' }, el('span', { class: 'yes' }, icon(I.check), el('span', { class: 'vh' }, '있음')),
              r.source && r.source[i] && SRC[r.source[i]] ? tag('src' + (r.source[i] === 'both' ? ' both' : ''), SRC[r.source[i]][0], SRC[r.source[i]][1]) : null,
              words && r.wording && r.wording[i] ? el('span', { class: 'wording' }, r.wording[i]) : null))
          : el('td', { class: 'v na', title: '확인되지 않음' }, '—'))));
    });
    if (!shown) tb.append(el('tr', {}, el('td', { colspan: String(P.length + 1), class: 'v txt na' }, '차이가 있는 항목이 없습니다.')));
    const tog = (label, on, fn) => el('label', { class: 'toggle' }, el('input', { type: 'checkbox', checked: on, onchange: (e) => { fn(e.target.checked); draw(); } }), el('span', { class: 'sw', 'aria-hidden': 'true' }), el('span', {}, label));
    wrap.replaceChildren(
      el('div', { class: 'panel-tools' }, el('div', { class: 'key' }, el('span', {}, el('i', { class: 'k-diff' }), '일부 제품에만 있는 항목'), el('span', {}, '출처: 웹 = 제품 페이지, 모드 = 매뉴얼')),
        el('div', { class: 'key' }, tog('원문 표시', words, (v) => { words = v; }), P.length > 1 ? tog('차이만 보기', onlyDiff, (v) => { onlyDiff = v; }) : null)),
      el('div', { class: 'tbl-wrap' }, el('table', { class: 'cmp' }, el('caption', { class: 'vh' }, 'POD 기능 비교 매트릭스'),
        el('thead', {}, el('tr', {}, el('th', { class: 'lab', scope: 'col' }, el('span', { class: 'vh' }, '기능')), P.map((p) => el('th', { scope: 'col' }, prodHead(p, false))))), tb)));
  };
  draw(); return wrap;
}

function modesPanel(P, modes) {
  if (!modes.length) return el('div', { class: 'state' }, icon(I.box, 'glyph'), el('h3', {}, '추출된 동작 모드가 없습니다'),
    el('p', {}, '수집 옵션에서 ‘로컬 LLM으로 동작 모드 추출’을 켜고 다시 수집하면 매뉴얼에서 모드를 읽어옵니다. 켠 경우에도 매뉴얼에서 근거를 찾지 못하면 비어 있을 수 있습니다.'));
  return el('div', { class: 'modes-grid' }, P.map((p) => {
    const mine = modes.filter((m) => norm(m.brand) === norm(p.brand) && norm(m.model_number) === norm(p.model_number));
    return el('details', { class: 'mcol', open: true }, el('summary', {}, prodHead(p, false), el('span', { class: 'wording' }, `${mine.length}개 모드`)),
      mine.length ? mine.map((m) => el('article', { class: 'mode' }, el('h4', {}, m.mode_name, el('span', { class: 'tag' }, m.category)), el('p', {}, m.description),
        el('dl', {}, m.setting_range ? [el('dt', {}, '설정'), el('dd', {}, m.setting_range)] : null, m.how_to_activate ? [el('dt', {}, '방법'), el('dd', {}, m.how_to_activate)] : null,
          el('dt', {}, '출처'), el('dd', {}, `${m.source_doc}${m.source_page ? ` p.${m.source_page}` : ''}`))))
        : el('p', { class: 'empty-band' }, '이 제품에서는 모드를 찾지 못했습니다.'));
  }));
}

function docsPanel(P, docs) {
  if (!docs.length) return el('div', { class: 'state' }, el('h3', {}, '내려받은 문서가 없습니다'), el('p', {}, '제품 페이지에서 매뉴얼이나 에너지 가이드 PDF를 찾지 못했습니다.'));
  return el('div', { class: 'docs-list' }, P.map((p) => {
    const mine = docs.filter((d) => norm(d.brand) === norm(p.brand) && norm(d.model_number) === norm(p.model_number));
    return el('div', {}, el('div', { class: 'mcol', style: 'padding:14px 0;border-bottom:1px solid var(--rule)' }, prodHead(p, false)),
      mine.length ? mine.map((d) => {
        const name = (d.local_path || d.source_url || '').split(/[\/]/).pop() || d.doc_type;
        return el('div', { class: 'doc' }, d.href ? el('a', { href: d.href, target: '_blank', rel: 'noopener' }, name) : el('span', {}, name, el('small', {}, ' · 로컬 파일 없음')),
          tag('tag', d.doc_type), el('small', {}, [d.pages ? `${d.pages}쪽` : '', d.size_bytes ? `${Math.max(1, Math.round(d.size_bytes / 1024))} KB` : ''].filter(Boolean).join(' · ')));
      }) : el('p', { class: 'empty-band' }, '문서 없음'));
  }));
}

/* ======================================================================================
   v2 shell: region chips + brand popover (top bar) | mega-menu rail with fly-out | filter panel | result area.
   Search (network) = regions x brands x sub-categories, started only by the 검색 button.
   Filters are client-side over the candidates already loaded (no request). All text via textContent.
   ====================================================================================== */
const DEFAULT_REGIONS = [
  { key: 'kr', label_ko: '한국', enabled: false, note: '준비 중' }, { key: 'na', label_ko: '북미', enabled: true },
  { key: 'eu', label_ko: '유럽', enabled: false, note: '준비 중' }, { key: 'sa', label_ko: '남미', enabled: false, note: '준비 중' },
  { key: 'me', label_ko: '중동', enabled: false, note: '준비 중' }, { key: 'as', label_ko: '아시아', enabled: false, note: '준비 중' }, { key: 'oc', label_ko: '오세아니아', enabled: false, note: '준비 중' },
];
const PRIMARY_REGIONS = ['kr', 'na', 'eu', 'sa'];
const regionLabel = (k) => ((S.regionList.find((r) => r.key === k) || DEFAULT_REGIONS.find((r) => r.key === k) || {}).label_ko) || k || '';
const searchKey = () => JSON.stringify([[...S.regions].sort(), [...S.selBrands].sort(), [...S.selSubs].sort()]);
const narrow = () => matchMedia('(max-width: 899px)').matches;
const accordion = () => narrow() || matchMedia('(hover: none)').matches;
let moreRegions = false;

/* ---- regions ---- */
function renderRegions() {
  const list = S.regionList, shown = list.filter((r) => PRIMARY_REGIONS.includes(r.key) || moreRegions || S.regions.has(r.key));
  const rest = list.filter((r) => !shown.includes(r));
  $('#region-list').replaceChildren(...shown.map((r) => {
    const on = r.enabled && S.regions.has(r.key);
    return el('button', { class: 'rchip' + (r.enabled ? '' : ' off'), type: 'button', 'aria-pressed': String(on), 'aria-disabled': r.enabled ? null : 'true', 'data-fk': 'rg-' + r.key,
      title: r.enabled ? null : (r.note || '준비 중'), onclick: () => regionClick(r) }, r.label_ko, r.enabled ? null : el('small', {}, '준비 중'));
  }), rest.length ? el('button', { class: 'rchip more', type: 'button', 'data-fk': 'rg-more', onclick: () => { moreRegions = true; keepFocus(renderRegions); } }, `+ ${rest.map((r) => r.label_ko).join('·')}`) : null);
}
function regionClick(r) {
  if (!r.enabled) return toast(`${r.label_ko}: ${r.note || '준비 중'}입니다. 지원하는 사이트(어댑터)가 아직 없습니다.`);
  if (S.regions.has(r.key)) { if (S.regions.size === 1) return toast('출향지는 하나 이상 선택해야 합니다.'); S.regions.delete(r.key); }
  else if (S.regions.size >= 4) return toast('출향지는 최대 4곳까지 선택할 수 있습니다.');
  else S.regions.add(r.key);
  const dropped = prune();
  keepFocus(() => { renderRegions(); renderBrands(); renderMajors(); renderSubs(); renderBrandPop(); renderRail(); });
  if (dropped) toast('선택한 출향지에서 판매하지 않는 브랜드·소분류 선택을 해제했습니다.');
  updateSummary(); saveSel();
}

/* ---- brand popover: grouped by 계열 (group), searchable, only brands that sell in a selected region are enabled ---- */
const brandOk = (b) => b.enabled && regionOk(b);
const brandSel = () => S.brands.filter((b) => S.selBrands.has(b.name) && brandOk(b)).length;   // selected AND usable in the chosen markets
const brandWhy = (b) => (!b.enabled ? '준비 중' : !regionOk(b) ? '선택한 출향지 미판매' : '');
const foldQ = (t) => String(t).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').normalize('NFC').toLowerCase().replace(/[^a-z0-9\u3131-\u318e\uac00-\ud7a3]/g, '');
const brandGroups = () => [...new Set(S.brands.map((b) => b.group || '기타'))];
function brandCount() {
  const n = $('#pop-n'); if (n) n.textContent = `${brandSel()}/${S.brands.length} 선택 · 선택 가능 ${S.brands.filter(brandOk).length}`;
}
/* search = hide rows/groups in place (no re-render, so the input keeps focus) */
function filterBrandRows() {
  const q = foldQ(S.brandQ); let shown = 0;
  document.querySelectorAll('#brand-pop .pop-g').forEach((g) => {
    const rows = [...g.querySelectorAll('.pop-li')]; let any = 0;
    rows.forEach((li) => { const hit = !q || li.dataset.q.includes(q); li.hidden = !hit; if (hit) any++; });
    g.hidden = !any; shown += any;
  });
  const e = document.querySelector('#brand-pop .pop-empty'); if (e) e.hidden = shown > 0;
}
function renderBrandPop() {
  const pop = $('#brand-pop'), ready = S.brands.filter(brandOk);
  const allOn = ready.length > 0 && ready.every((b) => S.selBrands.has(b.name));
  const toggle = (list, on) => { list.forEach((b) => (on ? S.selBrands.delete(b.name) : S.selBrands.add(b.name))); onSelectionChange(true); };
  const row = (b, i) => {
    const ok = brandOk(b), why = brandWhy(b);
    return el('li', { class: 'pop-li', 'data-q': foldQ(b.name + ' ' + (b.group || '')) }, el('label', { class: 'pop-row' + (ok ? '' : ' off') },
      el('input', { type: 'checkbox', checked: ok && S.selBrands.has(b.name), disabled: !ok, 'data-fk': 'bp-' + i,
        onchange: (e) => { e.target.checked ? S.selBrands.add(b.name) : S.selBrands.delete(b.name); onSelectionChange(true); } }),
      el('span', { class: 'bn' }, b.name),
      b.countries.length ? el('span', { class: 'cc', title: '어댑터가 있는 국가' }, b.countries.map((c) => c.toUpperCase()).join(' ')) : null,
      why ? el('small', {}, why) : (b.delay ? el('small', { class: 'slow', title: b.note }, `요청 간격 ${b.delay}초 · 느림`) : null)));
  };
  const groups = brandGroups().map((g, gi) => {
    const list = S.brands.filter((b) => (b.group || '기타') === g), rdy = list.filter(brandOk), on = rdy.length > 0 && rdy.every((b) => S.selBrands.has(b.name));
    return el('section', { class: 'pop-g', 'aria-label': g },
      el('div', { class: 'pop-gh' }, el('h4', {}, g), el('span', { class: 'gn' }, `${list.filter((b) => S.selBrands.has(b.name) && brandOk(b)).length}/${list.length}`),
        el('button', { class: 'link-btn', type: 'button', 'data-fk': 'bg-' + gi, disabled: !rdy.length, onclick: () => toggle(rdy, on) }, on ? '해제' : '전체')),
      el('ul', {}, list.map((b) => row(b, S.brands.indexOf(b)))));
  });
  pop.replaceChildren(
    el('div', { class: 'pop-h' }, el('b', {}, '브랜드'), el('span', { class: 'pop-n', id: 'pop-n' }),
      el('button', { class: 'link-btn', type: 'button', 'data-fk': 'bp-all', onclick: () => toggle(ready, allOn) }, allOn ? '전체 해제' : '전체 선택')),
    el('input', { class: 'pop-q', type: 'search', 'data-fk': 'bp-q', placeholder: '브랜드 검색 (이름·계열)', 'aria-label': '브랜드 검색', autocomplete: 'off', value: S.brandQ,
      oninput: (e) => { S.brandQ = e.target.value; filterBrandRows(); } }),
    el('div', { class: 'pop-body' }, groups, el('p', { class: 'pop-empty', hidden: true }, '검색 결과가 없습니다.')));
  brandCount(); filterBrandRows();
}
function setPop(open) {
  const btn = $('#brand-btn'), pop = $('#brand-pop');
  pop.hidden = !open; btn.setAttribute('aria-expanded', String(open));
  if (open) { const f = pop.querySelector('.pop-q') || pop.querySelector('input:not(:disabled)') || pop.querySelector('button'); if (f) f.focus(); }
}
function initBrandPop() {
  $('#brand-btn').addEventListener('click', () => setPop($('#brand-pop').hidden));
  document.addEventListener('pointerdown', (e) => { if (!$('#brand-pop').hidden && !e.target.closest('.brandpick')) setPop(false); });
  $('#brand-pop').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); setPop(false); $('#brand-btn').focus(); } });
}

/* ---- mega-menu rail ---- */
const subsOf = (c) => c.children.filter((k) => S.selSubs.has(k.key));
function renderRail() {
  const ul = $('#rail-list');
  ul.replaceChildren(...S.cats.map((c) => {
    const st = majorState(c), open = st.ok && S.openMajor === c.key, n = subsOf(c).length, sup = c.children.filter((k) => supporters(k.key).length);
    const allOn = sup.length > 0 && sup.every((k) => S.selSubs.has(k.key));
    const fly = el('div', { class: 'fly', id: 'fly-' + c.key, role: 'group', 'aria-label': `${c.label_ko} 소분류`, hidden: !open },
      el('div', { class: 'fly-h' }, el('b', {}, c.label_ko), el('button', { class: 'link-btn', type: 'button', 'data-fk': 'fa-' + c.key, disabled: !sup.length,
        onclick: () => { sup.forEach((k) => (allOn ? S.selSubs.delete(k.key) : S.selSubs.add(k.key))); subsChanged(c.key); } }, allOn ? '전체 해제' : '지원 항목 전체 선택')),
      el('div', { class: 'fly-l' }, c.children.map((k) => {
        const sp = supporters(k.key), ok = sp.length > 0, on = ok && S.selSubs.has(k.key);
        return el('button', { class: 'fsub' + (ok ? '' : ' off'), type: 'button', 'aria-pressed': String(on), 'aria-disabled': ok ? null : 'true', 'data-fk': 'fs-' + k.key,
          title: ok ? null : '선택한 브랜드·출향지에서 지원하지 않습니다.',
          onclick: () => { if (!ok) return toast(`${k.label_ko}: 선택한 브랜드·출향지에서 지원하지 않습니다.`); on ? S.selSubs.delete(k.key) : S.selSubs.add(k.key); subsChanged(c.key); } },
          el('span', { class: 'box', 'aria-hidden': 'true' }, icon(I.check)), el('span', { class: 'ft' }, el('span', { class: 'fl' }, k.label_ko), el('span', { class: 'fc' }, ok ? `${sp.length}개 브랜드` : '미지원')));
      })));
    return el('li', { class: 'rail-i' + (open ? ' open' : '') },
      el('button', { class: 'major', type: 'button', 'aria-expanded': String(open), 'aria-controls': 'fly-' + c.key, 'aria-disabled': st.ok ? null : 'true', 'data-fk': 'mj-' + c.key, 'data-k': c.key,
        title: st.ok ? null : st.why, onclick: (e) => majorClick(c, st, e) },
      icon(I[c.key] || I.box, 'tile-i'), el('span', { class: 'mn' }, c.label_ko),
      el('span', { class: 'mc' }, st.ok ? (n ? el('b', {}, `${n}`) : `${st.kids}`) : '준비 중'),
      icon('M9 6l6 6-6 6', 'chev')), fly);
  }));
  const tabs = [...ul.querySelectorAll('.major')], cur = tabs.find((b) => b.dataset.k === S.openMajor) || tabs.find((b) => b.getAttribute('aria-disabled') !== 'true') || tabs[0];
  tabs.forEach((b) => { b.tabIndex = b === cur ? 0 : -1; });
}
function subsChanged(majorKey) {
  S.selMajors = new Set([...S.selSubs].map((s) => S.subs.get(s).major)); S.fmajor = majorKey;
  keepFocus(() => { renderMajors(); renderSubs(); renderRail(); }); updateSummary(); saveSel();
}
function openFly(key) {
  S.openMajor = key;
  document.querySelectorAll('#rail-list .rail-i').forEach((li) => {
    const b = li.querySelector('.major'), on = b.dataset.k === key; li.classList.toggle('open', on);
    b.setAttribute('aria-expanded', String(on)); li.querySelector('.fly').hidden = !on;
  });
}
function majorClick(c, st, e) {
  if (!st.ok) return toast(st.why);
  const same = S.openMajor === c.key;
  if (same && (accordion() || e.detail > 0)) { if (accordion()) return openFly(null); }
  openFly(c.key);
  if (e.detail === 0) { const f = $('#fly-' + c.key + ' .fsub:not(.off)') || $('#fly-' + c.key + ' button'); if (f) f.focus(); }   // keyboard: Enter/Space moves into the fly-out
}
function initRail() {
  const wrap = $('#rail-wrap'), ul = $('#rail-list'); let tin, tout;
  ul.addEventListener('pointerover', (e) => {
    if (e.pointerType !== 'mouse' || accordion()) return;
    const b = e.target.closest('.rail-i') && e.target.closest('.rail-i').querySelector('.major');
    if (!b || b.getAttribute('aria-disabled') === 'true' || b.dataset.k === S.openMajor) { clearTimeout(tout); return; }
    clearTimeout(tout); clearTimeout(tin); tin = setTimeout(() => openFly(b.dataset.k), 120);
  });
  wrap.addEventListener('pointerleave', (e) => { if (e.pointerType !== 'mouse' || accordion()) return; clearTimeout(tin); tout = setTimeout(() => { if (!wrap.contains(document.activeElement)) openFly(null); }, 250); });
  wrap.addEventListener('pointerenter', () => clearTimeout(tout));
  ul.addEventListener('focusin', (e) => {
    const b = e.target.closest('.major'); if (b) { ul.querySelectorAll('.major').forEach((x) => { x.tabIndex = x === b ? 0 : -1; }); if (!accordion() && b.getAttribute('aria-disabled') !== 'true') openFly(b.dataset.k); }
  });
  ul.addEventListener('keydown', (e) => {
    const t = e.target, majors = [...ul.querySelectorAll('.major')];
    if (t.classList.contains('major')) {
      const i = majors.indexOf(t), mv = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: majors.length - 1 }[e.key];
      if (mv != null) { e.preventDefault(); const n = majors[(mv + majors.length) % majors.length]; n.focus(); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); if (t.getAttribute('aria-disabled') === 'true') return; openFly(t.dataset.k); const f = $('#fly-' + t.dataset.k + ' .fsub:not(.off)') || $('#fly-' + t.dataset.k + ' button'); if (f) f.focus(); }
      else if ((e.key === 'Escape' && !accordion()) || e.key === 'ArrowLeft') { if (S.openMajor) { e.preventDefault(); e.stopPropagation(); openFly(null); } }
    } else if (t.closest('.fly')) {
      const items = [...t.closest('.fly').querySelectorAll('button:not(:disabled)')], i = items.indexOf(t), mv = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
      if (mv != null) { e.preventDefault(); items[(mv + items.length) % items.length].focus(); }
      else if (e.key === 'ArrowLeft' || (e.key === 'Escape' && !accordion())) { e.preventDefault(); e.stopPropagation(); const k = t.closest('.rail-i').querySelector('.major'); if (!accordion()) openFly(null); k.focus(); }
    }
  });
}

/* ---- drawer (rail) and sheet (filters) on narrow screens ---- */
let sheetOpener = null;
function setSheet(which, open) {
  const box = which === 'rail' ? $('#rail-wrap') : $('#fpanel'), scrim = which === 'rail' ? $('#rail-scrim') : $('#fp-scrim'), btn = which === 'rail' ? $('#rail-open') : $('#filter-open');
  if (open) closeSheets();
  box.classList.toggle('open', open); scrim.hidden = !open; btn.setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('lock', open);
  if (open) { sheetOpener = btn; const f = box.querySelector('button:not(:disabled), input:not(:disabled)'); if (f) f.focus(); }
  else if (sheetOpener === btn) { btn.focus(); sheetOpener = null; }
}
function closeSheets() { ['rail', 'filter'].forEach((w) => { const b = w === 'rail' ? $('#rail-wrap') : $('#fpanel'); if (b.classList.contains('open')) setSheet(w, false); }); }
function initSheets() {
  $('#rail-open').addEventListener('click', () => setSheet('rail', true));
  $('#rail-close').addEventListener('click', () => setSheet('rail', false)); $('#rail-done').addEventListener('click', () => setSheet('rail', false));
  $('#rail-scrim').addEventListener('click', () => setSheet('rail', false));
  $('#filter-open').addEventListener('click', () => setSheet('filter', true));
  $('#fp-close').addEventListener('click', () => setSheet('filter', false)); $('#fp-apply').addEventListener('click', () => setSheet('filter', false));
  $('#fp-scrim').addEventListener('click', () => setSheet('filter', false));
  document.addEventListener('keydown', (e) => {
    const open = $('#rail-wrap').classList.contains('open') ? 'rail' : $('#fpanel').classList.contains('open') ? 'filter' : null;
    if (!open) return;
    if (e.key === 'Escape') { e.preventDefault(); setSheet(open, false); return; }
    if (e.key !== 'Tab') return;                                             // focus trap
    const box = open === 'rail' ? $('#rail-wrap') : $('#fpanel');
    const f = [...box.querySelectorAll('button:not(:disabled):not([hidden]), input:not(:disabled), [tabindex="0"]')].filter((x) => x.offsetParent !== null && x.tabIndex >= 0);
    if (!f.length) return; const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  matchMedia('(min-width: 900px)').addEventListener('change', closeSheets);
}

/* ---- filter schema (GET /api/filters) normalised to {key,label,type,values:[{key,label,min,max}],unit,level} ---- */
const DUP_GROUPS = new Set(['brand', 'region', 'price', 'price_band', 'keyword', 'spec_known', 'door_type', 'machine_type', 'cook_type']);   // covered by the menu / top bar / synthetic groups
const normGroup = (g) => ({ key: g.key, label: g.label_ko || g.label || g.key, type: g.type || 'multi', level: g.level || 'listing', unit: g.unit || '', units: g.display_units || [],
  open: g.default_open !== false, moreAfter: g.more_after || 6, source: g.source || [],
  values: (g.values || []).map((v) => ({ key: String(v.key), label: v.label_ko || v.label || v.key, min: v.min == null ? null : +v.min, max: v.max == null ? null : +v.max })) });
let schemaKey = '', schemaSeq = 0;
async function loadFilterSchema() {
  const subs = [...S.selSubs]; if (!subs.length) { if (S.fgroups.length) { S.fgroups = []; renderFilters(); } schemaKey = ''; return; }
  const fm = (S.subs.get(subs.find((s) => (S.subs.get(s) || {}).major === S.fmajor) || subs[0]) || {}).major || S.fmajor;
  const sub = subs.length === 1 ? subs[0] : (S.fmajor && subs.some((s) => S.subs.get(s).major === S.fmajor) ? S.fmajor : fm);
  const key = `${sub}|${[...S.regions].sort().join(',')}`; if (key === schemaKey) return; schemaKey = key; const seq = ++schemaSeq;
  try {
    const r = await api(`/api/filters?subcategory=${encodeURIComponent(sub)}&region=${encodeURIComponent([...S.regions].join(','))}`);
    if (seq !== schemaSeq) return;
    S.hasFilters = true; S.fgroups = (r.groups || []).filter((g) => !DUP_GROUPS.has(g.key)).map(normGroup);
  } catch { if (seq !== schemaSeq) return; S.hasFilters = false; S.fgroups = []; }
  S.fsel.forEach((_, k) => { if (!groupList().some((g) => g.key === k)) S.fsel.delete(k); });
  renderFilters();
}
/* groups shown = synthetic ones built from the loaded candidates + server groups */
let glMemo = null;
function groupList() {
  if (glMemo && glMemo.g === S.groups && glMemo.f === S.fgroups && glMemo.r === S.regions.size) return glMemo.out;
  const cs = allCands(), out = [];
  const done = (o) => { glMemo = { g: S.groups, f: S.fgroups, r: S.regions.size, out: o }; return o; };
  const uniq = (f) => [...new Set(cs.map(f).filter(Boolean))].sort();
  if (cs.length) {
    out.push({ key: '__brand', label: '제조사(브랜드)', type: 'multi', level: 'listing', synth: true, open: true, moreAfter: 6, values: uniq((c) => c.brand).map((b) => ({ key: b, label: b })), get: (c) => [c.brand] });
    if (S.groups.length && new Set(cs.map((c) => c.subcategory)).size > 1) out.push({ key: '__sub', label: '소분류', type: 'multi', level: 'listing', synth: true, open: true, moreAfter: 8, values: uniq((c) => c.subcategory).map((k) => ({ key: k, label: (S.subs.get(k) || {}).label_ko || k })), get: (c) => [c.subcategory] });
    if (new Set(cs.map((c) => c.region).filter(Boolean)).size > 1) out.push({ key: '__region', label: '출향지', type: 'multi', level: 'listing', synth: true, open: true, moreAfter: 6, values: uniq((c) => c.region).map((k) => ({ key: k, label: regionLabel(k) })), get: (c) => [c.region] });
    const cur = [...new Set(cs.filter((c) => c.price_usd == null && c.price_local != null).map((c) => c.currency || ''))];
    out.push({ key: '__price', label: '가격대', type: 'range', level: 'listing', synth: true, open: true, unit: cur.length === 1 && cs.every((c) => c.price_usd == null) ? cur[0] : 'USD', values: [], get: (c) => (c.price_usd != null ? [c.price_usd] : c.price_local != null ? [c.price_local] : null) });
  }
  return done(out.concat(S.fgroups));
}
/* candidate -> value keys (array) or null when unknown. Reads cand.attrs[group.key], then the schema's `source` paths. */
function candVals(c, g) {
  if (g.get) return g.get(c);
  const a = c.attrs || {}; let v = a[g.key];
  for (const src of g.source) { if (v != null) break; v = a[String(src).replace(/^attrs\./, '')]; }
  if (v == null || v === '') return null;
  const range = g.type === 'range' && !g.values.length;
  if (typeof v === 'number') {
    if (range) return [v];
    const hit = g.values.filter((x) => (x.min == null || v >= x.min) && (x.max == null || v < x.max)).map((x) => x.key);
    return hit.length ? hit : [String(v)];
  }
  if (typeof v === 'boolean') return v ? ['true'] : ['false'];
  return (Array.isArray(v) ? v : [v]).map(String);
}
function matchGroup(c, g, sel) {
  const vals = candVals(c, g); if (vals == null) return S.funk.has(g.key);
  if (g.type === 'range' && !g.values.length) { const n = vals[0]; return (sel.min == null || n >= sel.min) && (sel.max == null || n <= sel.max); }
  return vals.some((v) => sel.has(v));
}
function passes(c, skip) {
  if (!c) return false;
  if (S.filter && !`${c.brand} ${c.model_number} ${c.name} ${subLabel(c)}`.toLowerCase().includes(S.filter)) return false;
  if (!S.fsel.size) return true;
  const gl = groupList();
  for (const [k, sel] of S.fsel) { if (k === skip) continue; const g = gl.find((x) => x.key === k); if (g && !matchGroup(c, g, sel)) return false; }
  return true;
}

/* ---- filter panel ---- */
const fopen = new Map(), fmore = new Set();
function setSel(g, fn) { const cur = S.fsel.get(g.key); const next = fn(cur); if (next == null || (next instanceof Set && !next.size)) S.fsel.delete(g.key); else S.fsel.set(g.key, next); applyFilter(); }
function fRow(g, v, count, checked, on) {
  return el('label', { class: 'frow' + (count === 0 && !checked ? ' zero' : '') }, el('input', { type: 'checkbox', checked, 'data-fk': `f-${g.key}-${v.key}`, onchange: (e) => on(e.target.checked) }),
    el('span', { class: 'fl' }, v.label), count == null ? null : el('span', { class: 'fc' }, String(count)));
}
function renderFilters() {
  const body = $('#fp-body'), cs = allCands(), loaded = cs.length > 0, gl = groupList(), active = S.fsel.size;
  const vis = cs.filter((c) => passes(c)).length;
  $('#fp-n').textContent = loaded ? `${vis} / ${cs.length}건` : '';
  $('#fp-reset').disabled = !active; $('#filter-open-t').textContent = active ? `필터 (${active})` : '필터';
  $('#fp-note').textContent = loaded ? '받아온 후보를 즉시 좁힙니다. 다시 검색하지 않습니다.' : '후보 검색 후 건수가 표시됩니다. 스펙 필터는 상세 수집된 제품에만 정확히 적용됩니다.';
  $('#fp-apply').textContent = loaded ? `결과 ${vis}건 보기` : '닫기';
  keepFocus(() => {
    if (!gl.length) { body.replaceChildren(el('p', { class: 'empty-band' }, S.selSubs.size ? '표시할 필터가 없습니다.' : '왼쪽 메뉴에서 소분류를 선택하면 필터가 나타납니다.')); return; }
    body.replaceChildren(...gl.map((g) => groupView(g, cs, loaded)));
  });
  renderActive(gl);
}
function groupView(g, cs, loaded) {
  const pool = loaded ? cs.filter((c) => passes(c, g.key)) : [], sel = S.fsel.get(g.key);
  const det = el('details', { class: 'fg', open: fopen.has(g.key) ? fopen.get(g.key) : g.open !== false, ontoggle: (e) => fopen.set(g.key, e.target.open) });
  det.append(el('summary', {}, el('span', { class: 'gl' }, g.label), g.level === 'spec' ? el('span', { class: 'lv', title: '상세 수집된 제품에만 정확히 적용됩니다' }, '스펙') : null, g.units && g.units.length > 1 ? el('span', { class: 'un' }, g.units.join(' / ')) : (g.unit ? el('span', { class: 'un' }, g.unit) : null), sel && (sel.size || sel.min != null || sel.max != null) ? el('i', { class: 'dot', 'aria-label': '적용 중' }) : null));
  const bd = el('div', { class: 'fg-b' });
  if (g.type === 'range' && !g.values.length) {
    const cur = sel || {}, read = (e) => { const f = (id) => { const x = parseFloat($(id).value); return Number.isFinite(x) ? x : null; }; const mn = f('#fr-min-' + g.key), mx = f('#fr-max-' + g.key); setSel(g, () => (mn == null && mx == null ? null : { min: mn, max: mx })); };
    const nums = pool.map((c) => candVals(c, g)).filter(Boolean).map((v) => v[0]);
    bd.append(el('div', { class: 'range' },
      el('label', {}, el('span', { class: 'vh' }, `${g.label} 최소`), el('input', { type: 'number', inputmode: 'decimal', id: 'fr-min-' + g.key, placeholder: nums.length ? String(Math.floor(Math.min(...nums))) : '최소', value: cur.min == null ? '' : cur.min, onchange: read })),
      el('span', { 'aria-hidden': 'true' }, '–'),
      el('label', {}, el('span', { class: 'vh' }, `${g.label} 최대`), el('input', { type: 'number', inputmode: 'decimal', id: 'fr-max-' + g.key, placeholder: nums.length ? String(Math.ceil(Math.max(...nums))) : '최대', value: cur.max == null ? '' : cur.max, onchange: read })),
      el('span', { class: 'ru' }, g.unit || '')));
  } else {
    const vals = g.type === 'boolean' ? [{ key: 'true', label: '있음' }] : g.values;
    const counts = new Map(); let unk = 0;
    pool.forEach((c) => { const v = candVals(c, g); if (v == null) unk++; else new Set(v).forEach((k) => counts.set(k, (counts.get(k) || 0) + 1)); });
    vals.forEach((v) => { if (loaded && !counts.has(v.key)) counts.set(v.key, 0); });
    const chosen = sel instanceof Set ? sel : new Set(), more = fmore.has(g.key);
    const order = vals.filter((v, i) => more || i < g.moreAfter || chosen.has(v.key));
    order.forEach((v) => bd.append(fRow(g, v, loaded ? counts.get(v.key) : null, chosen.has(v.key), (on) => setSel(g, (cur) => { const n = new Set(cur || []); on ? n.add(v.key) : n.delete(v.key); return n; }))));
    if (vals.length > order.length || (more && vals.length > g.moreAfter)) bd.append(el('button', { class: 'link-btn more', type: 'button', 'aria-expanded': String(more), 'data-fk': 'fm-' + g.key,
      onclick: () => { more ? fmore.delete(g.key) : fmore.add(g.key); renderFilters(); } }, more ? '접기' : `더보기 (+${vals.length - order.length})`));
    if (loaded && unk > 0 && !g.synth) bd.append(el('label', { class: 'frow unk' }, el('input', { type: 'checkbox', checked: S.funk.has(g.key), 'data-fk': 'fu-' + g.key, onchange: (e) => { e.target.checked ? S.funk.add(g.key) : S.funk.delete(g.key); applyFilter(); } }),
      el('span', { class: 'fl' }, el('span', { class: 'qm', 'aria-hidden': 'true' }, '?'), '미확인 포함', el('span', { class: 'vh' }, '(리스팅에 정보 없는 후보)')), el('span', { class: 'fc', title: '리스팅에 정보가 없는 후보. 체크하면 포함합니다.' }, String(unk))));
    if (!vals.length) bd.append(el('p', { class: 'empty-band' }, '값이 없습니다.'));
  }
  det.append(bd); return det;
}
function renderActive(gl) {
  let box = $('#active-f');
  if (!box) { box = el('div', { id: 'active-f', class: 'active-f', 'aria-label': '적용 중인 필터' }); $('#candidates').insertBefore(box, $('#cand-body')); }
  const chips = [];
  S.fsel.forEach((sel, k) => {
    const g = gl.find((x) => x.key === k); if (!g) return;
    if (sel instanceof Set) sel.forEach((v) => chips.push([`${g.label}: ${(g.values.find((x) => x.key === v) || { label: v === 'true' ? '있음' : v }).label}`, () => setSel(g, (cur) => { const n = new Set(cur); n.delete(v); return n; })]));
    else chips.push([`${g.label}: ${sel.min == null ? '' : sel.min}–${sel.max == null ? '' : sel.max}${g.unit ? ' ' + g.unit : ''}`, () => setSel(g, () => null)]);
  });
  box.hidden = !chips.length;
  box.replaceChildren(...chips.map(([t, fn]) => el('span', { class: 'chip' }, el('span', { class: 'cs' }, t), el('button', { type: 'button', 'aria-label': `${t} 해제`, onclick: fn }, icon(I.x)))));
}
function resetFilters() { S.fsel.clear(); S.funk.clear(); applyFilter(); }

/* ---- header crumb, stale banner, placeholders ---- */
function updateStale() {
  const st = $('#stale'), stale = !!S.result && searchKey() !== S.searchKey;
  st.hidden = !stale;
  if (stale) st.replaceChildren(icon(I.warn, 'glyph'), el('div', {}, '검색 조건(출향지·브랜드·소분류)이 바뀌었습니다. 위의 ', el('b', {}, '후보 검색'), ' 버튼을 눌러 후보를 다시 불러오세요. 아래 목록은 이전 검색 결과입니다.'));
}
function syncShell() {
  const subs = [...S.selSubs].map((s) => (S.subs.get(s) || {}).label_ko || s), majors = S.cats.filter((c) => S.selMajors.has(c.key)).map((c) => c.label_ko);
  $('#crumb').textContent = `${[...S.regions].map(regionLabel).join('·')} · ${majors.length ? majors.join(' · ') + ' › ' : ''}${subs.length ? subs[0] + (subs.length > 1 ? ` 외 ${subs.length - 1}` : '') : '소분류를 선택하세요'}`;
  $('#brand-btn-t').textContent = `브랜드 ${brandSel()}/${S.brands.length}`;
  $('#brand-btn').title = `선택 ${brandSel()}개 · 현재 출향지에서 선택 가능 ${S.brands.filter(brandOk).length}개 · 전체 ${S.brands.length}개`;
  $('#v2-empty').hidden = !$('#candidates').hidden;
  updateStale(); loadFilterSchema();
}
function renderShell() { renderRegions(); renderBrandPop(); renderRail(); syncShell(); renderFilters(); }

/* ---- init ---- */
async function initShell() {
  document.body.classList.add('ui-v2');
  $('#setup').hidden = true; $('#shell').hidden = false; $('#subbar').hidden = false;
  $('#v2-band').append($('#band-step .step-b')); $('#v2-cta').append($('#cta-row')); $('#v2-recent').append($('#recent'));
  initBrandPop(); initRail(); initSheets();
  $('#fp-reset').addEventListener('click', resetFilters);
  document.querySelectorAll('.stages a, a.brand').forEach((a) => a.addEventListener('click', (e) => {
    const st = a.dataset.stage || 'setup'; if (a.getAttribute('aria-disabled') === 'true' || (st !== 'setup' && st !== 'candidates')) return;
    e.preventDefault(); showStage(st, false); window.scrollTo({ top: 0 });
  }));
  let list = null;
  try { list = await api('/api/regions'); } catch { /* backend without regions: UI shows the default list */ }
  S.hasRegions = Array.isArray(list) && list.length > 0;
  S.regionList = (S.hasRegions ? list : DEFAULT_REGIONS).map((r) => ({ key: r.key, label_ko: r.label_ko || r.key, enabled: r.enabled !== false, note: r.note || (r.enabled === false ? '준비 중' : ''), brands: Array.isArray(r.brands) && r.brands.length ? r.brands : null, def: !!r.default }));
  if (!S.hasRegions) S.regionList.forEach((r) => { if (r.key !== 'na') r.enabled = false; });
}

/* ---------- boot ---------- */
async function boot() {
  let v1 = false; try { v1 = localStorage.getItem('gauge.ui') === 'v1'; } catch { /* ignore */ }
  S.v2 = !v1;
  if (v1) { $('#main').className = 'wrap'; $('#main').insertBefore($('#candidates'), $('#collect')); } else await initShell();
  $('#go-search').addEventListener('click', runSearch);
  $('#go-collect').addEventListener('click', runCollect);
  document.querySelectorAll('.stages a').forEach((a) => a.addEventListener('click', (e) => { if (a.getAttribute('aria-disabled') === 'true') e.preventDefault(); }));
  try {
    const [brands, cats, meta] = await Promise.all([api('/api/brands'), api('/api/categories'), api('/api/meta').catch(() => ({}))]);
    loadCatalog(brands, cats); $('#mockflag').hidden = !meta.mock; S.maxSel = +meta.max_selected || 12; S.maxCombos = +meta.max_search_combos || 120;
    const saved = lsGet(LS + 'sel', null);
    if (saved) applySel(saved);
    if (S.v2) {                                                      // regions: only enabled ones; default 북미
      const en = S.regionList.filter((r) => r.enabled).map((r) => r.key);
      S.regions = new Set([...S.regions].filter((k) => en.includes(k)));
      if (!S.regions.size) S.regions.add(en.includes('na') ? 'na' : en[0] || 'na');
    }
    if (!saved || !S.selBrands.size) S.selBrands = new Set(S.brands.filter((b) => b.enabled && regionOk(b)).map((b) => b.name));
    if (!S.selMajors.size) { const c = S.cats.find((x) => majorState(x).ok); if (c) S.selMajors.add(c.key); }
    prune();
    renderAllSetup(); renderMatrix(); renderRecent(); updateSummary();
  } catch (e) {
    $('#brands').replaceChildren(el('div', { class: 'state err-s' }, icon(I.warn, 'glyph'), el('h3', {}, '서버에 연결하지 못했습니다'), el('p', {}, e.message),
      el('div', { class: 'acts' }, el('button', { class: 'btn', type: 'button', onclick: () => location.reload() }, '새로고침'))));
    $('#cats').replaceChildren();
  }
}
boot();
})();
