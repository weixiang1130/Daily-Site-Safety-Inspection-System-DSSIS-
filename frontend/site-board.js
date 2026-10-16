// 工地看板主邏輯。資料來源：
//   /api/board-sites          工地清單（選單）
//   /api/board-data/{id}      整頁資料（每分鐘輪詢一次）
// 區塊：作業循環（現在做什麼＋時間軸）、今日作業危害（附法定應辦事項）、
// 環境監測與法定作為、本日出工、缺失改善提醒、緊急連絡人、安全佈告跑馬燈；
// 無災害天數在標題列，溫濕度等參考讀數在工具列。
// 依賴 common.js（esc）、board-core.js（日期、時段、SLOT_COLORS/slotType）。

const POLL_MS = 60000;        // 資料輪詢
const PAGE_MS = 10000;        // 聯絡人／出工／危害分頁輪播
const TICKER_PX_PER_S = 45;   // 跑馬燈速度（每秒像素）：固定速度，內容多寡不影響閱讀；
                              // 公告是整句中文，太快站在遠處讀不完
const STALE_AFTER_MIN = 15;   // 失更門檻（分）；伺服器有下發 stale_after_min 時以它為準
const RETRY_MS = 30000;       // 初始化失敗的重試間隔（冷開機防毒掃描要等）
const fmt = (v, d = 1) => (v == null || isNaN(v) ? '—' : Number(v).toFixed(d));

// ---------------------------------------------------------------------------
// 動畫：快進慢停、時間短；「減少動態效果」開啟時一律直接換，不播動畫
// ---------------------------------------------------------------------------
const EASE_OUT = 'cubic-bezier(.22,1,.36,1)';
const reduceMotion = () => !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);

/** 換內容：內容真的變了才寫入並淡入（換頁時不再瞬間跳換）。回傳是否有變。
 *  寫入是同步的——呼叫端接著量高度做自適應不受影響，動畫只是疊在上面。 */
function swapHtml(el, html) {
  if (el._html === html) return false;
  const first = el._html === undefined;
  el._html = html;
  el.innerHTML = html;
  if (!first && !reduceMotion() && el.animate) {
    el.animate([{ opacity: 0, transform: 'translateY(.375rem)' }, { opacity: 1, transform: 'none' }],
               { duration: 320, easing: EASE_OUT });
  }
  return true;
}

/** 滾動數字：逐字一格，變了的那一位舊字往上滑出、新字從下滑入；沒變的不動。
 *  完整數字放 aria-label，讀螢幕的人聽到的是整個數字而不是一格一格。 */
function rollText(el, text) {
  text = String(text);
  if (el._roll === text) return;
  const prev = el._roll;
  el._roll = text;
  el.setAttribute('aria-label', text);
  const chars = [...text];
  if (prev === undefined || [...prev].length !== chars.length || reduceMotion()) {
    el.innerHTML = chars.map(c => `<span class="rd" aria-hidden="true"><i>${esc(c)}</i></span>`).join('');
    return;
  }
  const old = [...prev];
  [...el.children].forEach((cell, k) => {
    if (old[k] === chars[k]) return;
    cell.innerHTML = `<i class="rd-out">${esc(old[k])}</i><i>${esc(chars[k])}</i>`;
    const [out, inn] = cell.children;
    const t = { duration: 380, easing: EASE_OUT, delay: (chars.length - 1 - k) * 40 };
    out.animate([{ transform: 'translateY(0)', opacity: 1 }, { transform: 'translateY(-100%)', opacity: 0 }],
                { ...t, fill: 'forwards' }).onfinish = () => out.remove();
    inn.animate([{ transform: 'translateY(100%)', opacity: 0 }, { transform: 'none', opacity: 1 }], { ...t, fill: 'backwards' });
  });
}

let DATA = null;              // 最近一次 /api/board-data 的回應
let SITE_ID = null;
let contactPage = 0, wfPage = 0, hzPage = 0;
let contactPer = 6;           // 聯絡人每頁筆數；畫完被裁切會自動縮
let lastContactSig = '';      // 聯絡人資料指紋；資料變了才把筆數放回上限
let hzLayout = null;          // 危害分頁結果 {key, pages:[[索引…]…]}；資料或卡片尺寸變了才重算
let hzHoldUntil = 0;          // 手動翻頁後暫停自動換頁到這個時間（毫秒）
const HZ_HOLD_MS = 30000;     // 手動翻頁後停留多久才恢復自動換頁
let lastLaterHtml = null;     // 「之後」兩個時段的上次內容；沒變就不動 DOM
let lastTickerSig = null;     // 跑馬燈內容指紋：沒變就不重建（重建會讓捲動從頭開始）
let cycleSig = null;          // 作業循環時程指紋：沒變就不重畫時間軸
let wfPer = 6;                // 出工每頁列數；畫完被裁切會自動縮（renderWorkforce）
let lastWfSig = '';           // 上次出工資料的指紋；資料變了才把列數放回上限
let BUILDING_ORDER = [];      // 出工一覽表分棟顯示的棟別順序（依 branding）
let pollTimer = null;
// 雲端模式：站台是唯讀看板（branding.wallboard=true）時，board-data 合併
// GitHub 排程推上來的外部區塊與雲端快取，而非即時查詢。此時是「固定視圖」——
// 主場站、隱藏工地下拉，比照舊戰情室的看板模式。
let WALL = false, WALL_KEY = '', POLL = POLL_MS;

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
(async () => {
  WALL_KEY = new URLSearchParams(location.search).get('k') || '';
  const brand = (await renderBrandLite()) || {};
  BUILDING_ORDER = buildingList(brand);
  WALL = !!brand.wallboard;
  // 雲端 15 分鐘（資料每 2 小時才更新一次，輪詢再勤也是對同一份快照付費；
  // 失更門檻 180 分容得下這個輪詢間隔）、地端 1 分鐘
  POLL = WALL ? 900000 : POLL_MS;
  document.getElementById('org').textContent = brand.war_room_name || '工地職安戰情室';
  setupBrandLogo(brand);
  // 雲端固定視圖：工地下拉不作用（快照只含主場站，工地名由快照帶出、
  // 於 load() 後填入）。
  if (WALL) {
    document.getElementById('site').disabled = true;
  }
  // 看板管理入口只在「免登入大螢幕」（帶 ?k= 權杖的 kiosk）隱藏；一般雲端
  // 訪客仍要進得去登入維護——看板設定（聯絡人、無災害起算日…）在雲端才是
  // 權威來源，之前對整個雲端一律隱藏會讓雲端根本沒有入口可維護。
  if (WALL_KEY) {
    const sl = document.getElementById('settingsLink');
    if (sl) sl.classList.add('hidden');
  }

  // 計時器先開再抓資料：整面牆整天停在錯誤畫面，沒有人按 F5 它永遠
  // 不會自己好——時鐘、輪播與重試都不能被一次失敗擋掉（舊戰情室的教訓）
  tick();
  setInterval(tick, 1000);
  setInterval(() => {
    contactPage++; wfPage++;
    if (Date.now() >= hzHoldUntil) hzPage++;   // 剛手動翻過就先停在那一頁
    if (DATA) { renderContacts(); renderWorkforce(); renderHazards(); }
  }, PAGE_MS);
  // 視窗尺寸變了（含進出全螢幕）或網頁字型載入完成（字寬、行高會變）就把
  // 每頁筆數放回上限、跑馬燈與危害分頁重新量測——螢幕變大時才長得回來，
  // 字型晚到時跑馬燈也不會因為寬度量錯而在接縫露出空白
  const refit = () => {
    wfPer = 6; contactPer = 6; hzLayout = null; lastTickerSig = null;
    if (DATA) { renderWorkforce(); renderContacts(); renderTicker(); renderHazards(); }
  };
  window.addEventListener('resize', refit);
  if (document.fonts && document.fonts.addEventListener) {
    document.fonts.addEventListener('loadingdone', refit);
  }

  document.getElementById('fullscreen').onclick = () => {
    // 電視棒／老 WebKit 只有帶前綴的 API；失敗就安靜作罷，牆上沒人看錯誤
    try {
      const el = document.documentElement;
      const enter = el.requestFullscreen || el.webkitRequestFullscreen;
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      const p = document.fullscreenElement ? exit.call(document) : enter.call(el);
      if (p && p.catch) p.catch(() => {});
    } catch (e) { /* 不支援全螢幕 */ }
  };

  // 今日作業危害：點分類籤條直接跳到那一類所在的頁，停留 30 秒再恢復自動換頁
  document.getElementById('hzChips').addEventListener('click', e => {
    const chip = e.target.closest('[data-i]');
    if (!chip || !hzLayout) return;
    const i = +chip.dataset.i;
    const pg = hzLayout.pages.findIndex(p => p.includes(i));
    if (pg < 0) return;
    hzPage = pg;
    hzHoldUntil = Date.now() + HZ_HOLD_MS;
    renderHazards();
  });

  initSites(brand);
})();

/**
 * 版頭公司標誌：圖由伺服器提供（不進版控），兩張都載入成功才顯示；
 * 任一張失敗（未設定）就改顯示文字公司名，版頭不會出現破圖。
 */
function setupBrandLogo(brand) {
  const box = document.getElementById('brandLogo');
  const text = document.getElementById('orgShort');
  text.textContent = brand.org_short || '';
  const imgs = [...box.querySelectorAll('img')];
  imgs.forEach(img => img.setAttribute('alt', ''));
  let ok = 0;
  const done = () => {
    if (++ok === imgs.length) {
      box.hidden = false;
      text.hidden = true;
      imgs[0].setAttribute('alt', brand.org_short || '公司標誌');
    }
  };
  imgs.forEach(img => {
    if (img.complete && img.naturalWidth) done();
    else { img.addEventListener('load', done); img.addEventListener('error', () => { box.hidden = true; }); }
  });
}

/** 抓工地清單並開始輪詢；失敗自動重試（冷開機時伺服器要 20~60 秒才起來） */
async function initSites(brand) {
  // 雲端固定視圖不需要工地清單：快照只含主場站一份，SITE_ID 只是路由
  // 佔位（雲端 board-data 忽略它、一律回快照），board-sites 也就不必打
  // ——省一次雲端資料庫喚醒，kiosk 免登入也不會卡在需登入的清單端點。
  if (WALL) {
    SITE_ID = parseInt(new URLSearchParams(location.search).get('site_id'), 10) || 0;
    load();
    pollTimer = setInterval(load, POLL);
    return;
  }
  let sites = [];
  try {
    sites = await API.get('/api/board-sites');
  } catch (e) {
    setStatus(`讀不到工地清單：${e.message}（${RETRY_MS / 1000} 秒後重試）`, true);
    setTimeout(() => initSites(brand), RETRY_MS);
    return;
  }
  // 單一主場站模式下只列主場站——board-data 的環境與出工本來就以主場站
  // 為準，列出全公司 40+ 個工地只會讓人切到「掛著別站名字的同一份資料」
  sites = fillableSites(sites, brand);
  const sel = document.getElementById('site');
  sel.innerHTML = sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');

  // 預設工地：網址參數 > 上次選的 > 主場站 > 第一個
  const urlId = parseInt(new URLSearchParams(location.search).get('site_id'), 10);
  const saved = parseInt(recallPref('boardSite'), 10);
  const primary = sites.find(s => s.code === (brand.primary_site_code || '').trim());
  SITE_ID = [urlId, saved, primary && primary.id, sites[0] && sites[0].id]
    .find(v => sites.some(s => s.id === v)) || null;
  if (SITE_ID) sel.value = SITE_ID;
  sel.onchange = () => { SITE_ID = parseInt(sel.value, 10); rememberPref('boardSite', sel.value); load(); };

  load();
  pollTimer = setInterval(load, POLL_MS);
}

/** 品牌設定；失敗或非 2xx 一律回 null（看板照常，只是表頭少字） */
async function renderBrandLite() {
  try {
    const r = await fetch('/api/branding', { credentials: 'same-origin' });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

function recallPref(k) { try { return localStorage.getItem('pref:' + k) || ''; } catch (e) { return ''; } }
function rememberPref(k, v) { try { localStorage.setItem('pref:' + k, v); } catch (e) { /* 無痕模式 */ } }

/**
 * 版頭狀態：只在出問題時顯示（讀取失敗、尚未收到資料）。正常時版頭已有時鐘、
 * 資料過期另有紅色警示條，「更新 HH:MM」只會讓版頭擁擠——改放時鐘的滑鼠提示。
 */
function setStatus(text, isError) {
  const el = document.getElementById('connection');
  el.textContent = text;
  el.className = isError ? 'error' : '';
  el.hidden = !isError;
  const sep = el.previousElementSibling;          // 前面的「·」分隔符一起收
  if (sep && sep.classList.contains('sep')) sep.hidden = !isError;
}

// ---------------------------------------------------------------------------
// 每秒：日期行、頁尾時鐘、錶盤與「現在應做事項」
// ---------------------------------------------------------------------------
function tick() {
  const now = new Date();
  document.getElementById('dateLine').textContent = rocDateLine(now);
  document.getElementById('clockText').textContent =
    now.toLocaleTimeString('zh-TW', { hour12: false });

  // 失更警示：牆上的時鐘照走、面板卻是舊資料時，看起來完全健康——
  // 必須大聲說。門檻由伺服器依資料來源的更新節奏下發（雲端約 75 分），
  // 沒下發就用地端的 15 分。generated_at 為 null 代表外部資料從未送達。
  const banner = document.getElementById('staleBanner');
  if (banner) {
    const limit = (DATA && DATA.stale_after_min) || STALE_AFTER_MIN;
    const at = DATA && DATA.generated_at;
    const stale = !!DATA && (!at || now - new Date(at) > limit * 60000);
    banner.hidden = !stale;
    // 版頭的點：資料新鮮時緩慢呼吸（看板是活的），過期就停下來變灰
    const dot = document.querySelector('.live-dot');
    if (dot) { dot.classList.toggle('fresh', !!DATA && !stale); dot.classList.toggle('stale', stale); }
    if (stale) {
      banner.textContent = (at ? `資料已停止更新（最後更新 ${at.replace('T', ' ')}）`
                               : '尚未收到外部資料（氣象／出工）')
        + (WALL ? '——雲端排程可能已停止或收集失敗，畫面上的數字不是現況'
                : '——地端主機或收集程式可能已停止，畫面上的數字不是現況');
    }
  }

  renderCycle(now);
}

/** 分鐘數 → 「X 小時 Y 分」／「Y 分鐘」 */
function durText(min) {
  const m = Math.max(0, Math.ceil(min));
  return m >= 60 ? `${Math.floor(m / 60)} 小時 ${m % 60} 分` : `${m} 分鐘`;
}

// ---------------------------------------------------------------------------
// 作業循環：大字「現在應做事項」＋本時段剩餘時間與進度＋整天時間軸
// （取代原本的圓形錶盤——站在遠處要一眼看懂「現在做什麼、還剩多久」）
// ---------------------------------------------------------------------------
function renderCycle(now) {
  const schedule = (DATA && DATA.board.config.schedule) || [];
  const rows = schedule.slice().sort((a, b) => minOf(a.start) - minOf(b.start));
  const { current, next } = scheduleAt(schedule, now);
  const nowMin = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
  const mod = v => ((v % 1440) + 1440) % 1440;

  document.getElementById('currentTask').textContent =
    current ? current.label : (rows.length ? '非作業時段' : '尚未設定時程');
  const card = document.getElementById('nowCard');
  card.style.setProperty('--slot', current ? SLOT_COLORS[slotType(current.label)] : 'rgba(255,255,255,.25)');
  card.classList.toggle('idle', !current);

  let remain = '', span = '', pct = 0;
  if (current) {
    const len = mod(minOf(current.end) - minOf(current.start)) || 1440;
    const done = mod(nowMin - minOf(current.start));
    remain = `本項還有 ${durText(len - done)}`;
    span = `${current.start}–${current.end}`;
    pct = Math.min(100, done / len * 100);
  } else if (next) {
    remain = `距下一項還有 ${durText(mod(minOf(next.start) - nowMin))}`;
  }
  document.getElementById('nowRemain').textContent = remain;
  document.getElementById('nowSpan').textContent = span;
  document.getElementById('nowBar').style.width = pct.toFixed(1) + '%';
  document.getElementById('nextTask').innerHTML = next
    ? `<span>下一項</span><b>${esc(next.start)}</b>${esc(next.label)}`
    : esc(rows.length ? '今日作業時段已結束' : '請由「看板管理」設定每日安全作業循環');
  // 下一項之後再列兩個時段，站遠也能預先知道接下來的安排
  const ni = next ? rows.indexOf(next) : -1;
  const later = ni >= 0 ? rows.slice(ni + 1, ni + 3) : [];
  const laterHtml = later.map(r => `<li><span>${esc(r.start)}</span>${esc(r.label)}</li>`).join('');
  if (laterHtml !== lastLaterHtml) {
    lastLaterHtml = laterHtml;
    document.getElementById('laterList').innerHTML = laterHtml;
  }

  // 時間軸：時程變了才重畫，每秒只移動「現在」指標與亮起目前時段
  const tl = document.getElementById('timeline');
  const sig = JSON.stringify(rows);
  if (sig !== cycleSig) {
    cycleSig = sig;
    if (!rows.length) {
      tl.innerHTML = '';
      document.getElementById('cycleRange').textContent = '';
    } else {
      const t0 = minOf(rows[0].start);
      const t1 = Math.max(...rows.map(r => minOf(r.start) + (mod(minOf(r.end) - minOf(r.start)) || 1440)));
      const pos = m => ((m - t0) / (t1 - t0) * 100).toFixed(2) + '%';
      const segs = rows.map((r, i) => {
        const a = minOf(r.start), len = mod(minOf(r.end) - a) || 1440;
        const w = len / (t1 - t0) * 100;
        // 太窄的時段（早會 5 分鐘）不塞字，滑鼠停留看全名
        return `<div class="tl-seg" data-i="${i}" data-t="${slotType(r.label)}" title="${esc(r.start)}–${esc(r.end)} ${esc(r.label)}"
          style="left:${pos(a)};width:${w.toFixed(2)}%;--c:${SLOT_COLORS[slotType(r.label)]}">${w >= 9 ? `<span>${esc(r.label)}</span>` : ''}</div>`;
      }).join('');
      const hours = [];
      const step = (t1 - t0) > 12 * 60 ? 2 : 1;
      for (let h = Math.ceil(t0 / 60); h * 60 <= t1; h += step) {
        hours.push(`<span style="left:${pos(h * 60)}">${String(h % 24).padStart(2, '0')}</span>`);
      }
      tl.dataset.t0 = t0; tl.dataset.t1 = t1;
      tl.innerHTML = `<div class="tl-bar">${segs}<i class="tl-now" id="tlNow"></i></div>
        <div class="tl-axis">${hours.join('')}</div>`;
      document.getElementById('cycleRange').textContent =
        `${rows[0].start}–${rows[rows.length - 1].end}・${rows.length} 個時段`;
    }
  }
  const nowEl = document.getElementById('tlNow');
  if (nowEl) {
    const t0 = +tl.dataset.t0, t1 = +tl.dataset.t1;
    let m = nowMin;
    if (m < t0 && m + 1440 <= t1) m += 1440;
    const inside = m >= t0 && m <= t1;
    nowEl.style.display = inside ? '' : 'none';
    if (inside) nowEl.style.left = ((m - t0) / (t1 - t0) * 100).toFixed(2) + '%';
    const idx = current ? rows.indexOf(current) : -1;
    tl.querySelectorAll('.tl-seg').forEach(el => el.classList.toggle('on', +el.dataset.i === idx));
  }
}

// ---------------------------------------------------------------------------
// 每分鐘：抓整頁資料並渲染
// ---------------------------------------------------------------------------
async function load() {
  if (SITE_ID == null) return;
  // 雲端看板讀快照，內容含缺失／廠商／聯絡電話，比照 wallboard 以 ?k=
  // 權杖驗證（大螢幕無登入）；已登入者不必帶，用 session 即可
  const q = WALL && WALL_KEY ? '?k=' + encodeURIComponent(WALL_KEY) : '';
  let d;
  try {
    d = await API.get('/api/board-data/' + SITE_ID + q);
  } catch (e) {
    // 路由不存在＝開在沒有 board-data 的站台——這是永久狀態，停止輪詢，
    // 不要每分鐘白打一次函式呼叫燒額度
    if (String(e.message).includes('找不到路由')) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      setStatus('此站台不提供看板資料——看板由工地檢視器／地端主機顯示', true);
      return;
    }
    if (String(e.message).includes('尚無快照')) {
      setStatus('雲端尚未收到地端推送的看板快照——請確認中央主機已開啟並在看板時段內', true);
      return;
    }
    setStatus('資料讀取失敗：' + e.message + '（自動重試）', true);
    return;
  }
  DATA = d;
  // 出工資料變了就把每頁列數放回上限重估——只縮不長的話，某天一筆特長的
  // 施作項目會讓之後每一天、每一棟都被切得過細
  const wfSig = JSON.stringify((d.worklog && d.worklog.rows) || []);
  if (wfSig !== lastWfSig) { lastWfSig = wfSig; wfPer = 6; }
  const ctSig = JSON.stringify((d.board && d.board.config.contacts) || []);
  if (ctSig !== lastContactSig) { lastContactSig = ctSig; contactPer = 6; }
  // 雲端固定視圖：工地名由快照帶出，填進（已停用的）下拉當標題
  if (WALL && d.board && d.board.site_name) {
    document.getElementById('site').innerHTML =
      `<option>${esc(d.board.site_name)}</option>`;
  }
  // 版頭只放短字；完整時間放滑鼠提示。資料過期另有紅色警示條
  setStatus(d.generated_at ? '' : '尚未收到外部資料（氣象／出工）', !d.generated_at);
  document.getElementById('clockText').title = d.generated_at
    ? `資料更新於 ${d.generated_at.replace('T', ' ')}` + (WALL ? '（雲端每 2 小時更新）' : '') : '';
  renderTicker();
  renderContacts();
  renderRecords();
  renderEnvironment();
  renderWorkforce();
  renderGate();
  renderHazards();
}

/** 門禁在場人數（地端：人臉辨識／門禁資料庫，一小時內最新一筆）。沒有讀數就整格隱藏 */
function renderGate() {
  const box = document.getElementById('wfGate');
  if (!box) return;
  const hc = DATA.headcount;
  const present = hc && hc.headcount_present;
  box.hidden = present == null;
  if (present == null) return;
  rollText(document.getElementById('gateNum'), Math.round(present).toLocaleString());
  const parts = [];
  if (hc.headcount_in != null) parts.push(`進 ${Math.round(hc.headcount_in)}`);
  if (hc.headcount_out != null) parts.push(`出 ${Math.round(hc.headcount_out)}`);
  // 讀數超過 15 分鐘就改標「幾點的讀數」，疏散點名時才不會把舊數字當現況；
  // 超過預期的更新間隔仍沒有新讀數（設備連不上）才轉紅字。雲端依
  // FACE_REFRESH_MIN 下發 expect_within_min；地端每 5 分鐘收集，用 15 分鐘
  const age = hc.reading_at ? Date.now() - new Date(hc.reading_at).getTime() : 0;
  const dated = age > 15 * 60000;
  const late = age > (hc.expect_within_min || 15) * 60000;
  box.classList.toggle('old', late);
  document.getElementById('gateSub').textContent =
    dated ? `${hc.reading_at.slice(11, 16)} 讀數` : parts.join('・');
  // 讀數時間放滑鼠提示：單行放不下，而且超過一小時的讀數後端本來就不回
  box.title = '門禁人臉辨識設備的即時在場人數；門禁人數不代替出工回報'
    + (hc.reading_at ? `（讀數 ${hc.reading_at.slice(11, 16)}）` : '');
}

// ---------------------------------------------------------------------------
// 安全佈告跑馬燈：危險級環境警示 ＋ 工地公告 ＋ 職安署新知
// ---------------------------------------------------------------------------
/** 跑馬燈項目（原始字串，轉義只在 renderTicker 一處做）。 */
function noticeItems() {
  const todayIso = today();   // common.js 的本地日期——toISOString 是 UTC，晚上會差一天
  const list = [];
  // 環境警示排最前面。第二級以下由「環境監測與法定作為」面板常駐顯示，
  // 只有第三級以上才在佈告再大聲說一次。
  const st = DATA.station;
  if (st && st.heat && st.heat.level >= 3) {
    list.push({ tag: '警示', cls: 'bad',
      title: `熱危害${st.heat.name || ''}・熱指數 ${fmt(st.metrics.heat_index)}°C`,
      body: st.heat.principle || '請加強熱危害防護' });
  }
  if (st && st.noise && st.noise.level >= 3) {
    list.push({ tag: '警示', cls: 'bad',
      title: `噪音${st.noise.name || ''}・${fmt(st.metrics.noise, 0)} dB`,
      body: st.noise.principle || '請採取聽力保護措施' });
  }
  for (const n of DATA.board.config.announcements || []) {
    if (!noticeActive(n, todayIso)) continue;
    list.push({ tag: '公告', cls: 'note', title: n.title, body: n.body || '' });
  }
  for (const n of DATA.news || []) {
    list.push({ tag: '新知', cls: 'news', title: n.title,
                body: n.published ? `職安署 ${n.published}` : '職安署' });
  }
  if (!list.length) list.push({ tag: '', cls: '', title: '今日無公告', body: '可由「看板管理」發布工地公告' });
  return list;
}

// ---------------------------------------------------------------------------
// 02 無災害紀錄
// ---------------------------------------------------------------------------
function renderRecords() {
  const r = DATA.record || {};
  // days 為 null＝尚未設定起算日：顯示「—」並提示設定，不用湊出來的數字
  rollText(document.getElementById('recordDays'), r.days == null ? '—' : r.days.toLocaleString());
  const note = document.getElementById('recordNote');
  note.textContent = '尚未設定起算日，請至「看板管理」設定';
  note.hidden = r.days != null;
  note.closest('.record-badge').title = r.days == null ? '無災害紀錄' : `無災害紀錄・自 ${r.since} 起算`;
  // 0 也是真實數字（上月整月無回報），不能顯示成「—」——那在這面牆上
  // 到處都代表「沒資料／斷線」
  document.getElementById('lastMonth').textContent = r.last_month_mandays == null
    ? '—' : `${r.last_month_mandays.toLocaleString()} 人日`;
  renderFindings();
}

/** 缺失改善提醒：三個數字，>0 才上色，另附文字狀態（不只靠顏色辨識）。
 *  格子只建一次、之後只換數字，數字變動才看得到滾動。 */
const FINDING_TILES = [
  ['findings_today', '今日新增', 'warn', '今日新開缺失'],
  ['open', '未結案', 'warn', '待改善或待複驗'],
  ['overdue', '逾期未改善', 'bad', '已逾期限，請優先處理'],
];
function renderFindings() {
  const el = document.getElementById('stats');
  const s = DATA.stats;
  if (!s) { el.innerHTML = '<p class="empty">缺失統計暫無資料</p>'; return; }
  if (!el.querySelector('.stat')) {
    el.innerHTML = FINDING_TILES.map(([key, label]) => `<div class="tile stat" data-k="${key}">
      <div><span class="tile-label">${label}</span><span class="state"></span></div><b></b></div>`).join('');
  }
  for (const [key, , tone, hint] of FINDING_TILES) {
    const tile = el.querySelector(`[data-k="${key}"]`);
    const v = Number(s[key]) || 0;
    tile.className = 'tile stat' + (v > 0 ? ' ' + tone : '');
    tile.querySelector('.state').textContent = v > 0 ? hint : '無';
    rollText(tile.querySelector('b'), v);
  }
}

/**
 * 安全佈告跑馬燈。內容重複到填滿一個視窗寬再放兩份，整條左移一份的寬度＝
 * 無縫循環；內容沒變就不重建，否則每次輪詢都會讓捲動從頭開始。
 */
function renderTicker() {
  const track = document.getElementById('tickerTrack');
  if (!track || !DATA) return;
  // 唯一的轉義點：noticeItems 給的都是原始字串。內文壓成一行、過長截斷
  const one = noticeItems().map(n => {
    const body = String(n.body || '').replace(/\s+/g, ' ').trim();
    return `<span class="tk-item ${n.cls}">${n.tag ? `<em>${esc(n.tag)}</em>` : ''}<b>${esc(n.title)}</b>`
      + (body ? `<span class="tk-body">${esc(body.length > 90 ? body.slice(0, 90) + '…' : body)}</span>` : '')
      + `</span><span class="tk-sep">◆</span>`;
  }).join('');
  const sig = one + '|' + window.innerWidth;
  if (sig === lastTickerSig) return;
  lastTickerSig = sig;
  track.innerHTML = `<div class="tk-run">${one}</div>`;
  const w = track.firstElementChild.getBoundingClientRect().width || 1;
  const vp = track.parentElement.getBoundingClientRect().width || 1;
  const run = one.repeat(Math.max(1, Math.ceil(vp / w)));
  track.innerHTML = `<div class="tk-run">${run}</div><div class="tk-run" aria-hidden="true">${run}</div>`;
  const runW = track.firstElementChild.getBoundingClientRect().width;
  track.style.animationDuration = Math.max(10, runW / TICKER_PX_PER_S).toFixed(1) + 's';
}

// ---------------------------------------------------------------------------
// 03 緊急連絡人（超過一頁就輪播）
// ---------------------------------------------------------------------------
function renderContacts() {
  if (!DATA) return;
  const rows = DATA.board.config.contacts || [];
  const body = document.getElementById('contacts');
  const PER = contactPer;
  if (!rows.length) {
    swapHtml(body, '<p class="empty">尚未設定聯絡人，請由「看板管理」維護。</p>');
    document.getElementById('contactPage').textContent = '';
    return;
  }
  const pages = Math.ceil(rows.length / PER);
  const pg = contactPage % pages;
  // 窄卡：一筆兩行（項目在上，姓名＋電話在下）
  swapHtml(body, rows.slice(pg * PER, pg * PER + PER).map(c => `<div class="contact">
    <span class="role">${esc(c.role)}</span>
    <div class="line"><span class="who">${esc(c.name)}</span><span class="tel">${esc(c.phone)}</span></div></div>`).join(''));
  document.getElementById('contactPage').textContent =
    pages > 1 ? `第 ${pg + 1}／${pages} 頁` : '';
  // 牆面 overflow:hidden：放不下就少放一筆重畫（同出工表）；手機版卡片高度
  // 跟著內容長，不會溢出，也就不會縮
  const panel = body.closest('.board-panel');
  if (contactPer > 2 && panel && panel.scrollHeight > panel.clientHeight + 1) {
    contactPer--;
    renderContacts();
  }
}

// ---------------------------------------------------------------------------
// 04 環境數值表
// ---------------------------------------------------------------------------
function renderEnvironment() {
  const st = DATA.station;
  renderWeatherStrip(st);
  renderEnvActions(st);
  // 不是今天的讀數才加日期——只寫時間會讓昨天的讀數看起來像今天
  document.getElementById('envUpdated').textContent = st && st.reading_at
    ? `更新 ${st.reading_at.slice(0, 10) === today() ? '' : st.reading_at.slice(5, 10) + ' '}${st.reading_at.slice(11, 16)}` : '';
}

/**
 * 環境數據 → 依法規現在應做什麼。熱危害依《高氣溫作業熱危害預防指引》分級
 * 與設施規則第 324-6 條；噪音依設施規則第 300 條。措施文字來自後端
 * heat_guidance／noise_guidance，這裡不另寫法規內容。
 */
function renderEnvActions(st) {
  const el = document.getElementById('envActions');
  if (!el) return;
  if (!st) { el.innerHTML = '<p class="empty">近三小時無環境數據（氣象站離線或未設定）</p>'; return; }
  const m = st.metrics || {};
  // 牆上只放最具體、有數字的做法；「補充水分：每 15～20 分鐘 1 次、…，規律執行
  // 而非口渴才喝」取冒號前當標題、第一個逗號前當內容，完整原文放滑鼠提示
  const item = t => {
    const s = String(t);
    const i = s.indexOf('：');
    if (i < 0) return `<li title="${esc(s)}">${esc(s)}</li>`;
    const body = s.slice(i + 1).split('，')[0];
    return `<li title="${esc(s)}"><b>${esc(s.slice(0, i))}</b>${esc(body)}</li>`;
  };
  const card = (lv, kind, name, value, unit, todo, extra, basis, basisFull) => `<section class="act lv${lv}">
      <header><span class="act-k">${kind}</span><span class="act-v">${value}<small>${unit}</small></span></header>
      <b class="act-lv">${esc(name)}</b>
      ${todo ? `<ul>${todo}</ul>` : ''}${extra || ''}
      <cite title="${esc(basisFull)}">依據：${esc(basis)}</cite></section>`;
  const parts = [];
  const h = st.heat;
  if (h) {
    const list = (h.reminders && h.reminders.length ? h.reminders : h.focus || []).slice(0, 2);
    const sun = h.if_direct_sun && h.if_direct_sun.level > h.level
      ? `<p class="act-sun" title="${esc(h.if_direct_sun.principle || '')}">日曬下視同<b>${esc(h.if_direct_sun.name)}</b></p>` : '';
    parts.push(card(h.level, '熱危害・熱指數', h.name || '', fmt(h.value), '°C',
      list.map(item).join(''), sun, '熱危害預防指引、設施規則§324-6',
      `${h.basis || '職安署《高氣溫作業熱危害預防指引》'}；職業安全衛生設施規則第324-6條。${h.principle || ''}`));
  }
  const n = st.noise;
  if (n) {
    const list = (n.focus || []).slice(0, 2).map(item).join('')
      + (n.allowed_hours != null ? `<li><b>每日容許</b>約 ${n.allowed_hours} 小時</li>` : '');
    parts.push(card(n.level, '噪音', n.name || '', fmt(n.value, 0), 'dB', list, '',
      '設施規則§300・即時值', `${n.basis || '職業安全衛生設施規則第300條'}。${n.principle || ''}${n.caveat || ''}`));
  } else if (m.noise != null) {
    // 未達 80 dB 時後端不回噪音指引；只說明門檻，不說「符合」——
    // 即時值不能判定 8 小時日時量平均是否合法
    parts.push(card(0, '噪音', '未達注意值', fmt(m.noise, 0), 'dB',
      '<li><b>85 dB 以上</b>（8 小時平均）應戴耳塞、耳罩</li>', '', '設施規則§300・即時值',
      '職業安全衛生設施規則第300條：勞工 8 小時日時量平均音壓級超過 85 分貝時，應使勞工戴用有效之耳塞、耳罩等防音防護具。本數值為即時音壓級，非 8 小時日時量平均值。'));
  }
  el.innerHTML = parts.join('') || '<p class="empty">無熱指數與噪音讀數</p>';
}

/** 工具列的參考讀數（溫度、濕度、風速、粉塵）。熱指數與噪音在「環境監測與
 *  法定作為」卡，這裡不重複。粉塵是環保空品指標，標「環保」以免被讀成職安超標。 */
function renderWeatherStrip(st) {
  const el = document.getElementById('wxStrip');
  if (!el) return;
  if (!st) { el.innerHTML = '<span class="wx-empty">氣象站離線</span>'; return; }
  const m = st.metrics || {};
  // 一格一項：小標籤在上、大數字在下。沒有讀數只顯示「—」，不掛單位
  const cell = (label, v, d, unit, epa) => {
    const val = fmt(v, d);
    return `<span class="wx${epa ? ' wx-epa' : ''}"${epa ? ' title="環境部空氣品質指標，非職安法定暴露值"' : ''}>`
      + `<span class="wx-k">${label}${epa ? '<em>環保</em>' : ''}</span>`
      + `<b>${val}${val === '—' ? '' : `<small>${unit}</small>`}</b></span>`;
  };
  el.innerHTML = cell('溫度', m.temperature, 1, '°C') + cell('濕度', m.humidity, 0, '%')
    + cell('風速', m.wind_speed, 1, 'm/s')
    + cell('PM2.5', m.pm25, 0, 'μg/m³', true) + cell('PM10', m.pm10, 0, 'μg/m³', true);
}

// ---------------------------------------------------------------------------
// 今日作業危害：危害類別、觸發的作業、法定應辦事項（附條號）。
// 分頁依實際高度裝箱：每一類先畫一次量高度，依序塞進卡片，放不下才換頁。
// 一類特別長（三條措施）不會拖累其他頁也只放一類。
// ---------------------------------------------------------------------------
function hazardHtml(h) {
  const src = h.sources || [];
  // 後端每類最多收 6 處來源，剛好 6 處時實際可能更多
  const count = `${src.length}${src.length >= 6 ? '+' : ''} 處作業`;
  const ms = (h.measures || []).map(x => `<li>${esc(x.text)}<cite>${esc(x.basis)}</cite></li>`).join('');
  return `<article class="hz"><header><span class="hz-name">${esc(h.label)}</span><span class="hz-count">${count}</span></header>
    <p class="hz-src" title="${esc(src.join('、'))}">${esc(src.join('、'))}</p>${ms ? `<ul class="hz-measures">${ms}</ul>` : ''}</article>`;
}

/** 危害分頁：[[索引…]…]。結果依「資料＋卡片尺寸」快取，換頁時不重量。 */
function hazardPages(hz, list) {
  const key = JSON.stringify(hz) + '|' + list.clientWidth + 'x' + list.clientHeight;
  if (hzLayout && hzLayout.key === key) return hzLayout.pages;
  let pages = [];
  if (getComputedStyle(list).overflowY === 'auto') {
    // 手機版可捲動：固定一頁兩類
    for (let i = 0; i < hz.length; i += 2) pages.push(hz.slice(i, i + 2).map((_, k) => i + k));
  } else {
    list.classList.remove('single');
    list.innerHTML = hz.map(hazardHtml).join('');
    const room = list.clientHeight;
    const gap = parseFloat(getComputedStyle(list).rowGap) || 0;
    let cur = [], used = 0;
    [...list.children].forEach((el, i) => {
      const h = el.offsetHeight;
      const need = cur.length ? used + gap + h : h;
      if (cur.length && need > room) { pages.push(cur); cur = [i]; used = h; }
      else { cur.push(i); used = need; }
    });
    if (cur.length) pages.push(cur);
  }
  hzLayout = { key, pages };
  return pages;
}

function renderHazards() {
  const list = document.getElementById('hazardList');
  const pageEl = document.getElementById('hzPage');
  if (!list) return;
  const hz = (DATA && DATA.hazards) || [];
  if (!hz.length) {
    list.innerHTML = '<p class="empty">今日出工回報尚無可對應的作業危害</p>';
    pageEl.textContent = '';
    document.getElementById('hzChips').hidden = true;
    hzLayout = null;
    return;
  }
  // 籤條先畫：它占掉的高度要算進分頁量測
  const chips = document.getElementById('hzChips');
  chips.hidden = hz.length <= 1;
  const chipHtml = hz.map((h, i) => `<button type="button" role="tab" data-i="${i}">${esc(h.label)}</button>`).join('');
  if (chips._html !== chipHtml) { chips._html = chipHtml; chips.innerHTML = chipHtml; hzLayout = null; }
  const before = hzLayout;
  const pages = hazardPages(hz, list);
  const measured = hzLayout !== before;     // 剛量測過：清單內容被整份重畫，一定要寫回
  const pg = hzPage % pages.length;
  const page = pages[pg];
  list.classList.toggle('single', page.length === 1);   // 一頁一類：卡片撐滿、字放大
  const html = page.map(i => hazardHtml(hz[i])).join('');
  if (html !== list._page || measured) {
    const turned = list._page !== undefined && html !== list._page;
    list._page = html;
    list.innerHTML = html;
    if (turned && !reduceMotion() && list.animate) {     // 換頁才淡入
      list.animate([{ opacity: 0, transform: 'translateY(.375rem)' }, { opacity: 1, transform: 'none' }],
                   { duration: 320, easing: EASE_OUT });
    }
  }
  pageEl.textContent = `${hz.length} 類危害`;
  // 目前這頁的分類反白；籤條放不下時捲到看得到的位置
  let first = null;
  chips.querySelectorAll('[data-i]').forEach(b => {
    const on = page.includes(+b.dataset.i);
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    if (on && !first) first = b;
  });
  if (first && chips.scrollWidth > chips.clientWidth) {
    chips.scrollTo({ left: first.offsetLeft - chips.offsetLeft - 8, behavior: reduceMotion() ? 'auto' : 'smooth' });
  }
}

// ---------------------------------------------------------------------------
// 05 本日出工一覽表
// ---------------------------------------------------------------------------
/**
 * 出工分頁：辦公棟／住宅棟分開，先依棟別分組（順序照 branding，沒對到的
 * 棟別排後面、未填棟別歸「未分棟」），每頁只放同一棟、同棟太多就分頁。
 * 純函式——縮列時要用新的列數重算頁序。
 */
function workforcePages(rows, per) {
  const groups = new Map();                         // 棟別 → rows[]
  for (const r of rows) {
    const b = r.building || '未分棟';
    if (!groups.has(b)) groups.set(b, []);
    groups.get(b).push(r);
  }
  const order = [...BUILDING_ORDER,
    ...[...groups.keys()].filter(b => !BUILDING_ORDER.includes(b))];
  const pages = [];
  for (const b of order) {
    const list = groups.get(b);
    if (!list || !list.length) continue;
    const headcount = list.reduce((sum, r) => sum + (r.headcount || 0), 0);
    const total = Math.ceil(list.length / per);
    for (let i = 0; i < total; i++) {
      pages.push({ building: b, start: i * per, rows: list.slice(i * per, i * per + per),
        headcount, vendors: list.length, pg: i + 1, total });
    }
  }
  return pages;
}

/**
 * 「今日出工數」卡：全案總人數＋各棟人數佔比條（分母＝本日全案總人數）。
 * 資料沒變就不重畫——出工表每 10 秒換頁會呼叫 renderWorkforce，
 * 每次重畫會讓佔比條的進場動畫一直重播。
 */
let lastKpiSig = null;
function renderHeadcountCard(w) {
  const sig = JSON.stringify([w.date, w.total, w.rows.map(r => [r.building, r.headcount])]);
  if (sig === lastKpiSig) return;
  lastKpiSig = sig;
  const total = document.getElementById('wfKpiNum');
  const vendors = document.getElementById('wfKpiVendors');
  const bars = document.getElementById('wfBuildings');
  if (!total || !vendors || !bars) return;
  if (!w.rows.length) {
    rollText(total, '—');
    vendors.textContent = '等待今日出工回報';
    bars.innerHTML = '';
    return;
  }
  rollText(total, w.total.toLocaleString());
  vendors.textContent = `${w.rows.length} 家廠商回報`;
  const groups = workforcePages(w.rows, w.rows.length);   // 每棟一頁＝每棟一組
  const sum = groups.reduce((a, g) => a + g.headcount, 0) || 1;
  bars.innerHTML = groups.map(g => `<div class="bar-row">
    <span class="name">${esc(g.building)}</span>
    <span class="val">${g.headcount.toLocaleString()} 人<small>${g.vendors} 家</small></span>
    <div class="bar-track"><div class="bar-fill" style="width:${(g.headcount / sum * 100).toFixed(1)}%"></div></div></div>`).join('');
}

function renderWorkforce() {
  const w = DATA.worklog;
  const body = document.getElementById('wfRows');
  renderHeadcountCard(w);
  if (!w.rows.length) {
    body._html = undefined;
    body.innerHTML = `<tr><td colspan="5"><div class="workforce-empty">
      <span class="empty-mark">／</span><h3>等待今日出工回報</h3>
      <p>各廠商於工務所群組回報後，依當日日期彙整顯示。</p></div></td></tr>`;
    document.getElementById('wfMeta').textContent = '工種 / 人數 / 施作項目';
    return;
  }
  const pages = workforcePages(w.rows, wfPer);
  const page = pages[wfPage % pages.length];

  // 工種與施作項目截為兩行（滑鼠停留看全文）：長文字撐高列高，是牆面
  // 裁切半列的主因；截行讓每列高度有上限，下面的自適應列數才會收斂。
  // 出工人數旁的細條：分母是本日單一廠商最多人數，用來比較家與家
  const maxHc = Math.max(1, ...w.rows.map(r => r.headcount || 0));
  // 每頁只放同一棟（棟別在卡片右上），所以不列棟別欄；整頁都沒有作業主管
  // 就收掉那一欄，把寬度讓給施作項目
  body.closest('table').classList.toggle('no-sup', !page.rows.some(r => r.supervisor));
  swapHtml(body, page.rows.map(r => `<tr>
    <td>${esc(r.vendor)}</td>
    <td title="${esc(r.trade || '')}"><div class="clamp2">${esc(r.trade || '—')}</div></td>
    <td>${r.headcount == null ? '—'
      : `<span class="hc"><b>${r.headcount}</b><i style="--w:${(r.headcount / maxHc * 100).toFixed(0)}%"></i></span>`}</td>
    <td>${esc(r.supervisor || '—')}</td>
    <td title="${esc(r.tasks || '')}"><div class="clamp2">${esc(r.tasks || '—')}</div></td></tr>`).join(''));
  document.getElementById('wfMeta').textContent =
    page.total > 1
      ? `${page.building} ${page.headcount.toLocaleString()} 人・${page.vendors} 家・第 ${page.pg}／${page.total} 頁`
      : `${page.building} ${page.headcount.toLocaleString()} 人・${page.vendors} 家`;

  // 畫完發現最後一列仍超出表格區（牆面 overflow:hidden 會直接裁掉半列）
  // 就少放一列重畫，縮到塞得下為止；手機版 table-area 可捲動，不縮。
  // 視窗尺寸變動（含進出全螢幕）或出工資料變動時 wfPer 會重設回 6 再重估。
  const area = body.closest('.table-area');
  if (area && wfPer > 2 && getComputedStyle(area).overflowY !== 'auto') {
    const last = body.lastElementChild;
    if (last && last.getBoundingClientRect().bottom >
        area.getBoundingClientRect().bottom + 1) {
      wfPer--;
      // 縮列會改變總頁數：把輪播位置對到「同一棟、包含本頁第一列」的新頁，
      // 否則頁序位移會讓某些廠商列在這一輪完全沒被顯示
      const idx = workforcePages(w.rows, wfPer).findIndex(x => x.building === page.building
        && x.start <= page.start && page.start < x.start + wfPer);
      if (idx >= 0) wfPage = idx;
      renderWorkforce();
    }
  }
}
