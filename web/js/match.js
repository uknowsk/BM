/* 경쟁 모델 선별: form -> POST /api/match, launch radar -> GET /api/launches. All site-derived text goes in via textContent. */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'title') n.title = v;
      else if (k.startsWith('data-')) n.setAttribute(k, v);
      else if (k === 'style') n.style.cssText = v;
      else n[k] = v;
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  };
  const CUR = { us: 'USD', kr: 'KRW', de: 'EUR', uk: 'GBP', br: 'BRL' };
  const FEAT_KO = { wifi: 'Wi-Fi', convection: '컨벡션', air_fry: '에어프라이', steam: '스팀', energy_star: 'ENERGY STAR' };
  const BASIS_KO = { release_date: '출시일', site_new: '사이트 NEW', first_seen: '최초 발견', site_order: '사이트 최신순', unknown: '출시 시점 미확인' };
  const COMP_KO = { price: '가격', spec: '스펙', recency: '최근성', response: '호응' };
  let radarTimer = 0;

  function money(v, cur) {
    if (v == null) return '가격 미확인';
    try { return new Intl.NumberFormat('ko-KR', { style: 'currency', currency: cur || 'USD', maximumFractionDigits: cur === 'KRW' || cur === 'JPY' ? 0 : 2 }).format(v); }
    catch { return `${v} ${cur || ''}`; }
  }
  const toast = (m) => { const t = $('#toast'); t.textContent = m; t.classList.add('on'); setTimeout(() => t.classList.remove('on'), 2600); };

  /* theme (same storage key as the main page) */
  $('#theme').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('gauge.theme', next); } catch { /* ignore */ }
  });

  /* remembered form values (a convenience only) */
  const KEY = 'gauge.match.form';
  const FIELDS = ['sub', 'country', 'price', 'band', 'tier-window', 'width', 'cap', 'fuel', 'burners', 'w-price', 'w-spec', 'w-recency', 'w-response'];
  let savedSub = null;
  function save() {
    try {
      const o = Object.fromEntries(FIELDS.map((f) => [f, $('#' + f).value]));
      o.feat = [...document.querySelectorAll('input[name=feat]:checked')].map((i) => i.value);
      localStorage.setItem(KEY, JSON.stringify(o));
    } catch { /* ignore */ }
  }
  function restore() {
    try {
      const o = JSON.parse(localStorage.getItem('gauge.match.form') || 'null');
      if (!o) return;
      FIELDS.forEach((f) => { if (o[f] != null && f !== 'sub') $('#' + f).value = o[f]; });
      document.querySelectorAll('input[name=feat]').forEach((i) => { i.checked = (o.feat || []).includes(i.value); });
      savedSub = o.sub;
    } catch { /* ignore */ }
  }

  /* live labels */
  const bandV = () => { $('#band-v').textContent = `±${$('#band').value}%`; };
  const wV = (k) => { $('#w-' + k + '-v').textContent = $('#w-' + k).value; };
  const curV = () => { $('#cur').textContent = CUR[$('#country').value] || 'USD'; };

  async function api(path, body) {
    const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
    const r = await fetch(path, opt);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : '요청을 처리하지 못했습니다. 입력값을 확인하세요.');
    return j;
  }

  async function loadSubs() {
    const cats = await api('/api/categories?region=na,kr,eu');
    const cooking = cats.find((c) => c.key === 'cooking');
    const sel = $('#sub');
    sel.replaceChildren(...(cooking ? cooking.children.filter((k) => k.enabled).map((k) => el('option', { value: k.key }, k.label_ko)) : []));
    if (savedSub && [...sel.options].some((o) => o.value === savedSub)) sel.value = savedSub;
  }

  /* ---------- result ---------- */
  function readiness(d) {
    const evidence = d.with_release_date + d.site_new_flagged + d.new_discoveries;
    const thin = d.models < 20 || evidence === 0;
    const n = (v) => el('b', {}, v);
    return el('div', { class: 'mt-data' + (thin ? ' thin' : '') },
      el('span', {}, '분석 대상 ', n(d.models), '개 모델'),
      el('span', {}, '가격 확인 ', n(d.with_price)),
      el('span', {}, '평점 확인 ', n(d.with_rating)),
      el('span', {}, '출시 시점 근거 ', n(evidence)),
      el('span', { class: 'sp' }, thin
        ? '근거가 부족합니다. 브랜드·소분류를 더 검색하거나(후보 검색), 평점과 출시일은 상세 수집 후 더 채워집니다. 점수는 참고용으로만 보세요.'
        : `출시일 ${d.with_release_date} · 사이트 NEW ${d.site_new_flagged} · 앱이 새로 발견한 모델 ${d.new_discoveries}개를 근거로 최근성을 판단합니다.`));
  }

  function tierStrip(out) {
    const me = out.target.tier;
    return el('div', {},
      el('div', { class: 'tiers', role: 'list', 'aria-label': '가격 5단계' }, out.tiers.map((t) => {
        const far = me && Math.abs(t.tier - me) > 1;
        return el('div', { class: 'tier' + (t.tier === me ? ' me' : '') + (far ? ' far' : ''), role: 'listitem' },
          el('b', {}, `${t.tier}. ${t.label}`),
          el('span', { class: 'rg' }, t.min == null ? '해당 모델 없음' : `${money(t.min, out.target.currency)} ~ ${money(t.max, out.target.currency)}`),
          el('div', {}, `${t.count}개`));
      })),
      el('p', { class: 'hint' }, `단계 기준: ${out.tier_scope}의 가격 분포 5등분. 같은 단계 안에서도 목표 가격에 가까울수록 근접도 점수가 높습니다.`));
  }

  function compCell(key, c) {
    const unk = c.score == null;
    return el('div', { class: 'comp' + (unk ? ' unk' : ''), title: c.tip },
      COMP_KO[key] + (unk ? ' 미확인' : ` ${Math.round(c.score * 100)}`),
      el('div', { class: 'bar' }, el('i', { style: `width:${unk ? 0 : Math.round(c.score * 100)}%` })));
  }

  function rowOf(r, i) {
    const comp = r.components, rec = comp.recency, resp = comp.response;
    const tips = {
      price: { score: comp.price.score, tip: comp.price.delta_pct == null ? '가격 미확인' : `목표 대비 ${comp.price.delta_pct > 0 ? '+' : ''}${comp.price.delta_pct}%` },
      spec: { score: comp.spec.score, tip: comp.spec.details.map((d) => `${d.key}: 기준 ${d.want} / 모델 ${d.actual ?? '미확인'}`).join('\n') || '입력한 스펙 없음' },
      recency: { score: rec.score, tip: `${BASIS_KO[rec.basis] || rec.basis}: ${rec.evidence}` },
      response: { score: resp.score, tip: resp.adjusted == null ? '평점·리뷰 수 미확인' : `평점 ${resp.rating} (리뷰 ${resp.reviews}건) → 보정 ${resp.adjusted}` },
    };
    const tags = [];
    if (r.is_launch) tags.push(el('span', { class: 'tag new', title: rec.evidence }, `신제품 · ${BASIS_KO[rec.basis]}`));
    else if (rec.basis !== 'unknown') tags.push(el('span', { class: 'tag', title: rec.evidence }, `${BASIS_KO[rec.basis]}${rec.months != null ? ` ${Math.round(rec.months)}개월 전` : ''}`));
    else tags.push(el('span', { class: 'tag dim' }, '출시 시점 미확인'));
    if (resp.adjusted != null) tags.push(el('span', { class: 'tag good', title: `리뷰 ${resp.reviews}건` }, `★ ${resp.rating} (${resp.reviews})`));
    else tags.push(el('span', { class: 'tag dim' }, '평점 미확인'));
    const link = /^https:\/\//.test(r.url) ? el('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.model_number) : el('span', {}, r.model_number);
    return el('tr', {},
      el('td', { class: 'n' }, i + 1),
      el('td', { class: 'nm' }, el('div', {}, el('b', {}, r.brand), ' ', link), el('small', { title: r.name }, r.name), el('div', {}, tags)),
      el('td', { class: 'pr' }, money(r.price, r.currency),
        el('small', {}, r.tier ? `${r.tier}단계 ${r.tier_label}${r.tier_diff ? ` (${r.tier_diff > 0 ? '+' : ''}${r.tier_diff})` : ''}` : '단계 미확인')),
      el('td', {}, el('div', { class: 'score' }, el('div', { class: 'bar' }, el('i', { style: `width:${r.total ?? 0}%` })), el('b', {}, r.total == null ? '—' : Math.round(r.total))),
        el('div', { class: 'cov' }, `근거 ${Math.round(r.coverage * 100)}%`)),
      el('td', {}, el('div', { class: 'comps' }, Object.keys(COMP_KO).map((k) => compCell(k, tips[k])))));
  }

  function renderResult(out) {
    const t = out.target;
    const head = el('p', { class: 'hint' },
      `기준: ${t.tier ? `${t.tier}단계(${t.tier_label}) · ` : ''}${t.price ? money(t.price, t.currency) : '가격 미입력'} ±${out.band_pct}% · 후보 ${out.counts.pool}개 중 `,
      el('b', {}, `${out.counts.ranked}개`), ' 표시',
      out.counts.excluded_by_spec ? ` · 폭 조건 밖 ${out.counts.excluded_by_spec}개 제외` : '',
      out.counts.outside_tier_window ? ` · 단계 범위 밖 ${out.counts.outside_tier_window}개 제외` : '',
      out.counts.price_unknown ? ` · 가격 미확인 ${out.counts.price_unknown}개는 가격 비교가 불가능해 순위에서 제외` : '');
    const rows = out.results.length
      ? el('div', { class: 'rank-wrap' }, el('table', { class: 'rank' },
        el('thead', {}, el('tr', {}, ['#', '모델', '가격 · 단계', '종합 점수', '요소별 점수 (마우스를 올리면 근거)'].map((h) => el('th', { scope: 'col' }, h)))),
        el('tbody', {}, out.results.map((r, i) => rowOf(r, i)))))
      : el('p', { class: 'v2-empty' }, '조건에 맞는 모델이 없습니다. 허용폭이나 단계 범위를 넓히거나, 먼저 해당 소분류를 후보 검색해 이력을 쌓으세요.');
    $('#out').replaceChildren(readiness(out.data), tierStrip(out), head, rows);
  }

  /* ---------- launch radar ---------- */
  function renderRadar(d, cur) {
    $('#radar').hidden = false;
    $('#radar-sub').textContent = `${d.totals.models}개 모델 중 신제품 ${d.totals.new}개`;
    const cards = Object.entries(d.by_brand).map(([brand, list]) =>
      el('div', { class: 'rcard' }, el('h3', {}, brand, el('span', {}, `${list.length}개`)),
        el('ul', {}, list.map((m) => el('li', {},
          /^https:\/\//.test(m.url) ? el('a', { href: m.url, target: '_blank', rel: 'noopener noreferrer' }, m.model_number) : m.model_number,
          ' ', money(m.price, m.currency || cur),
          el('small', {}, `${BASIS_KO[m.basis] || m.basis} — ${m.evidence}${m.rating ? ` · ★ ${m.rating} (${m.review_count})` : ''}`))))));
    const trendRows = d.trend.filter((t) => t.new.known && t.existing.known).map((t) => {
      const a = t.new.pct ?? 0, b = t.existing.pct ?? 0, delta = t.delta_pts;
      return el('div', { class: 'trow' + (t.low_sample ? ' low' : ''), title: t.low_sample ? '표본이 적어 참고용입니다' : '' },
        el('span', {}, FEAT_KO[t.feature] || t.feature),
        el('div', { class: 'bars' }, el('i', { class: 'a', style: `width:${a}%` }), el('i', { class: 'b', style: `width:${b}%` })),
        el('span', { class: 'd' + (delta > 0 ? ' up' : '') }, delta == null ? '비교 불가' : `${delta > 0 ? '+' : ''}${delta}%p`));
    });
    const med = d.median_price;
    $('#radar-body').replaceChildren(...[
      cards.length ? el('div', { class: 'radar-grid' }, cards)
        : el('p', { class: 'v2-empty' }, '아직 신제품으로 판단할 근거가 없습니다. 출시일·NEW 표시를 제공하는 사이트가 적고, 앱이 처음 조회한 모델은 기준선이라 신제품에서 제외됩니다. 같은 소분류를 며칠 간격으로 다시 검색하면 새로 올라온 모델이 여기에 나타납니다.'),
      trendRows.length ? el('div', { class: 'trend' }, el('h3', {}, '신제품에서 늘어난 기능'), trendRows,
        el('div', { class: 'legend' }, el('span', {}, el('i', { style: 'background:var(--accent)' }), '신제품'), el('span', {}, el('i', { style: 'background:var(--ink-3)' }), '기존 모델')),
        med.new != null && med.existing != null ? el('p', { class: 'hint' }, `가격 중앙값: 신제품 ${money(med.new, cur)} / 기존 ${money(med.existing, cur)}`) : null) : null,
      el('p', { class: 'hint' }, d.note),
      d.distrusted_new_flags && d.distrusted_new_flags.length
        ? el('p', { class: 'hint' }, `신뢰하지 않은 NEW 표시: ${d.distrusted_new_flags.join(', ')} — 목록 대부분이 NEW로 표시돼 신제품 구분에 쓸 수 없어 제외했습니다.`) : null,
      Object.keys(d.basis_counts).length
        ? el('p', { class: 'hint' }, '근거 구성: ' + Object.entries(d.basis_counts).map(([k, v]) => `${BASIS_KO[k] || k} ${v}`).join(' · ')) : null,
    ].filter(Boolean));  // replaceChildren(null) would print the text "null"
  }

  async function loadRadar() {
    const sub = $('#sub').value, cc = $('#country').value;
    if (!sub) return;
    try {
      renderRadar(await api(`/api/launches?sub=${encodeURIComponent(sub)}&country=${cc}&window=${$('#window').value}`), CUR[cc]);
    } catch { $('#radar').hidden = true; }
  }
  const radarSoon = () => { clearTimeout(radarTimer); radarTimer = setTimeout(loadRadar, 250); };

  /* ---------- submit ---------- */
  function body() {
    const num = (id) => { const v = parseFloat($('#' + id).value); return Number.isFinite(v) ? v : undefined; };
    const burners = num('burners');
    const specs = { width_in: num('width'), capacity_total_cuft: num('cap'), burners: burners && Math.round(burners), fuel: $('#fuel').value || undefined,
      features: [...document.querySelectorAll('input[name=feat]:checked')].map((i) => i.value) };
    Object.keys(specs).forEach((k) => specs[k] === undefined && delete specs[k]);
    const tw = $('#tier-window').value;
    return { sub: $('#sub').value, country: $('#country').value, price: num('price'), specs,
      weights: { price: +$('#w-price').value, spec: +$('#w-spec').value, recency: +$('#w-recency').value, response: +$('#w-response').value },
      band_pct: +$('#band').value, tier_window: tw === '' ? null : +tw, top: 20 };
  }

  $('#form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const err = $('#err'), out = $('#out'), go = $('#go');
    err.hidden = true;
    const b = body();
    if (!b.price) { err.textContent = '목표 가격을 입력하세요. 가격이 없으면 가격 근접도와 단계를 계산할 수 없습니다.'; err.hidden = false; $('#price').focus(); return; }
    if (Object.values(b.weights).every((v) => v === 0)) { err.textContent = '중요도를 하나 이상 올리세요.'; err.hidden = false; return; }
    go.disabled = true; out.setAttribute('aria-busy', 'true'); save();
    try { renderResult(await api('/api/match', b)); loadRadar(); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
    finally { go.disabled = false; out.setAttribute('aria-busy', 'false'); }
  });

  /* ---------- scheduled re-search ---------- */
  const STATUS_KO = { ok: '정상', partial: '일부 실패', failed: '실패', cancelled: '취소됨' };
  const everyKo = (h) => ({ 12: '12시간마다', 24: '매일', 72: '3일마다', 168: '매주', 336: '2주마다', 720: '매월' }[h] || `${h}시간마다`);
  const whenKo = (iso) => {
    const d = iso ? new Date(iso) : null;
    return d && !Number.isNaN(+d) ? d.toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
  };
  let schedTimer = 0;

  function schedCard(s) {
    const c = s.config, sum = s.last_summary;
    const status = s.running ? el('span', { class: 'tag new' }, '실행 중')
      : !s.enabled ? el('span', { class: 'tag dim' }, '중지됨')
        : s.last_status ? el('span', { class: 'tag' + (s.last_status === 'ok' ? ' good' : s.last_status === 'failed' ? '' : ' dim') }, STATUS_KO[s.last_status] || s.last_status)
          : el('span', { class: 'tag dim' }, '곧 실행');
    const result = sum
      ? `후보 ${sum.candidates}개 · 새로 발견 ${sum.new_discoveries}개 · ${sum.duration_s}초${sum.brands_failed.length ? ` · 실패: ${sum.brands_failed.join(', ')}` : ''}`
      : '아직 실행 기록이 없습니다';
    const act = async (path, label, body) => {
      try { await api(`/api/schedules/${s.id}/${path}`, body || {}); toast(label); } catch (e) { toast(e.message); }
      loadSchedules();
    };
    return el('div', { class: 'scard' },
      el('div', { class: 'scard-h' }, el('b', {}, s.name), status),
      el('p', { class: 'smeta' }, `${c.brands.length}개 브랜드 · 소분류 ${c.subcategories.length}개 · ${c.regions.join('/')} · 조합 ${s.combos}개 · ${everyKo(s.interval_h)} · 브랜드당 ${c.limit}개`),
      el('p', { class: 'smeta' }, `마지막 실행 ${whenKo(s.last_run)} → 다음 ${s.enabled ? whenKo(s.next_run) : '—'}`),
      el('p', { class: 'smeta' }, result, sum && sum.log && sum.log.length ? el('span', { class: 'slog', title: sum.log.join('\n') }, ' (자세히)') : null),
      el('div', { class: 'sact' },
        el('button', { class: 'btn ghost', type: 'button', disabled: s.running, onclick: () => act('run', '재검색을 시작했습니다') }, '지금 실행'),
        el('button', { class: 'btn ghost', type: 'button', onclick: () => act('toggle', s.enabled ? '예약을 중지했습니다' : '예약을 다시 켰습니다', { enabled: !s.enabled }) }, s.enabled ? '중지' : '다시 켜기'),
        el('button', { class: 'btn ghost', type: 'button', onclick: () => { if (confirm(`'${s.name}' 예약을 삭제할까요?`)) act('delete', '삭제했습니다'); } }, '삭제')));
  }

  async function loadSchedules() {
    clearTimeout(schedTimer);
    try {
      const d = await api('/api/schedules');
      $('#sched-off').hidden = d.runner;
      $('#sched-sub').textContent = d.schedules.length ? `${d.schedules.length}/${d.limits.max_schedules}개 예약` : '아직 예약이 없습니다';
      $('#sched-list').replaceChildren(...(d.schedules.length ? d.schedules.map(schedCard)
        : [el('p', { class: 'v2-empty' }, '예약이 없습니다. 아래 “새 예약 만들기”에서 브랜드와 소분류를 고르면 정해진 간격으로 새 모델을 찾아 이력에 쌓습니다.')]));
      if (d.schedules.some((s) => s.running)) schedTimer = setTimeout(loadSchedules, 5000);
    } catch { $('#sched-list').replaceChildren(el('p', { class: 'v2-empty' }, '예약 목록을 불러오지 못했습니다.')); }
  }

  const checked = (sel) => [...document.querySelectorAll(sel + ' input:checked')].map((i) => i.value);
  function schedEstimate() {
    const n = checked('#s-brands').length, m = checked('#s-subs').length, r = checked('#s-regions').length;
    $('#s-est').textContent = n && m && r ? `브랜드 ${n}개 × 소분류 ${m}개 × 시장 ${r}개 = 최대 ${n * m * r}개 조합을 한 번에 순서대로 검색합니다. 지원하지 않는 조합은 건너뜁니다.` : '';
  }

  async function buildSchedForm() {
    try {
      const brands = await api('/api/brands?region=na,kr,eu');
      $('#s-brands').replaceChildren(...brands.filter((b) => b.enabled).map((b) => el('label', {}, el('input', { type: 'checkbox', value: b.name }), b.name,
        b.delay_s ? el('small', { title: b.note }, ' 느림') : null)));
    } catch { /* the list stays empty; creating is refused by the server anyway */ }
    $('#s-subs').replaceChildren(...[...$('#sub').options].map((o) => el('label', {}, el('input', { type: 'checkbox', value: o.value }), o.textContent)));
    schedEstimate();
  }

  $('#sched-form').addEventListener('change', schedEstimate);
  $('#s-brands-all').addEventListener('click', () => { document.querySelectorAll('#s-brands input').forEach((i) => { i.checked = true; }); schedEstimate(); });
  $('#s-brands-none').addEventListener('click', () => { document.querySelectorAll('#s-brands input').forEach((i) => { i.checked = false; }); schedEstimate(); });
  $('#sched-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const err = $('#s-err');
    err.hidden = true;
    try {
      await api('/api/schedules', { name: $('#s-name').value.trim(), brands: checked('#s-brands'), subcategories: checked('#s-subs'),
        regions: checked('#s-regions'), limit: +$('#s-limit').value, interval_h: +$('#s-interval').value });
      $('#s-name').value = '';
      $('#sched-new').open = false;
      toast('예약을 만들었습니다. 곧 첫 검색이 시작됩니다.');
      loadSchedules();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });

  /* ---------- init ---------- */
  restore(); bandV(); curV(); ['price', 'spec', 'recency', 'response'].forEach(wV);
  $('#band').addEventListener('input', bandV);
  ['price', 'spec', 'recency', 'response'].forEach((k) => $('#w-' + k).addEventListener('input', () => wV(k)));
  $('#country').addEventListener('change', () => { curV(); radarSoon(); });
  $('#sub').addEventListener('change', radarSoon);
  $('#window').addEventListener('change', loadRadar);
  $('#form').addEventListener('change', save);
  loadSubs().then(() => { bandV(); curV(); loadRadar(); buildSchedForm(); loadSchedules(); }).catch(() => toast('제품군을 불러오지 못했습니다. 서버가 켜져 있는지 확인하세요.'));
})();
