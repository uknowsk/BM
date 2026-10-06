/* Dev-only mock backend. Active only with ?mock=1 — replaces window.fetch for /api/* so the UI can be built and reviewed
   without server.py. Contract mirrored here: /api/brands[].categories, /api/categories[].children, POST /api/search
   {brands, subcategories, ...} -> groups[], POST /api/collect -> products[] + groups[]. No effect without ?mock=1. */
(() => {
'use strict';
let on = false;
try { on = new URLSearchParams(location.search).get('mock') === '1'; } catch { /* ignore */ }
if (!on) return;
window.__GAUGE_MOCK__ = true;   // app.js then namespaces localStorage as 'gauge.mock.*'

const MAJORS = [
  { key: 'refrigerator', label_ko: '냉장고', children: [['french_door', '프렌치도어'], ['side_by_side', '사이드바이사이드'], ['top_freezer', '상냉동'], ['bottom_freezer', '하냉동'], ['built_in', '빌트인'], ['compact', '소형']] },
  { key: 'washer', label_ko: '세탁기', children: [['top_load', '전자동/탑로더'], ['front_load', '드럼'], ['dryer', '건조기'], ['laundry_center', '트윈/스택·워시타워']] },
  { key: 'cooking', label_ko: '조리기기', children: [['microwave', '전자레인지'], ['sco', 'SCO (스피드쿡 오븐)'], ['otr', 'OTR'], ['gas_oven', '가스오븐'], ['gas_cooktop', '가스 쿡탑'], ['electric_oven', '전기오븐'], ['induction', '인덕션'], ['radiant', '라디언트']] },
];
const SUPPORT = {
  Samsung: ['french_door', 'side_by_side', 'top_freezer', 'bottom_freezer', 'built_in', 'top_load', 'front_load', 'dryer', 'microwave', 'otr', 'gas_oven', 'gas_cooktop', 'electric_oven', 'induction', 'radiant'],
  LG: ['french_door', 'side_by_side', 'top_freezer', 'bottom_freezer', 'compact', 'top_load', 'front_load', 'dryer', 'laundry_center', 'microwave', 'sco', 'otr', 'gas_oven', 'gas_cooktop', 'electric_oven', 'induction', 'radiant'],
  KitchenAid: ['french_door', 'side_by_side', 'bottom_freezer', 'built_in', 'microwave', 'sco', 'otr', 'gas_oven', 'gas_cooktop', 'electric_oven', 'induction', 'radiant'],
  GE: ['french_door', 'side_by_side', 'top_freezer', 'bottom_freezer', 'built_in', 'compact', 'top_load', 'front_load', 'dryer', 'microwave', 'sco', 'otr', 'gas_oven', 'gas_cooktop', 'electric_oven', 'induction', 'radiant'],
  Whirlpool: ['french_door', 'side_by_side', 'top_freezer', 'bottom_freezer', 'compact', 'top_load', 'front_load', 'dryer', 'microwave', 'otr', 'gas_oven', 'gas_cooktop', 'electric_oven', 'radiant'],
  Bosch: ['french_door', 'bottom_freezer', 'built_in', 'front_load', 'dryer', 'microwave', 'gas_oven', 'gas_cooktop', 'electric_oven', 'induction', 'radiant'],
};
const DOMAIN = { Samsung: 'samsung.com', LG: 'lg.com', KitchenAid: 'kitchenaid.com', GE: 'geappliances.com', Whirlpool: 'whirlpool.com', Bosch: 'bosch-home.com' };
const PREFIX = { Samsung: 'RF', LG: 'LR', KitchenAid: 'KR', GE: 'GN', Whirlpool: 'WR', Bosch: 'B36' };
/* 6 -> 30 brands. [name, group, countries with an adapter, ready, domain, prefix, coverage]; ready=false -> '준비 중' (no adapter file yet).
   coverage: full = every sub-category, cook = cooking + built-in fridge, fridge = refrigerators, laundry = washers/dryers + cooking */
const GROUP = { Samsung: 'Samsung·LG', LG: 'Samsung·LG', KitchenAid: 'Whirlpool Corp.', GE: 'Haier·GE', Whirlpool: 'Whirlpool Corp.', Bosch: 'BSH' };
const COUNTRIES = { Samsung: ['us', 'kr'], LG: ['us', 'kr'], KitchenAid: ['us'], GE: ['us'], Whirlpool: ['us'], Bosch: ['us'] };
const NEW_BRANDS = [
  ['Maytag', 'Whirlpool Corp.', ['us'], true, 'maytag.com', 'MT', 'full'], ['JennAir', 'Whirlpool Corp.', ['us'], true, 'jennair.com', 'JA', 'cook'],
  ['Amana', 'Whirlpool Corp.', ['us'], false, 'amana.com', 'AM', 'full'], ['Thermador', 'BSH', ['us'], true, 'thermador.com', 'TH', 'cook'],
  ['Gaggenau', 'BSH', [], false, 'gaggenau.com', 'GG', 'cook'], ['Siemens', 'BSH', [], false, 'siemens-home.bsh-group.com', 'SI', 'full'],
  ['Frigidaire', 'Electrolux', ['us'], true, 'frigidaire.com', 'FG', 'full'], ['Electrolux', 'Electrolux', ['us'], true, 'electrolux.com', 'EL', 'full'],
  ['AEG', 'Electrolux', ['us'], true, 'aeg.com', 'AE', 'full'], ['Café', 'Haier·GE', ['us'], true, 'cafeappliances.com', 'CF', 'cook'],
  ['Monogram', 'Haier·GE', [], false, 'monogram.com', 'MG', 'cook'], ['Haier', 'Haier·GE', ['us'], true, 'haierappliances.com', 'HA', 'full'],
  ['Fisher & Paykel', 'Haier·GE', [], false, 'fisherpaykel.com', 'FP', 'laundry'], ['Viking', '프리미엄', ['us'], true, 'vikingrange.com', 'VK', 'cook'],
  ['Sub-Zero', '프리미엄', ['us'], true, 'subzero-wolf.com', 'SZ', 'fridge'], ['Wolf', '프리미엄', [], false, 'subzero-wolf.com', 'WF', 'cook'],
  ['Miele', '프리미엄', ['us'], true, 'miele.com', 'MI', 'full'], ['Smeg', '프리미엄', ['us'], true, 'smeg.com', 'SM', 'cook'],
  ['Liebherr', '프리미엄', ['us'], true, 'liebherr.com', 'LB', 'fridge'], ['Bertazzoni', '프리미엄', [], false, 'bertazzoni.com', 'BZ', 'cook'],
  ['De Dietrich', '프리미엄', [], false, 'dedietrich-electromenager.fr', 'DD', 'cook'], ['Beko', '글로벌', ['us'], true, 'beko.com', 'BK', 'full'],
  ['Hisense', '글로벌', [], false, 'hisense-usa.com', 'HS', 'full'], ['Panasonic', '글로벌', [], false, 'panasonic.com', 'PA', 'cook'],
];
const COVER = {
  full: () => MAJORS.flatMap((m) => m.children.map(([k]) => k)),
  cook: () => ['built_in', ...MAJORS[2].children.map(([k]) => k)],
  fridge: () => ['french_door', 'side_by_side', 'bottom_freezer', 'built_in', 'compact'],
  laundry: () => [...MAJORS[1].children.map(([k]) => k), ...MAJORS[2].children.map(([k]) => k)],
};
const NOT_READY = new Set();
NEW_BRANDS.forEach(([n, g, cc, ready, dom, pre, cov]) => { GROUP[n] = g; COUNTRIES[n] = cc; DOMAIN[n] = dom; PREFIX[n] = pre; if (ready) SUPPORT[n] = COVER[cov](); else NOT_READY.add(n); });
const SUBS = new Map(MAJORS.flatMap((m) => m.children.map(([k, l]) => [k, { label: l, major: m.key }])));
const PRICE = { refrigerator: [699, 4200], washer: [549, 2100], cooking: [149, 3400] };
const REGIONS = [
  { key: 'kr', label_ko: '한국', enabled: true, countries: ['kr'], currency: 'KRW', brands: ['Samsung', 'LG'] },
  { key: 'na', label_ko: '북미', enabled: true, countries: ['us'], currency: 'USD', brands: Object.keys(SUPPORT) },
  { key: 'eu', label_ko: '유럽', enabled: false, note: '준비 중', countries: ['de'], currency: 'EUR', brands: [] },
  { key: 'sa', label_ko: '남미', enabled: false, note: '준비 중', countries: ['br'], currency: 'BRL', brands: [] },
  { key: 'me', label_ko: '중동', enabled: false, note: '준비 중', countries: ['ae'], currency: 'AED', brands: [] },
  { key: 'as', label_ko: '아시아', enabled: false, note: '준비 중', countries: ['jp'], currency: 'JPY', brands: [] },
  { key: 'oc', label_ko: '오세아니아', enabled: false, note: '준비 중', countries: ['au'], currency: 'AUD', brands: [] },
];
const FILTERS = (sub) => [
  { key: 'capacity_total_cuft', label_ko: '총용량', type: 'multi', level: 'listing', unit: 'cu ft', display_units: ['cu ft', 'L'], default_open: true, more_after: 4,
    values: [{ key: 'lt16', label_ko: '~16 cu ft', min: 0, max: 16 }, { key: '16_20', label_ko: '16~20', min: 16, max: 20 }, { key: '20_24', label_ko: '20~24', min: 20, max: 24 }, { key: '24_28', label_ko: '24~28', min: 24, max: 28 }, { key: '28p', label_ko: '28 cu ft~', min: 28, max: null }] },
  { key: 'width_in', label_ko: '폭', type: 'multi', level: 'listing', unit: 'in', display_units: ['in', 'mm'], values: [24, 30, 33, 36].map((w) => ({ key: String(w), label_ko: `${w}in (${Math.round(w * 25.4)}mm)` })) },
  { key: 'smart', label_ko: '스마트', type: 'multi', level: 'listing', values: [{ key: 'wifi', label_ko: 'Wi-Fi' }, { key: 'app_control', label_ko: '앱 제어' }] },
  { key: 'energy_star', label_ko: 'ENERGY STAR', type: 'boolean', level: 'spec', values: [] },
  { key: 'door_type', label_ko: '도어 타입', type: 'multi', level: 'listing', values: [{ key: 'french_door', label_ko: '프렌치도어' }] },
  { key: 'price', label_ko: '가격대', type: 'range', level: 'listing', unit: 'USD', values: [] },
];
const ORDER = ['Samsung', 'LG', 'KitchenAid', 'GE', 'Whirlpool', 'Bosch', ...NEW_BRANDS.map((b) => b[0])];
const brands = () => [...Object.keys(SUPPORT), ...NOT_READY].map((n) => ({ name: n, enabled: !NOT_READY.has(n), note: NOT_READY.has(n) ? '준비 중' : '', categories: SUPPORT[n] || [], group: GROUP[n], countries: COUNTRIES[n] || [] }))
  .sort((a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name));
const categories = () => MAJORS.map((m) => ({ key: m.key, label_ko: m.label_ko, enabled: true, children: m.children.map(([key, label_ko]) => ({ key, label_ko })) }));

const hash = (s) => { let h = 2166136261; for (const c of s) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; };
const failing = (c) => c.brand === 'LG' && /-2$/.test(c.url);          // LG's 2nd model in every sub-category fails to scrape (failure path)

const attrsFor = (model, major) => {      // listing facts; some keys deliberately missing so the 'unknown' bucket shows up
  const r = hash(model + 'a'), a = {};
  if (r > 0.2) a.capacity_total_cuft = Math.round((major === 'refrigerator' ? 14 + r * 16 : 3 + r * 4) * 10) / 10;
  if (r > 0.35) a.width_in = [24, 30, 33, 36][Math.floor(r * 4) % 4];
  if (r > 0.1) a.smart = r > 0.55 ? ['wifi', 'app_control'] : ['wifi'];
  if (r > 0.5) a.energy_star = true;
  return a;
};
function candidatesFor(brand, sub, region = 'na') {
  const { label, major } = SUBS.get(sub), [lo, hi] = PRICE[major], out = [];
  for (let n = 1; n <= 4; n++) {
    const r = hash(brand + sub + n), model = `${PREFIX[brand]}${sub.slice(0, 2).toUpperCase()}${100 * n + Math.round(r * 90)}${'SR'.slice(0, 1 + (n % 2))}`;
    out.push({ brand, model_number: model, name: `${brand} ${label} ${(18 + Math.round(r * 12))}${major === 'cooking' ? '" ' : ' cu. ft. '}${label} 모델 ${n}`, url: `https://www.${DOMAIN[brand]}/${region === "kr" ? "kr" : "us"}/${sub}/${model.toLowerCase()}-${n}`,
      price_usd: hash(model) < 0.12 ? null : Math.round((lo + (hi - lo) * Math.pow(r, 1.3)) / 10) * 10 - 1, category: major, subcategory: sub, subcategory_ko: label,
      region, country: region === 'kr' ? 'kr' : 'us', ...(region === 'kr' ? { price_local: null, currency: 'KRW' } : {}), attrs: attrsFor(model, major) });
  }
  return out;
}

function searchResult(req) {
  const wanted = new Set(req.subcategories || []), groups = [];
  for (const m of MAJORS) {
    const cands = [];
    for (const b of req.brands) for (const [k] of m.children) if (wanted.has(k) && SUPPORT[b] && SUPPORT[b].includes(k)) for (const rg of (req.regions || ['na'])) if (REGIONS.find((x) => x.key === rg && x.enabled && x.brands.includes(b))) cands.push(...candidatesFor(b, k, rg));
    if (!cands.length && !m.children.some(([k]) => wanted.has(k))) continue;
    const priced = cands.map((c) => c.price_usd).filter((p) => p != null).sort((a, b) => a - b), n = priced.length;
    const thr = req.band_mode === 'custom' ? [...req.thresholds].sort((a, b) => a - b) : (n ? [priced[Math.floor(n / 3)], priced[Math.floor(2 * n / 3)]] : null);
    const bands = { budget: [], mid: [], premium: [], unknown: [] };
    cands.forEach((c) => { (c.price_usd == null ? bands.unknown : !thr ? bands.mid : c.price_usd < thr[0] ? bands.budget : c.price_usd < thr[1] ? bands.mid : bands.premium).push(c); });
    Object.values(bands).forEach((l) => l.sort((a, b) => (a.price_usd ?? 1e9) - (b.price_usd ?? 1e9)));
    groups.push({ category: m.key, label_ko: m.label_ko, bands, thresholds: thr, total: cands.length });
  }
  return { groups, failed_brands: [], total: groups.reduce((s, g) => s + g.total, 0) };
}

const POD = {
  refrigerator: [['Wi-Fi 연결 제어', 'Wi-Fi connected app control'], ['듀얼 냉각', 'Independent dual evaporators'], ['정수·얼음 디스펜서', 'Water and ice dispenser'], ['에너지스타', 'ENERGY STAR certified'], ['도어인도어', 'Door-in-door access']],
  washer: [['스팀 세탁', 'Steam wash cycle'], ['AI 세탁 감지', 'Load sensing auto-adjust'], ['세제 자동 투입', 'Auto detergent dispenser'], ['Wi-Fi 연결 제어', 'Wi-Fi connected app control'], ['소음 저감 모터', 'Inverter direct-drive motor']],
  cooking: [['에어프라이 모드', 'Air fry mode'], ['컨벡션 팬', 'True convection fan'], ['센서 쿠킹', 'Sensor cook'], ['Wi-Fi 연결 제어', 'Wi-Fi connected app control'], ['자동 세척', 'Self-clean cycle']],
};
const EXTRA = {
  washer: (r) => ({ drum_capacity_cuft: Math.round((4 + r * 2) * 10) / 10, max_spin_rpm: 1100 + Math.round(r * 5) * 60, wash_programs: 12 + Math.round(r * 8), steam: r > 0.4, water_factor: Math.round((2.5 + r) * 10) / 10 }),
  cooking: (r) => ({ power_w: 900 + Math.round(r * 8) * 100, cavity_cuft: Math.round((1.2 + r * 5) * 10) / 10, burners: r > 0.5 ? 5 : 4, convection: r > 0.3, control_type: r > 0.5 ? '터치' : '다이얼' }),
};

function collectResult(req) {
  const products = [], docs = [], runLog = [], byCat = {};
  for (const it of req.urls) {
    const sub = SUBS.get(it.subcategory) || { label: it.subcategory || '', major: it.category || 'refrigerator' }, major = it.category || sub.major, r = hash(it.model_number);
    if (failing(it)) { runLog.push([`${it.brand} ${it.model_number}`, 'failed', '제품 페이지 구조가 달라 사양을 읽지 못했습니다 (mock).']); continue; }
    const p = { brand: it.brand, model_number: it.model_number, product_name: it.name, product_url: it.url, price_usd: it.price_usd, category: major, subcategory: it.subcategory,
      width_in: Math.round((24 + r * 12) * 10) / 10, height_in: Math.round((34 + r * 36) * 10) / 10, depth_in: Math.round((26 + r * 8) * 10) / 10, weight_lb: Math.round(90 + r * 250),
      finish_color: r > 0.5 ? 'Stainless Steel' : 'Black Stainless', voltage_v: '115 V', frequency_hz: 60, energy_kwh_year: Math.round(300 + r * 400), energy_star: r > 0.35, wifi_supported: r > 0.3 };
    if (major === 'refrigerator') Object.assign(p, { door_style: sub.label, capacity_total_cuft: Math.round((17 + r * 14) * 10) / 10, capacity_fridge_cuft: Math.round((12 + r * 9) * 10) / 10, capacity_freezer_cuft: Math.round((5 + r * 5) * 10) / 10,
      ice_maker: r > 0.2, water_dispenser: r > 0.4, fridge_temp_range_f: '34-44 F', freezer_temp_range_f: '-6 to 8 F' });
    else { p.energy_kwh_year = major === 'cooking' ? null : p.energy_kwh_year; p.extra_specs = EXTRA[major](r); }
    products.push(p);
    (byCat[major] ||= []).push(p);
    docs.push({ brand: it.brand, model_number: it.model_number, doc_type: 'Manual', source_url: it.url + '/manual.pdf', local_path: null, href: null, size_bytes: 1800000, pages: 40 + Math.round(r * 60) });
  }
  const groups = Object.entries(byCat).map(([category, ps]) => ({ category, label_ko: MAJORS.find((m) => m.key === category).label_ko, products: ps,
    pod: { rows: POD[category].map(([ko, en]) => ({ category: '주요 기능', ko, en, present: ps.map((p) => hash(p.model_number + ko) > 0.3), source: ps.map(() => 'web'), wording: ps.map(() => en) })) } }));
  const modes = req.with_modes ? products.filter((p) => p.category === 'refrigerator').flatMap((p) => ['Turbo Cool', 'Vacation Mode', 'Sabbath Mode'].map((n) => ({ brand: p.brand, model_number: p.model_number, mode_name: n, category: 'Cooling', description: `${n} 설명 (mock).`, setting_range: null, how_to_activate: '디스플레이에서 선택', source_doc: 'manual.pdf', source_page: 12 }))) : [];
  return { products, documents: docs, raw_specs: [], modes, pod: { rows: [] }, run_log: runLog, groups };
}

const JOBS = new Map();
function snapshot(j) {
  const t = Date.now() - j.t0, n = j.kind === 'search' ? j.req.brands.length : j.req.urls.length, per = j.kind === 'search' ? 450 : 900;
  if (j.cancelled) return { id: j.id, kind: j.kind, status: 'cancelled', progress: { done: Math.min(n, Math.floor(t / per)), total: n, current: '', items: [] }, log: ['취소됨'], result: j.kind === 'collect' ? collectResult({ urls: [], with_modes: false }) : null, error: null };
  const done = Math.min(n, Math.floor(t / per)), fin = done >= n;
  const labelOf = (i) => (j.kind === 'search' ? j.req.brands[i] : `${j.req.urls[i].brand} ${j.req.urls[i].model_number}`);
  const items = Array.from({ length: n }, (_, i) => ({ label: labelOf(i), status: i < done ? (j.kind === 'collect' && failing(j.req.urls[i]) ? 'failed' : 'done') : i === done ? 'running' : 'pending',
    message: j.kind === 'collect' && failing(j.req.urls[i]) && i < done ? '제품 페이지 구조가 달라 사양을 읽지 못했습니다 (mock).' : '' }));
  const status = fin ? 'done' : 'running';
  if (fin && !j.result) j.result = j.kind === 'search' ? searchResult(j.req) : collectResult(j.req);
  return { id: j.id, kind: j.kind, status, progress: { done, total: n, current: '', items }, log: items.filter((x) => x.status !== 'pending').map((x) => `${x.label}: ${x.status}`), result: fin ? j.result : null, error: null, has_excel: false };  // no fake workbook in mock mode: the download stays hidden
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const realFetch = window.fetch.bind(window);
window.fetch = async (path, opts) => {
  const url = String(path);
  if (!url.startsWith('/api/')) return realFetch(path, opts);
  await new Promise((r) => setTimeout(r, 120));
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (url === '/api/brands') return json(brands());
  if (url === '/api/categories') return json(categories());
  if (url === '/api/meta') return json({ mock: true, max_selected: 12, delay_s: 0, max_search_combos: 120, max_brands: 30 });
  if (url === '/api/regions') return json(REGIONS);
  if (url.startsWith('/api/filters')) return json({ groups: FILTERS() });
  if (url === '/api/search' || url === '/api/collect') {
    const kind = url.endsWith('search') ? 'search' : 'collect';
    const id = Math.random().toString(36).slice(2, 10);
    JOBS.set(id, { id, kind, req: body, t0: Date.now(), result: null, cancelled: false });
    return json({ job_id: id });
  }
  let m = url.match(/^\/api\/jobs\/(\w+)\/cancel$/);
  if (m) { const j = JOBS.get(m[1]); if (j) j.cancelled = true; return json({ ok: true }); }
  m = url.match(/^\/api\/jobs\/(\w+)$/);
  if (m && JOBS.has(m[1])) return json(snapshot(JOBS.get(m[1])));
  return json({ detail: 'not found' }, 404);
};
document.addEventListener('DOMContentLoaded', () => { const f = document.getElementById('mockflag'); if (f) f.hidden = false; });
})();
