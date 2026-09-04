// 共用工具

// ---------------------------------------------------------------------------
// 品牌識別
// 公司名稱由後端 /api/branding 提供（來自部署環境的環境變數），
// 不寫死在前端，因此公開的程式碼中不含任何真實公司名稱。
// ---------------------------------------------------------------------------
let BRANDING = null;

/** 取品牌設定。**失敗不快取**——快取一個空物件等於把暫時的連線問題
    變成永久的：之後每次呼叫都拿到那份空的，重試永遠不會成功。
    回 null 讓呼叫端能分辨「拿不到」與「拿到但沒設定」。 */
async function loadBranding() {
  if (BRANDING) return BRANDING;
  try {
    const r = await fetch('/api/branding', { credentials: 'same-origin' });
    if (!r.ok) return null;
    BRANDING = await r.json();
  } catch (e) {
    return null;
  }
  return BRANDING;
}

/** 在 #brand 容器渲染版頭品牌標記。 */
async function renderBrand(subtitleKey = 'system_name') {
  // 取不到就當作沒有設定：版頭少幾個字沒關係，整頁因為讀不到品牌而
  // 掛掉才是問題（loadBranding 失敗會回 null）。
  const b = (await loadBranding()) || {};
  const el = document.getElementById('brand');
  if (!el) return b;
  const org = b.org_short || b.org_name || '';
  el.innerHTML = `
    <span class="mark"></span>
    <span class="name">${esc(b[subtitleKey] || '')}</span>
    ${org ? `<span class="sub">${esc(org)}</span>` : ''}`;
  if (b.system_name) {
    const t = document.querySelector('title');
    if (t && t.dataset.suffix !== 'done') {
      t.textContent = `${t.textContent}｜${org || b.system_name}`;
      t.dataset.suffix = 'done';
    }
  }
  return b;   // 呼叫端有時需要品牌設定本身（例如戰情室的主場站代碼）
}

// 建築框景：施工架立面線稿，作為版面右下角的框景元素。
// 以 inline SVG 注入，才能繼承 currentColor 隨深淺色主題變化。
const ARCH_FRAME_SVG = `
<svg class="arch-frame" viewBox="0 0 420 300" fill="none" stroke="currentColor"
     stroke-width="1" aria-hidden="true" style="color:var(--border-strong)">
  <g opacity="0.55">
    <path d="M60 300V70M130 300V50M200 300V30M270 300V50M340 300V70"/>
    <path d="M60 110h280M60 160h280M60 210h280M60 260h280"/>
    <path d="M60 70L130 50L200 30L270 50L340 70" stroke-width="1.5"/>
  </g>
  <g opacity="0.32">
    <path d="M60 110L130 160M130 110L60 160M200 110L270 160M270 110L200 160"/>
    <path d="M130 210L200 260M200 210L130 260"/>
  </g>
  <path d="M20 300h380" stroke-width="1.5" opacity="0.7"/>
</svg>`;

function injectArchFrame() {
  if (document.querySelector('.arch-frame')) return;
  document.body.insertAdjacentHTML('beforeend', ARCH_FRAME_SVG);
}

/** 未登入時導向登入頁，並帶上原本要去的頁面，登入後可直接回來。 */
function gotoLogin() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.href = `/static/index.html?next=${next}`;
}

const API = {
  async get(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (r.status === 401) { gotoLogin(); throw new Error('未登入'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (r.status === 401) { gotoLogin(); throw new Error('未登入'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  },
  async form(url, formData) {
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin', body: formData });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }
};

function toast(msg, ms = 2600) {
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), ms);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// 手寫簽名板
function initSignaturePad(canvas) {
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  function resize() {
    const r = canvas.getBoundingClientRect();
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    ctx.scale(dpr, dpr);
    ctx.lineWidth = 2.2;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#0b1f38';
  }
  resize();
  let drawing = false, dirty = false;
  const pos = e => {
    const r = canvas.getBoundingClientRect();
    const p = e.touches ? e.touches[0] : e;
    return { x: p.clientX - r.left, y: p.clientY - r.top };
  };
  const start = e => { e.preventDefault(); drawing = true; dirty = true; const p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); };
  const move = e => { if (!drawing) return; e.preventDefault(); const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); };
  const end = () => { drawing = false; };
  canvas.addEventListener('mousedown', start);
  canvas.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  canvas.addEventListener('touchstart', start, { passive: false });
  canvas.addEventListener('touchmove', move, { passive: false });
  canvas.addEventListener('touchend', end);
  return {
    clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); dirty = false; },
    isEmpty() { return !dirty; },
    toDataURL() { return canvas.toDataURL('image/png'); }
  };
}

/**
 * 產生工地下拉選單的內容，依事業處分組。
 * 公司有四十多個工地，不分組的話現場很難在選單裡找到自己的工地。
 * @param {Array} sites  /api/sites 回傳的工地陣列
 * @param {number|null} selectedId  預選的工地 id
 * @param {string|null} allLabel  若提供，最前面加一個「全部」選項
 */
function siteOptions(sites, selectedId = null, allLabel = null) {
  const groups = new Map();
  for (const s of sites) {
    const d = s.department || '其他';
    if (!groups.has(d)) groups.set(d, []);
    groups.get(d).push(s);
  }
  const opt = s =>
    `<option value="${s.id}"${s.id === selectedId ? ' selected' : ''}>${esc(s.name)}</option>`;

  let html = allLabel ? `<option value="">${esc(allLabel)}</option>` : '';
  // 只有一個分組時不必顯示群組標題
  if (groups.size <= 1) {
    html += sites.map(opt).join('');
  } else {
    for (const [dept, rows] of groups) {
      html += `<optgroup label="${esc(dept)}">${rows.map(opt).join('')}</optgroup>`;
    }
  }
  return html;
}

/* ---------------------------------------------------------------------------
   填報工地與棟別
   主場站領有兩張建照，但實際上是同一塊工地、同一批人在管，因此填報
   不拆成兩個工地，改以棟別區分。儀表板的缺失統計不分棟、不分工地，
   一律以填報資料整體計算；棟別只用來回答「這筆缺失在哪一棟」。
   名稱是通用詞（建物用途），不涉及任何公司識別。
   --------------------------------------------------------------------------- */

/**
 * 填報頁用的工地清單：設定主場站後只列主場站——目前全公司的填報都
 * 以它為準，掛在其他工地的帳號填報也一律記在主場站名下（刻意如此）。
 * 只有「填報選單」走這條；缺失清單／儀表板的瀏覽篩選與匯入工具
 * 仍用 /api/sites 的完整清單，在後端過濾會弄壞那些消費者。
 * 代碼對不上時退回完整清單——寧可多列，不能讓現場選不到工地而無法填報。
 */
function fillableSites(sites, brand) {
  const code = String((brand && brand.primary_site_code) || '').trim();
  const primary = sites.filter(s => s.code === code);
  return primary.length ? primary : sites;
}

// 棟別的預設選項。正式的清單由部署設定 BUILDING_LABELS 經
// /api/branding 的 buildings 下發（見 buildingList），這裡只是
// 沒有設定時的退路，讓開發與示範環境不用配置也能填。
const BUILDINGS = ['辦公棟', '住宅棟'];

/** 棟別選項清單：branding 有給就用它（單一來源），否則退回預設。 */
function buildingList(brand) {
  const fromBrand = (brand && Array.isArray(brand.buildings))
    ? brand.buildings.filter(Boolean) : [];
  return fromBrand.length ? fromBrand : BUILDINGS;
}

/**
 * 棟別下拉的選項。沒有記住的棟別時放一個不可選的佔位選項——
 * 瀏覽器對 select 預設選第一項，直接放實際棟別會讓沒注意到這一欄的
 * 首次填報者整張表（連同每筆缺失與存查 PDF）靜默記在錯的棟名下。
 */
function buildingOptions(list, selected = null) {
  const head = selected ? ''
    : '<option value="" disabled selected>請選擇棟別</option>';
  return head + list.map(b =>
    `<option value="${esc(b)}"${b === selected ? ' selected' : ''}>${esc(b)}</option>`).join('');
}

/** 同一個人通常連續多天填同一棟，記住上次選的棟別省去每天重選。 */
function recallBuilding(list) {
  try {
    const v = localStorage.getItem('lastBuilding');
    return list.includes(v) ? v : null;   // 選項改名後，舊值直接作廢
  } catch (e) { return null; }            // 無痕模式沒有 localStorage
}

function rememberBuilding(value) {
  try { localStorage.setItem('lastBuilding', value); } catch (e) { /* 同上 */ }
}

/* ---------------------------------------------------------------------------
   廠商輸入
   協力商在各工地差異很大、且會隨工程階段更換，無法由管理員預先建好完整清單。
   因此填報時用可輸入的 datalist（既能從既有廠商挑選，也能直接打新名稱），
   送出前再把名稱換成廠商 id —— 沒有的會自動建檔，
   這樣儀表板的「廠商缺失排行」才不會漏掉現場臨時新增的廠商。
   --------------------------------------------------------------------------- */
let VENDOR_CACHE = [];

/** 建立共用的 <datalist>，供所有廠商輸入欄位參照。 */
function installVendorDatalist(vendors) {
  VENDOR_CACHE = vendors.slice();
  let dl = document.getElementById('vendorList');
  if (!dl) {
    dl = document.createElement('datalist');
    dl.id = 'vendorList';
    document.body.appendChild(dl);
  }
  dl.innerHTML = vendors.map(v => `<option value="${esc(v.name)}">`).join('');
}

/** 廠商名稱 → id。找不到就請後端建檔，並更新本地快取與 datalist。 */
async function resolveVendorId(name) {
  const n = String(name || '').trim();
  if (!n) return null;
  const hit = VENDOR_CACHE.find(v => v.name.trim().toLowerCase() === n.toLowerCase());
  if (hit) return hit.id;

  const v = await API.post('/api/vendors/resolve', { name: n });
  VENDOR_CACHE.push({ id: v.id, code: v.code, name: v.name });
  installVendorDatalist(VENDOR_CACHE);
  return v.id;
}

const HAZARDS = [
  ['FALL', '墜落'], ['ELEC', '感電'], ['COLLAPSE', '倒塌崩塌'],
  ['FALLING_OBJ', '物體飛落'], ['COLLISION', '衝撞'], ['CAUGHT', '被夾被捲'],
  ['PUNCTURE', '穿刺'], ['FIRE', '火災'], ['CONFINED', '局限空間'],
  ['MACHINE', '危險機械吊掛'], ['PPE', '門禁與防護具'], ['ENV', '環境整潔'],
  ['GENERAL', '一般管理'], ['OTHER', '其他']
];

/**
 * 必填姓名欄位的共用檢查與記憶。
 *
 * 現場共用同一組帳號，這個名字是唯一能回答「這張表是誰填的」的東西，
 * 兩張表單的規則必須一致——先前各寫一份，很快就在 trim、提示方式與是否
 * 記憶上分岔了。
 *
 * 記憶用的鍵帶角色：檢查人員與紀錄人員常常不是同一個人，共用一個鍵會把
 * 別人的名字預先填進去，而那個名字會一路進到簽核存查的 PDF 裡。
 */
function requireName(el, message) {
  const v = (el.value || '').trim();
  if (!v) { toast(message); el.focus(); return null; }
  el.value = v;                       // 一併正規化，送出的就是修過的值
  return v;
}

function recallName(role) {
  try { return localStorage.getItem('lastName:' + role) || ''; }
  catch (e) { return ''; }            // 無痕模式沒有 localStorage
}

function rememberName(role, value) {
  try { localStorage.setItem('lastName:' + role, value); } catch (e) { /* 同上 */ }
}

const STATUS_LABEL = { open: '改善中', fixed: '待複驗', verified: '已複驗', closed: '已結案' };

// 狀態對應的標籤樣式。先前各頁自己寫三元判斷，結果都漏掉 verified，
// 「已複驗」會落到 .tag.open 的紅底而顯示成需要處理——訊號剛好相反。
// 標籤文字已經集中在上面，顏色也集中在這裡，兩者才不會再各自漂移。
const STATUS_CLASS = { open: 'open', fixed: 'fixed', verified: 'verified', closed: 'closed' };

/** 產出狀態標籤。status 未知時退回原字串，不硬套成「需處理」的紅色。 */
function statusTag(status) {
  const cls = STATUS_CLASS[status] || '';
  return `<span class="tag ${cls}">${esc(STATUS_LABEL[status] || status)}</span>`;
}
