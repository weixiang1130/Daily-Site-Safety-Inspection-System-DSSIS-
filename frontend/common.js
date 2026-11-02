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
  return localDate(new Date());
}

/** 本機日期 YYYY-MM-DD。toISOString 是 UTC：台灣早上八點前會差一天。 */
function localDate(d) {
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
  let drawing = false, dirty = false, points = 0;
  const pos = e => {
    const r = canvas.getBoundingClientRect();
    const p = e.touches ? e.touches[0] : e;
    return { x: p.clientX - r.left, y: p.clientY - r.top };
  };
  const start = e => { e.preventDefault(); drawing = true; dirty = true; const p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); };
  const move = e => { if (!drawing) return; e.preventDefault(); const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); points++; };
  const end = () => { drawing = false; };
  canvas.addEventListener('mousedown', start);
  canvas.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  canvas.addEventListener('touchstart', start, { passive: false });
  canvas.addEventListener('touchmove', move, { passive: false });
  canvas.addEventListener('touchend', end);
  return {
    clear() { ctx.clearRect(0, 0, canvas.width, canvas.height); dirty = false; points = 0; },
    isEmpty() { return !dirty; },
    points() { return points; },
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

/** 廠商名稱 → id。找不到就請後端建檔，並更新本地快取。 */
async function resolveVendorId(name) {
  const n = String(name || '').trim();
  if (!n) return null;
  const hit = VENDOR_CACHE.find(v => v.name.trim().toLowerCase() === n.toLowerCase());
  if (hit) return hit.id;

  const v = await API.post('/api/vendors/resolve', { name: n });
  VENDOR_CACHE.push({ id: v.id, code: v.code, name: v.name });
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

/** 作廢單據：問原因後送出，回 true＝已作廢。作廢不是真的刪除：單據要留存，內容留在修改紀錄。 */
async function voidDoc(kind, id, revision) {
  const reason = (prompt('作廢這張單？未結案的缺失會一起移除，需要重新填寫；原內容留在修改紀錄。\n\n請填寫作廢原因：') || '').trim();
  if (!reason) return false;
  try {
    await API.post(`/api/sign/${encodeURIComponent(kind)}/${id}/void`, { revision, reason });
    toast('已作廢');
    return true;
  } catch (err) { toast('作廢失敗：' + err.message); return false; }
}

/** 產出狀態標籤。status 未知時退回原字串，不硬套成「需處理」的紅色。 */
function statusTag(status) {
  const cls = STATUS_CLASS[status] || '';
  return `<span class="tag ${cls}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

/* ---------------------------------------------------------------------------
   缺失處置（可複選）
   一筆缺失常同時有好幾種處置（立即改善＋扣款、限期改善＋代工＋扣款），
   而且二、三十筆缺失各自不同，不能整張表套一種。巡檢表與協議巡視表共用。
   --------------------------------------------------------------------------- */
const DISPOSITION_OPTS = [['onsite', '立即改善'], ['scheduled', '限期改善'],
                          ['proxy', '代工'], ['deduct', '扣款']];

/** 處置勾選區＋期限＋扣款金額。key 是這筆缺失在頁面上的識別字。 */
function dispositionHtml(key) {
  return `<label>處置方式（可複選）</label>
    <div class="disp">${DISPOSITION_OPTS.map(([v, l]) => `<label class="chk">
      <input type="checkbox" id="dp-${v}-${key}" value="${v}"${v === 'onsite' ? ' checked' : ''}
             onchange="syncDisposition('${key}', this)"> ${l}</label>`).join('')}</div>
    <div class="row">
      <div id="duewrap-${key}" class="hidden"><label>改善期限</label><input type="date" id="due-${key}"></div>
      <div id="penwrap-${key}" class="hidden"><label>扣款金額（元，可之後補）</label>
        <input type="number" min="0" step="100" id="pen-${key}" inputmode="numeric"></div>
    </div>`;
}

/** 勾了限期改善或代工才要期限（預設三天後）；勾了扣款才顯示金額。
    立即改善和限期改善、代工互斥（一個已改好、一個還沒）；扣款可以和任何一種並存。
    changed 是使用者剛勾的那格，還原暫存時不傳。 */
function syncDisposition(key, changed) {
  const on = v => document.getElementById(`dp-${v}-${key}`).checked;
  if (changed && changed.checked) {
    const clash = changed.value === 'onsite' ? ['scheduled', 'proxy']
      : ['scheduled', 'proxy'].includes(changed.value) ? ['onsite'] : [];
    clash.forEach(v => { document.getElementById(`dp-${v}-${key}`).checked = false; });
  }
  const later = on('scheduled') || on('proxy');
  document.getElementById('duewrap-' + key).classList.toggle('hidden', !later);
  document.getElementById('penwrap-' + key).classList.toggle('hidden', !on('deduct'));
  const due = document.getElementById('due-' + key);
  if (later && !due.value) {
    const d = new Date(); d.setDate(d.getDate() + 3);
    due.value = localDate(d);
  }
}

/** 讀出處置；沒勾任何一種回 null（呼叫端要擋下）。 */
function readDisposition(key) {
  const list = DISPOSITION_OPTS.map(o => o[0])
    .filter(v => document.getElementById(`dp-${v}-${key}`).checked);
  if (!list.length) return null;
  const later = list.includes('scheduled') || list.includes('proxy');
  const pen = parseFloat(document.getElementById('pen-' + key).value);
  return {
    dispositions: list,
    // 舊版後端只認 action_type；新版以 dispositions 為準
    action_type: later ? 'scheduled' : 'onsite',
    due_date: later ? (document.getElementById('due-' + key).value || null) : null,
    penalty: list.includes('deduct') && pen > 0 ? pen : null,
  };
}

/** 清單／PDF 用的處置文字。 */
function dispositionText(f) {
  const d = f.dispositions && f.dispositions.length ? f.dispositions
    : [f.action_type === 'scheduled' ? 'scheduled' : 'onsite'];
  return DISPOSITION_OPTS.filter(o => d.includes(o[0])).map(([v, l]) =>
    v === 'deduct' && f.penalty ? `${l} ${Number(f.penalty).toLocaleString()} 元` : l).join('、');
}

/* ---------------------------------------------------------------------------
   現場照片：可多張、上傳前壓縮、縮圖預覽、點開放大、刪除
   一筆缺失的改善前／後各可多張。資料庫沿用 photo_before／photo_after 這兩個文字欄，
   多張以換行分隔：舊資料的單一路徑就是只有一張的清單，不必搬資料。
   --------------------------------------------------------------------------- */
const PHOTO_MAX = 10;        // 每欄上限：缺失佐證不需要更多，也避免一次灌爆檔案庫
const PHOTO_EDGE = 1600;     // 長邊像素：看得清現場即可。手機原圖 5～8MB 會超過函式的請求上限
let PHOTO_UPLOADING = 0;     // 上傳中的張數：送出表單前要等，否則照片會漏掉

/** 欄位值 → 路徑清單。字串（換行分隔）或陣列都接受；只收站內路徑，不把任意字串放進 src。 */
function photoList(v) {
  const raw = Array.isArray(v) ? v : String(v || '').split('\n');
  return raw.map(s => String(s).trim()).filter(s => s.startsWith('/api/file/') || s.startsWith('/uploads/'));
}
const photoJoin = v => photoList(v).join('\n') || null;

/** 壓成 JPEG。解不開的格式（部分 HEIC）退回原檔，由後端判斷能不能收。 */
async function shrinkPhoto(file) {
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, PHOTO_EDGE / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * k);
    c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close();
    const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.82));
    if (blob) return new File([blob], 'photo.jpg', { type: 'image/jpeg' });
  } catch (e) { /* 退回原檔 */ }
  return file;
}

/** 放大檢視，左右鍵切換。 */
function photoView(list, at = 0) {
  const paths = photoList(list);
  if (!paths.length) return;
  let i = at;
  const d = document.createElement('dialog');
  d.className = 'photo-view';
  d.innerHTML = `<img alt="現場照片"><div class="pv-bar"><button type="button" class="ghost" data-go="-1">上一張</button>
    <span class="pv-count"></span><button type="button" class="ghost" data-go="1">下一張</button>
    <button type="button" data-close>關閉</button></div>`;
  const show = () => {
    d.querySelector('img').src = paths[i];
    d.querySelector('.pv-count').textContent = `${i + 1} / ${paths.length}`;
    d.querySelectorAll('[data-go]').forEach(b => { b.hidden = paths.length < 2; });
  };
  d.addEventListener('click', e => {
    if (e.target === d || e.target.dataset.close !== undefined) { d.close(); d.remove(); return; }
    if (e.target.dataset.go) { i = (i + Number(e.target.dataset.go) + paths.length) % paths.length; show(); }
  });
  d.addEventListener('keydown', e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      i = (i + (e.key === 'ArrowLeft' ? -1 : 1) + paths.length) % paths.length; show();
    }
  });
  d.addEventListener('close', () => d.remove());
  document.body.appendChild(d);
  show();
  d.showModal();
}

/** 照片欄。容器 id、既有路徑、清單變動時的回呼（存暫存用）。
    同一格重建時：還在上傳、內容也相同（再點一次不合格）就沿用；其他情況取消舊的，
    否則舊閉包傳完後會把照片寫到新的那一筆。關掉視窗不再要這格時呼叫 .abort()。 */
function photoBox(id, initial, onChange) {
  const el = document.getElementById(id);
  const old = el._photoBox;
  if (old) {
    if (old.busy && old.list.join('\n') === photoList(initial).join('\n')) {
      old.onChange = onChange;
      return old;
    }
    old.abort();
  }
  let list = photoList(initial);
  let pending = [];                     // 上傳中：{ url: 本機預覽 }
  let dead = false;

  function render() {
    el.innerHTML = list.map((p, i) => `<div class="pb-thumb">
        <button type="button" class="pb-open" data-open="${i}" title="點開放大"><img src="${esc(p)}" alt="照片 ${i + 1}" loading="lazy"></button>
        <button type="button" class="pb-del" data-del="${i}" title="刪除這張" aria-label="刪除照片 ${i + 1}">✕</button>
      </div>`).join('')
      + pending.map(p => `<div class="pb-thumb pb-busy"><img src="${p.url}" alt=""><span>上傳中</span></div>`).join('')
      + (list.length + pending.length < PHOTO_MAX
        ? `<label class="pb-add">＋ 加入照片<input type="file" accept="image/*" multiple></label>` : '')
      + (list.length ? `<span class="pb-count">${list.length} 張</span>` : '');
    const input = el.querySelector('input[type=file]');
    if (input) input.onchange = () => add([...input.files]);
  }

  el.onclick = e => {
    const open = e.target.closest('[data-open]');
    const del = e.target.closest('[data-del]');
    if (open) photoView(list, Number(open.dataset.open));
    if (del && confirm('刪除這張照片？')) {
      list.splice(Number(del.dataset.del), 1);
      render();
      box.onChange(list.slice());
    }
  };

  async function add(files) {
    const room = PHOTO_MAX - list.length - pending.length;
    if (files.length > room) toast(`每欄最多 ${PHOTO_MAX} 張，這次只收前 ${room} 張`);
    const jobs = files.slice(0, room).map(f => ({ file: f, url: URL.createObjectURL(f) }));
    pending.push(...jobs);
    PHOTO_UPLOADING += jobs.length;
    render();
    const failed = [];
    // 依序傳：工地網路不穩，同時傳一堆容易全部逾時；一張失敗不影響其他張
    for (const job of jobs) {
      if (dead) return;
      let path = null;
      try {
        const fd = new FormData();
        fd.append('file', await shrinkPhoto(job.file));
        path = (await API.form('/api/upload/photo', fd)).path;
      } catch (e) {
        failed.push(job.file.name);
      }
      if (dead) return;
      if (path) list.push(path);
      URL.revokeObjectURL(job.url);
      pending = pending.filter(p => p !== job);
      PHOTO_UPLOADING--;
      render();
      box.onChange(list.slice());
    }
    if (failed.length) toast(`${jobs.length - failed.length} 張成功，${failed.length} 張失敗：${failed.join('、')}`, 5000);
    else if (jobs.length > 1) toast(`已上傳 ${jobs.length} 張`);
  }

  const box = el._photoBox = {
    onChange,
    get list() { return list.slice(); },
    get busy() { return pending.length > 0; },
    set(v) { list = photoList(v); render(); },
    abort() {                           // 上傳中的張數不再算：送出不必等被丟掉的照片
      dead = true;
      PHOTO_UPLOADING -= pending.length;
      pending.forEach(p => URL.revokeObjectURL(p.url));
      pending = [];
    },
  };
  render();
  return box;
}

/* ---------------------------------------------------------------------------
   暫存：填到一半自動存在這台裝置
   現場常被叫走、網路斷、手機鎖屏重整，回來整張表就沒了。表單欄位（有 id 的
   input/select/textarea，不含檔案與簽名）連同頁面自己的狀態每次變動就存；
   重開頁面時問要不要接著填。只存在這台裝置的瀏覽器，送出成功就清掉。
   --------------------------------------------------------------------------- */
const DRAFT_DAYS = 7;

const Draft = {
  _key: name => 'draft:' + name,
  _asking: new Set(),                  // 提示卡還沒回答：這段時間存檔會蓋掉要問的那份暫存

  /** 存目前所有欄位＋extra（頁面自己的狀態，如逐項結果、已上傳照片路徑）。 */
  save(name, extra) {
    const fields = {};
    document.querySelectorAll('input[id], select[id], textarea[id]').forEach(el => {
      if (el.type === 'file' || el.closest('[data-nodraft]')) return;
      fields[el.id] = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
    });
    try {
      localStorage.setItem(this._key(name), JSON.stringify({ at: Date.now(), fields, extra }));
      return true;
    } catch (e) { return false; }      // 無痕模式或容量滿：不擋填寫
  },

  load(name) {
    try {
      const d = JSON.parse(localStorage.getItem(this._key(name)) || 'null');
      if (!d || Date.now() - d.at > DRAFT_DAYS * 864e5) return null;
      return d;
    } catch (e) { return null; }
  },

  clear(name) {
    try { localStorage.removeItem(this._key(name)); } catch (e) { /* 同上 */ }
  },

  /** 把存下的欄位值寫回（找不到的 id 略過）。 */
  apply(fields) {
    Object.entries(fields || {}).forEach(([id, v]) => {
      const el = document.getElementById(id);
      if (!el || el.type === 'file') return;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = !!v;
      else el.value = v;
    });
  },

  /** 有暫存就在 anchor 前面插一張卡問「接著填／捨棄」。 */
  offer(name, anchor, onResume, onDrop) {
    const d = this.load(name);
    if (!d) return;
    this._asking.add(name);
    const t = new Date(d.at);
    const when = `${t.getMonth() + 1}/${t.getDate()} ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    const card = document.createElement('div');
    card.className = 'card draft-offer';
    card.innerHTML = `<p>這台裝置有一份 <b>${when}</b> 未送出的暫存（簽名不會暫存，需重簽）。</p>
      <button type="button" data-a="resume">接著填</button>
      <button type="button" class="ghost" data-a="drop">捨棄</button>`;
    card.onclick = async e => {
      const a = e.target.dataset && e.target.dataset.a;
      if (!a) return;
      card.remove();
      this._asking.delete(name);
      if (a === 'drop') {
        this.clear(name);
        if (onDrop) onDrop();
        document.dispatchEvent(new Event('change'));   // 回答之前改的內容現在才存
      } else await onResume(d);
    };
    anchor.parentNode.insertBefore(card, anchor);
  },

  /** 監聽輸入，0.6 秒沒動作就存一次；回傳立即存的函式給頁面在狀態變動時呼叫。
   *  送出成功要先呼叫回傳值的 .stop()：否則送出前一刻排定的那次存檔會在清掉
   *  暫存之後才執行，把已送出的表又存回來。 */
  watch(name, getExtra, statusEl) {
    let timer = null, stopped = false;
    const now = () => {
      clearTimeout(timer);
      if (stopped || Draft._asking.has(name)) return;
      if (Draft.save(name, getExtra()) && statusEl) {
        statusEl.textContent = `已暫存 ${new Date().toTimeString().slice(0, 5)}`;
      }
    };
    const later = () => { clearTimeout(timer); timer = setTimeout(now, 600); };
    document.addEventListener('input', later);
    document.addEventListener('change', later);
    now.stop = () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener('input', later);
      document.removeEventListener('change', later);
    };
    return now;
  },
};

/* ---------------------------------------------------------------------------
   廠商預帶：LINE 出工回報裡這家廠商最近一次的工種與作業主管
   選了廠商就帶出工種、責任人，欄位已有內容時不覆蓋（現場改過的優先）。
   --------------------------------------------------------------------------- */
let VENDOR_HINTS = [];

const _vkey = s => String(s || '').replace(/[\s()（）]/g, '').toLowerCase();

/** 載入工地的預帶資料。 */
async function loadVendorHints(siteId) {
  try { VENDOR_HINTS = await API.get('/api/vendors/hints?site_id=' + encodeURIComponent(siteId)); }
  catch (e) { VENDOR_HINTS = []; }     // 舊版後端沒有這個端點：只是不預帶
}

/** 廠商名稱 → 預帶資料。完全相同優先，其次互相包含（「甲工程 鋼筋綁紮」對「甲工程」）。 */
function vendorHint(name) {
  const k = _vkey(name);
  if (k.length < 2) return null;
  return VENDOR_HINTS.find(h => _vkey(h.vendor) === k)
    || VENDOR_HINTS.find(h => { const hk = _vkey(h.vendor); return hk.length >= 2 && (hk.includes(k) || k.includes(hk)); })
    || null;
}

/** 廠商欄位離開時預帶：targets = { trade: 欄位, person: 欄位 }，空的才填。 */
function bindVendorPrefill(vendorEl, targets) {
  vendorEl.addEventListener('change', () => {
    const h = vendorHint(vendorEl.value);
    const put = (el, v, why) => {
      if (el && v && !el.value.trim()) {
        el.value = v;
        el.title = why;
        el.dispatchEvent(new Event('input', { bubbles: true }));   // 觸發暫存
      }
    };
    const said = h && h.supervisor && optionCanon('person', h.supervisor);
    const person = said || vendorPerson(vendorEl.value);
    // 系統帶入的值送出時要放行，不能自己擋自己
    if (person) COMBO_EXTRA.person.add(person.trim().toLowerCase());
    const by = h ? `依 ${h.report_date} 出工回報預帶，可修改` : '依過去紀錄預帶，請確認';
    if (h) put(targets.trade, h.trade, by);
    put(targets.person, person, said ? by : '依過去紀錄預帶，請確認');
  });
}

/* ---------------------------------------------------------------------------
   可搜尋下拉（預設清單）
   輸入框標 data-combo="欄位"，點進去就列出這個工地的選項，打字即時篩選；找不到
   就按最下方「＋ 新增選項」，存進清單，同工地的人都看得到。每列有 ✎ 改正、✕ 刪除，
   現場就能維護（比照點工系統的名單池）。有選廠商時，同一列的職種／人員會把這家
   廠商用過的排在最前面（data-combo-vendor="廠商欄的 id"）。

   名單是候選清單，單據存的是字串：刪除、改正都不動已送出的單（送出即已簽核存查）。

   全頁共用一個選單、事件掛在 document：表單的列是動態長的，暫存還原也會重建，
   逐欄綁定很容易漏；也不用為了定位多包一層，版面不受影響。
   --------------------------------------------------------------------------- */
let FORM_OPTIONS = {}, FORM_OPTIONS_SITE = null, FORM_OPTIONS_OK = false;
const COMBO_MAX = 60;
const COMBO_FIELD_LABEL = { vendor: '廠商', trade: '職種', person: '人員', work_item: '作業項目', content: '作業內容' };
const PERSON_SEPARATOR = /[\/／、,，;；\s]/;       // 與後端相同：一格一個人
/** 不在清單但合法的值（今日 LINE 回報帶入的），送出檢查時放行。 */
const COMBO_EXTRA = { vendor: new Set(), person: new Set() };

/** 載入工地的預設清單。 */
async function loadFormOptions(siteId) {
  FORM_OPTIONS_SITE = siteId;
  try {
    FORM_OPTIONS = await API.get('/api/form-options?site_id=' + encodeURIComponent(siteId));
    FORM_OPTIONS_OK = true;
  } catch (e) {                        // 地端或舊版後端沒有這個端點：退回廠商主檔、不檢查名單
    FORM_OPTIONS = {};
    FORM_OPTIONS_OK = false;
  }
}

/** 被改正過的值換成新值（LINE 回報裡的舊寫法）。 */
function optionCanon(field, value) {
  const map = (FORM_OPTIONS.renames || {})[field] || {};
  return map[value] || value;
}

/* 以下規則與後端 form-options.ts 相同（cleanVendor、reportVendor），改一邊要同步改另一邊。 */
const WORK_SUFFIX = /鋼承板|續接器|輕隔間|連續壁|電梯井|綁紮|模板|模扳|鷹架|放樣|粉光|防火|壓送|植釘|地改|水電|帷幕|配管|吊裝|泥作|油漆|防水|機電|鋼筋|鋼構|監控|移工|點工|泰工/;
const WORK_TERM = /^(鋼承板|續接器鎖接|續接器|輕隔間|連續壁|鋼筋綁紮|模板|鷹架|放樣|粉光|防火批覆|壓送|植釘|地改|未標示廠商)$/;
const WORK_PREFIX = /^(放樣|測量|泥作|油漆)(?=\S{2})/;
const COMPANY_TAIL = /(公司|工程行|企業社?|實業|工業)$/;
const AREA_PREFIX = /^(住宅|商辦|辦公|[A-Za-z][棟動])(區|棟)?/;

/** 回報廠商欄 → {廠商, 順帶寫在後面的工項}：「某某連續壁」→ 某某＋連續壁。
    後段要是工項才拆：「某某鋼鐵」是另一家。不像廠商名回 vendor=null。 */
function cleanVendor(raw) {
  const s = String(raw ?? '')
    .replace(/[（(][^）)]*[）)]/g, ' ')
    .replace(/\s*\d+\s*[人名工].*$/, '')
    .replace(/^[\s.。．、,，+＋\-]+/, '')
    .replace(AREA_PREFIX, '')
    .trim();
  const m = /^(\S+)\s+(.+)$/.exec(s);
  let vendor = m ? m[1] : s;
  let work = m ? m[2].trim() : null;
  if (WORK_TERM.test(vendor)) {
    if (!work) return { vendor: null, work: vendor };
    [vendor, work] = [work, vendor];
  }
  const pre = WORK_PREFIX.exec(vendor);
  if (pre && !work) { work = pre[1]; vendor = vendor.slice(pre[1].length); }
  const cut = COMPANY_TAIL.test(vendor) ? -1 : vendor.slice(2).search(WORK_SUFFIX);
  if (cut >= 0) {
    work = [vendor.slice(cut + 2).replace('模扳', '模板'), work].filter(Boolean).join(' ');
    vendor = vendor.slice(0, cut + 2);
  }
  if (vendor.length < 2 || vendor.length > 8 || /\d|[A-Za-z]區|區$/.test(vendor)
      || AREA_PREFIX.test(vendor) || /作業主管|勞工|劳工|工人|更正|修正/.test(s)) {
    return { vendor: null, work: null };
  }
  return { vendor, work };
}

/** 回報一列 → 廠商、工項、工種。廠商欄填工項時，廠商常寫在工種欄（兩欄填反）：
    工種欄只有一個沒寫人數、也不像工種的名稱才敢認定。 */
function reportVendor(raw, tradeSummary) {
  const { vendor, work } = cleanVendor(raw);
  const parts = String(tradeSummary ?? '').split(/[、，,；;+＋\n]/).map(p => p.trim())
    .map(p => ({ name: p.replace(/[\s*＊xX×]*\d+\s*[人名工]?$/, '').trim(), p }))
    .filter(x => x.name.length >= 2 && x.name.length <= 10 && !/\d|\*/.test(x.name));
  const swapped = !vendor && work && parts.length === 1 && parts[0].name === parts[0].p
    && !WORK_SUFFIX.test(parts[0].name) && !/[工班]$/.test(parts[0].name);
  return swapped
    ? { vendor: parts[0].name, work, trade: null }
    : { vendor, work, trade: tradeSummary == null ? null : String(tradeSummary) };
}

/** LINE 回報的廠商對到清單，帶進表單才不會多一種寫法。清單上有這個寫法就照清單（現場親手加的）。 */
function listVendor(raw) {
  const v = optionCanon('vendor', String(raw || '').trim());
  if ((FORM_OPTIONS.vendor || []).some(o => o.value === v)) return { vendor: v, work: '' };
  const c = cleanVendor(v);
  return c.vendor ? { vendor: optionCanon('vendor', c.vendor), work: c.work || '' } : { vendor: v, work: '' };
}

/** 工種摘要 → 只留名稱：「電班8、水班1」→「電班、水班」。與後端 splitTrades 同規則，
    「B1F2」這種名稱裡的數字不能刪。 */
function tradeNames(raw) {
  return String(raw || '').split(/[、，,；;+＋\n]/)
    .map(p => p.trim().replace(/[\s*＊xX×]*\d+\s*[人名工]?$/, '').trim())
    .filter(n => n.length >= 2 && n.length <= 10 && !/\d|\*/.test(n)).join('、');
}

/** 這家廠商平常的人員（LINE 當天沒寫主管時預帶）。沒有回空字串。 */
function vendorPerson(vendor) {
  const map = FORM_OPTIONS.vendor_person || {};
  const k = _vkey(optionCanon('vendor', vendor));
  const hit = Object.keys(map).find(v => _vkey(v) === k);
  return hit ? map[hit] : '';
}

/** 送出檢查：這些欄位的值要來自名單，否則同一家廠商會有五種寫法、報表分組全錯。
    回第一個不合格的 {el, msg}。清單沒載入時不檢查（不能因此送不出去）。 */
function comboCheck(fields) {
  if (!FORM_OPTIONS_OK) return null;
  for (const field of fields) {
    if (!(FORM_OPTIONS[field] || []).length) continue;   // 清單還是空的：無從選起，第一筆可以直接打
    const known = new Set([...(FORM_OPTIONS[field] || []).map(o => o.value.trim().toLowerCase()),
                           ...(COMBO_EXTRA[field] || [])]);
    for (const el of document.querySelectorAll(`input[data-combo="${field}"]`)) {
      const v = el.value.trim();
      // 隱藏的欄位不會送出（改回合格的項目），擋下來使用者也找不到
      if (!v || known.has(v.toLowerCase()) || el.closest('.hidden')) continue;
      return { el, msg: `「${v}」不在${COMBO_FIELD_LABEL[field]}清單中，請從搜尋結果選取或點「＋ 新增選項」加入` };
    }
  }
  return null;
}

const Combo = {
  menu: null, input: null, items: [], active: -1, picking: false,

  /** 這個欄位的選項：這家廠商用過的在前。 */
  source(input) {
    const field = input.dataset.combo;
    let list = (FORM_OPTIONS[field] || []).map(o => Object.assign({ removable: true }, o));
    if (field === 'work_item') {     // 作業項目寫得少的工地，用作業內容補
      const have = new Set(list.map(o => o.value));
      list = list.concat((FORM_OPTIONS.content || []).filter(o => !have.has(o.value))
        .map(o => Object.assign({ removable: false }, o)));
    }
    if (field === 'vendor' && !FORM_OPTIONS_OK) {   // 清單不可用時才退回廠商主檔；平常名單各工地獨立
      const have = new Set(list.map(o => _vkey(o.value)));
      list = list.concat(VENDOR_CACHE.filter(v => !have.has(_vkey(v.name)))
        .map(v => ({ value: v.name, n: 0, vendors: [], removable: false })));
    }
    const vEl = input.dataset.comboVendor && document.getElementById(input.dataset.comboVendor);
    const vendor = vEl ? _vkey(vEl.value) : '';
    const mine = o => vendor && (o.vendors || []).some(x => _vkey(x) === vendor);
    if (vendor) {
      list = list.filter(mine).map(o => Object.assign({ mine: true }, o))
        .concat(list.filter(o => !mine(o)));
    }
    return list;
  },

  /** 空白分詞，每個詞都要出現。 */
  filter(list, text) {
    const words = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return list;
    const hits = list.filter(o => {
      const v = o.value.toLowerCase();
      return words.every(w => v.includes(w));
    });
    // 開頭符合的排前面；sort 穩定，同組仍維持這家廠商優先、次數多的在前
    const head = o => (o.value.toLowerCase().startsWith(words[0]) ? 0 : 1);
    return hits.sort((a, b) => head(a) - head(b));
  },

  open(input) {
    if (!this.menu) {
      this.menu = document.createElement('div');
      this.menu.className = 'combo-menu hidden';
      // 按選單不能讓輸入框失焦，否則 focusout 先把選單關掉，點擊就落空
      this.menu.addEventListener('mousedown', e => e.preventDefault());
      this.menu.addEventListener('click', e => this.onClick(e));
      document.body.appendChild(this.menu);
    }
    this.input = input;
    this.render();
  },

  close() {
    if (this.menu) this.menu.classList.add('hidden');
    this.input = null;
    this.active = -1;
  },

  render() {
    const input = this.input;
    const text = input.value;
    const all = this.source(input);
    // 欄位已有內容且正好等於某個選項（剛選好、或帶入的）：列全部方便改選，
    // 這個選項排第一，方向鍵＋Enter 才不會換成別家
    const exact = all.find(o => o.value === text.trim());
    const hits = exact ? [exact, ...all.filter(o => o !== exact)] : this.filter(all, text);
    const shown = hits.slice(0, COMBO_MAX);
    this.items = shown;
    this.active = -1;
    const t = text.trim();
    let html = '', mineHead = false, restHead = false;
    shown.forEach((o, i) => {
      if (o.mine && !mineHead) { html += '<div class="combo-head">這家廠商用過</div>'; mineHead = true; }
      if (!o.mine && mineHead && !restHead) { html += '<div class="combo-head">其他</div>'; restHead = true; }
      const x = o.removable ? `<span class="combo-acts">
        <button type="button" class="combo-x" data-ren="${i}" title="改正：修錯字。名單換成新名字，之後的 LINE 回報也跟著換；已送出的單不動">✎</button>
        <button type="button" class="combo-x" data-hide="${i}" title="刪除：不再出現在清單。已送出的單不動">✕</button></span>` : '';
      html += `<div class="combo-item" data-i="${i}"><span>${esc(o.value)}</span>${x}</div>`;
    });
    if (hits.length > shown.length) html += `<div class="combo-head">還有 ${hits.length - shown.length} 項，請打字縮小範圍</div>`;
    if (!shown.length && !t) html += '<div class="combo-head">清單還是空的，直接輸入即可</div>';
    if (t && !all.some(o => o.value.toLowerCase() === t.toLowerCase())) {
      html += `<div class="combo-item combo-add" data-add="1">＋ 新增選項：「${esc(t)}」</div>`;
    }
    this.menu.innerHTML = html;
    this.menu.classList.toggle('hidden', !html);
    this.place();
  },

  place() {
    const r = this.input.getBoundingClientRect();
    this.menu.style.left = (r.left + window.scrollX) + 'px';
    this.menu.style.top = (r.bottom + window.scrollY + 2) + 'px';
    this.menu.style.minWidth = r.width + 'px';
  },

  pick(value) {
    const input = this.input;
    input.value = value;
    this.close();
    // 廠商欄的預帶、暫存都靠這兩個事件。協議表選完會重排列並把焦點放回來，不能因此又打開
    this.picking = true;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    this.picking = false;
  },

  onClick(e) {
    if (!this.input) return;
    const hide = e.target.closest('[data-hide]');
    if (hide) { this.hide(this.items[+hide.dataset.hide]); return; }
    const ren = e.target.closest('[data-ren]');
    if (ren) { this.rename(this.items[+ren.dataset.ren]); return; }
    const add = e.target.closest('[data-add]');
    if (add) { this.add(this.input.value.trim()); return; }
    const row = e.target.closest('[data-i]');
    if (row) this.pick(this.items[+row.dataset.i].value);
  },

  async add(value) {
    const input = this.input;
    const field = input.dataset.combo;
    if (field === 'person' && PERSON_SEPARATOR.test(value)) {
      toast('一格只能填一個人名，請分開新增');
      return;
    }
    const vEl = input.dataset.comboVendor && document.getElementById(input.dataset.comboVendor);
    const vendor = vEl ? vEl.value.trim() : '';
    // 先加進本地並選取，不打斷填表；伺服器回來用完整清單覆蓋
    const local = { value, n: 1, vendors: vendor ? [vendor] : [] };
    (FORM_OPTIONS[field] = FORM_OPTIONS[field] || []).unshift(local);
    this.pick(value);
    try {
      const r = await API.post('/api/form-options', { site_id: FORM_OPTIONS_SITE, field, value, vendor });
      FORM_OPTIONS = r.options;
      if (r.value && r.value !== value) input.value = r.value;   // 只差大小寫：用清單既有的寫法
      toast('已新增至本工地共用清單');
    } catch (e) {
      // 本地不能留著：表單會通過檢查，下次重新整理清單裡卻沒有這個值
      FORM_OPTIONS[field] = (FORM_OPTIONS[field] || []).filter(o => o !== local);
      toast('新增失敗，請再試一次：' + e.message);
    }
  },

  async rename(o) {
    const input = this.input;
    const field = input.dataset.combo;
    const label = COMBO_FIELD_LABEL[field];
    const to = (prompt(`把${label}「${o.value}」改成：\n（修錯字用。與清單既有項目相同時會合併。）`, o.value) || '').trim();
    if (!to || to === o.value) return;
    if (field === 'person' && PERSON_SEPARATOR.test(to)) { toast('一格只能填一個人名'); return; }
    if (!confirm(`「${o.value}」→「${to}」\n\n・本工地清單換成新名字，之後的 LINE 回報出現舊寫法也會換成新的\n`
                 + '・已送出的單據維持原樣（已簽核存查）\n・這個動作無法自動復原')) return;
    try {
      const r = await API.post('/api/form-options/rename',
                               { site_id: FORM_OPTIONS_SITE, field, from: o.value, to });
      FORM_OPTIONS = r.options;
      // 表單上正選著舊值的欄位直接換成新值
      document.querySelectorAll(`input[data-combo="${field}"]`).forEach(el => {
        if (el.value.trim() === o.value) {
          el.value = to;
          el.dispatchEvent(new Event('input', { bubbles: true }));
        }
      });
      toast('已改正');
      if (this.input) this.render();
    } catch (e) { toast('改正失敗：' + e.message); }
  },

  async hide(o) {
    const field = this.input.dataset.combo;
    if (!confirm(`刪除「${o.value}」？\n\n若不是錯字，之後新單就選不到了（需要時可再新增）。\n已送出的單據不受影響。`)) return;
    try {
      const r = await API.post('/api/form-options/hide', { site_id: FORM_OPTIONS_SITE, field, value: o.value });
      FORM_OPTIONS = r.options;
      if (this.input) this.render();
    } catch (e) { toast('刪除失敗：' + e.message); }
  },

  keydown(e) {
    if (e.key === 'Escape') { this.close(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!this.items.length) return;
      e.preventDefault();
      const n = this.items.length;
      this.active = (this.active + (e.key === 'ArrowDown' ? 1 : n - 1 + (this.active < 0 ? 1 : 0))) % n;
      this.menu.querySelectorAll('[data-i]').forEach((el, i) => {
        el.classList.toggle('on', i === this.active);
        if (i === this.active) el.scrollIntoView({ block: 'nearest' });
      });
    } else if (e.key === 'Enter') {     // 沒用方向鍵選過就是維持原值，不能換成第一筆
      e.preventDefault();
      if (this.active >= 0) this.pick(this.items[this.active].value);
      else this.close();
    }
  },
};

document.addEventListener('focusin', e => {
  if (!Combo.picking && e.target.matches && e.target.matches('input[data-combo]')) Combo.open(e.target);
});
document.addEventListener('click', e => {           // Esc 收起後再點同一格要能打開
  if (!Combo.input && e.target.matches && e.target.matches('input[data-combo]')) Combo.open(e.target);
});
document.addEventListener('input', e => {
  if (Combo.input && e.target === Combo.input) Combo.render();
  // Esc 收起或選完之後繼續打字要能打開；程式帶值（別的欄位）不算
  else if (!Combo.input && !Combo.picking && e.target === document.activeElement
           && e.target.matches('input[data-combo]')) Combo.open(e.target);
});
document.addEventListener('keydown', e => {
  if (Combo.input && e.target === Combo.input) Combo.keydown(e);
});
document.addEventListener('focusout', e => {
  if (Combo.input && e.target === Combo.input) Combo.close();
});
window.addEventListener('resize', () => {          // 手機鍵盤彈出也算 resize，不能關掉
  if (Combo.input) Combo.place();
});
