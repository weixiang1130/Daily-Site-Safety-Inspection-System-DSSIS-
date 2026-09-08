// 工地看板主邏輯。資料來源：
//   /api/board-sites          工地清單（選單）
//   /api/board-data/{id}      整頁資料（每分鐘輪詢一次）
// 五個區塊：安全佈告輪播、無災害紀錄、緊急連絡人、作業循環與環境、本日出工。
// 依賴 common.js（esc、siteOptions 不用）、board-core.js（日期、錶盤、時段）。

const POLL_MS = 60000;        // 資料輪詢
const NOTICE_MS = 12000;      // 公告輪播
const PAGE_MS = 10000;        // 聯絡人分頁輪播
const STALE_AFTER_MIN = 15;   // 資料多久沒更新要在牆上大聲說（沿用舊戰情室）
const RETRY_MS = 30000;       // 初始化失敗的重試間隔（冷開機防毒掃描要等）
const LEVEL_LABELS = ['正常', '注意', '警戒', '危險', '極度危險'];
const ENV_ROWS = [
  ['pm25', 'PM 2.5', 'μg/m³'], ['pm10', 'PM 10', 'μg/m³'],
  ['noise', '噪音', 'dB'], ['temperature', '溫度', '°C'],
  ['humidity', '濕度', '%'], ['wind_speed', '風速', 'm/s'],
  ['heat_index', '熱指數 體感', '°C'],
];

let DATA = null;              // 最近一次 /api/board-data 的回應
let SITE_ID = null;
let notices = [], noticeIdx = 0, noticePaused = false;
let contactPage = 0, wfPage = 0;
let pollTimer = null;
// 雲端快照模式：站台是唯讀看板（branding.wallboard=true）時，board-data
// 讀地端每 15 分鐘推上來的快照，而非即時查詢。此時是「固定視圖」——
// 主場站、隱藏工地下拉、輪詢對齊推送間隔，比照舊戰情室的看板模式。
let WALL = false, WALL_KEY = '', POLL = POLL_MS;

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
(async () => {
  WALL_KEY = new URLSearchParams(location.search).get('k') || '';
  const brand = (await renderBrandLite()) || {};
  WALL = !!brand.wallboard;
  POLL = WALL ? 900000 : POLL_MS;   // 雲端 15 分鐘（對齊快照）、地端 1 分鐘
  document.getElementById('org').textContent =
    (brand.org_short ? brand.org_short + '　' : '') + (brand.war_room_name || '工地安全戰情室');
  // 雲端固定視圖：工地下拉不作用（快照只含主場站，工地名由快照帶出、
  // 於 load() 後填入），看板管理只在地端可用
  if (WALL) {
    document.getElementById('site').disabled = true;
    const sl = document.getElementById('settingsLink');
    if (sl) sl.classList.add('hidden');
  }

  // 計時器先開再抓資料：整面牆整天停在錯誤畫面，沒有人按 F5 它永遠
  // 不會自己好——時鐘、輪播與重試都不能被一次失敗擋掉（舊戰情室的教訓）
  tick();
  setInterval(tick, 1000);
  setInterval(() => { if (!noticePaused) showNotice(noticeIdx + 1); }, NOTICE_MS);
  setInterval(() => {
    contactPage++; wfPage++;
    if (DATA) { renderContacts(); renderWorkforce(); }
  }, PAGE_MS);

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
  document.getElementById('prevNotice').onclick = () => showNotice(noticeIdx - 1);
  document.getElementById('nextNotice').onclick = () => showNotice(noticeIdx + 1);
  document.getElementById('pauseNotice').onclick = e => {
    noticePaused = !noticePaused;
    e.target.textContent = noticePaused ? '恢復輪播' : '暫停輪播';
  };

  initSites(brand);
})();

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

function setStatus(text, isError) {
  const el = document.getElementById('connection');
  el.textContent = text;
  el.className = isError ? 'error' : '';
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
  // 必須大聲說。沿用舊戰情室的 15 分鐘門檻。
  const banner = document.getElementById('staleBanner');
  if (banner) {
    const age = DATA ? now - new Date(DATA.generated_at) : 0;
    banner.hidden = !(DATA && age > STALE_AFTER_MIN * 60000);
    if (!banner.hidden) {
      banner.textContent = `資料已停止更新（最後更新 ${DATA.generated_at.replace('T', ' ')}）`
        + '——地端主機或收集程式可能已停止，畫面上的數字不是現況';
    }
  }

  const schedule = DATA ? DATA.board.config.schedule : [];
  renderDial(document.getElementById('dial'), schedule, now);
  const { current, next } = scheduleAt(schedule, now);
  document.getElementById('currentTask').textContent =
    current ? current.label : (schedule.length ? '非作業時段' : '尚未設定時程');
  document.getElementById('nextTask').textContent =
    next ? `下一項：${next.start} ${next.label}`
         : (schedule.length ? '' : '請由看板管理設定每日安全作業循環');
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
  // 雲端固定視圖：工地名由快照帶出，填進（已停用的）下拉當標題
  if (WALL && d.board && d.board.site_name) {
    document.getElementById('site').innerHTML =
      `<option>${esc(d.board.site_name)}</option>`;
  }
  setStatus(`資料更新於 ${d.generated_at.replace('T', ' ')}`
    + (WALL ? '（雲端快照，最長 15 分鐘更新一次）' : ''));
  buildNotices();
  renderContacts();
  renderRecords();
  renderEnvironment();
  renderWorkforce();
}

// ---------------------------------------------------------------------------
// 01 安全佈告／宣導：手動公告 ＋ 環境自動警示 ＋ 職安署新知
// ---------------------------------------------------------------------------
function buildNotices() {
  const todayIso = today();   // common.js 的本地日期——toISOString 是 UTC，晚上會差一天
  const list = [];

  // 環境自動警示排最前面：這是「現在就要做」的事
  const st = DATA.station;
  // 這裡一律給「原始字串」：轉義只在 showNotice 一個地方做。兩邊都轉
  // 或都不轉遲早會弄反——kicker 沒轉就是牆面的注入點、title 轉兩次
  // 法規文字的 & 會顯示成 &amp;。
  if (st && st.heat && st.heat.level >= 2) {
    list.push({
      kicker: `環境自動警示 · 熱危害${st.heat.name || ''}`,
      title: `熱指數 ${st.metrics.heat_index != null ? st.metrics.heat_index.toFixed(1) : '—'} °C，${st.heat.principle || '請加強防護'}`,
      body: (st.heat.measures || []).slice(0, 4).map(m => '・' + m).join('\n'),
      cls: st.heat.level >= 3 ? 'critical' : 'warning',
      source: '依《高氣溫作業熱危害預防指引》自動判定',
    });
  }
  if (st && st.noise && st.noise.level >= 2) {
    list.push({
      kicker: `環境自動警示 · ${st.noise.name || '噪音'}`,
      title: `噪音 ${st.metrics.noise != null ? st.metrics.noise.toFixed(0) : '—'} dB，${st.noise.principle || '請採取聽力保護'}`,
      body: (st.noise.measures || []).slice(0, 4).map(m => '・' + m).join('\n'),
      cls: st.noise.level >= 3 ? 'critical' : 'warning',
      source: '依職業安全衛生設施規則第 300 條自動判定',
    });
  }

  // 今日作業危害告知：出工回報的作業＋列控表今日工項，對應應注意危害。
  // 排在環境警示之後、一般公告之前——這是「今天特別要盯」的事。
  const hz = DATA.hazards || [];
  if (hz.length) {
    list.push({
      kicker: '今日作業危害告知',
      title: hz.slice(0, 4).map(h => h.label).join('、') + '——今日作業請注意',
      body: hz.slice(0, 5).map(h =>
        `・${h.label}：${h.sources.slice(0, 4).join('、')}`).join('\n'),
      cls: 'hazard',   // 專屬放大樣式：這張是要現場抬頭看完的
      source: '依本日出工回報與列控表工項自動對應（提示用，管制依各作業自主檢查表）',
    });
  }

  for (const n of DATA.board.config.announcements || []) {
    if (!noticeActive(n, todayIso)) continue;
    list.push({ kicker: '工地公告', title: n.title, body: n.body,
                source: n.source || '工地自行發布' });
  }

  for (const n of DATA.news || []) {
    list.push({ kicker: '職安新知 · 職安署', title: n.title,
                body: n.published ? `發布日期：${n.published}` : '',
                source: '職業安全衛生署新聞稿' });
  }

  if (!list.length) {
    list.push({ kicker: '', title: '今日無公告', body: '可由「看板管理」發布工地公告。', source: '' });
  }
  notices = list;
  showNotice(Math.min(noticeIdx, list.length - 1));
}

function showNotice(idx) {
  if (!notices.length) return;
  noticeIdx = ((idx % notices.length) + notices.length) % notices.length;
  const n = notices[noticeIdx];
  const stage = document.getElementById('notice');
  stage.className = 'notice-stage ' + (n.cls || '');
  // 唯一的轉義點：kicker/title/body 進來時都是原始字串
  stage.innerHTML = `${n.kicker ? `<div class="notice-kicker">${esc(n.kicker)}</div>` : ''}
    <h3>${esc(n.title)}</h3>${n.body ? `<p>${esc(n.body)}</p>` : ''}`;
  document.getElementById('noticeSource').textContent = n.source || '';
  document.getElementById('noticePage').textContent = `第 ${noticeIdx + 1}／${notices.length} 則`;
}

// ---------------------------------------------------------------------------
// 02 無災害紀錄
// ---------------------------------------------------------------------------
function renderRecords() {
  const r = DATA.record;
  // days 為 null＝尚未設定起算日：顯示「—」提示設定，不用湊出來的數字
  document.getElementById('recordDays').innerHTML = r.days == null
    ? '— <small>天</small>' : `${r.days.toLocaleString()} <small>天</small>`;
  document.getElementById('recordNote').textContent = r.days == null
    ? '尚未設定無災害起算日，請至「看板管理」設定'
    : `自 ${r.since} 起算`;
  // 0 也是真實數字（上月整月無回報），不能顯示成「—」——那在這面牆上
  // 到處都代表「沒資料／斷線」
  document.getElementById('lastMonth').textContent =
    `${r.last_month_mandays.toLocaleString()} 人日`;

  const s = DATA.stats;
  document.getElementById('stats').innerHTML = [
    ['今日缺失', s.findings_today, s.findings_today > 0],
    ['未結案', s.open, s.open > 0],
    ['逾期未改善', s.overdue, s.overdue > 0],
  ].map(([label, v, warn]) => `<div><span>${label}</span><b class="${warn ? 'attention' : ''}">${v}</b></div>`).join('');
}

// ---------------------------------------------------------------------------
// 03 緊急連絡人（超過一頁就輪播）
// ---------------------------------------------------------------------------
function renderContacts() {
  if (!DATA) return;
  const rows = DATA.board.config.contacts || [];
  const body = document.getElementById('contacts');
  const PER = 6;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="3"><p class="empty">尚未設定聯絡人，請由「看板管理」維護。</p></td></tr>';
    document.getElementById('contactPage').textContent = '';
    return;
  }
  const pages = Math.ceil(rows.length / PER);
  const pg = contactPage % pages;
  body.innerHTML = rows.slice(pg * PER, pg * PER + PER).map(c => `<tr>
    <td>${esc(c.role)}</td><td>${esc(c.name)}</td><td>${esc(c.phone)}</td></tr>`).join('');
  document.getElementById('contactPage').textContent =
    pages > 1 ? `第 ${pg + 1}／${pages} 頁` : '';
  const up = (DATA.board.config.updated_at || '').slice(0, 16).replace('T', ' ');
  document.getElementById('contactUpdated').textContent =
    up ? `由工地維護 · 更新於 ${up}` : '由工地維護聯絡資訊';
}

// ---------------------------------------------------------------------------
// 04 環境數值表
// ---------------------------------------------------------------------------
function renderEnvironment() {
  const st = DATA.station;
  const body = document.getElementById('environment');
  if (!st) {
    body.innerHTML = '<tr><td colspan="4"><p class="empty">近三小時無環境數據（氣象站離線或未設定）</p></td></tr>';
    document.getElementById('envUpdated').textContent = '';
    return;
  }
  body.innerHTML = ENV_ROWS.map(([key, label, unit]) => {
    const v = st.metrics[key];
    const lv = (st.levels || {})[key] || 0;
    let hint = lv ? `<span class="level ${lv >= 3 ? 'danger' : lv >= 2 ? 'warn' : ''}">${LEVEL_LABELS[lv]}</span>` : '';
    if (key === 'heat_index' && st.heat && st.heat.level > 0) {
      hint = `<span class="level ${st.heat.level >= 3 ? 'danger' : st.heat.level >= 2 ? 'warn' : ''}">${esc(st.heat.name || '')}</span>`;
    }
    if (key === 'noise' && st.noise && st.noise.level > 0) {
      hint = `<span class="level ${st.noise.level >= 3 ? 'danger' : st.noise.level >= 2 ? 'warn' : ''}">${esc(st.noise.name || '')}</span>`;
    }
    return `<tr><td>${label}</td>
      <td>${v == null ? '—' : (Math.round(v * 10) / 10)}</td>
      <td>${unit}</td><td>${hint || '—'}</td></tr>`;
  }).join('');
  document.getElementById('envUpdated').textContent =
    st.reading_at ? `更新 ${st.reading_at.replace('T', ' ')}` : '';
}

// ---------------------------------------------------------------------------
// 05 本日出工一覽表
// ---------------------------------------------------------------------------
function renderWorkforce() {
  const w = DATA.worklog;
  document.getElementById('wfTotal').innerHTML =
    `${w.total.toLocaleString()} <small>人</small>`;
  document.getElementById('wfNote').textContent =
    w.rows.length ? `${w.rows.length} 家廠商回報（${w.date}）` : '來源：工務所群組出工回報';
  const body = document.getElementById('wfRows');
  if (!w.rows.length) {
    body.innerHTML = `<tr><td colspan="6"><div class="workforce-empty">
      <span class="empty-mark">／</span><h3>等待今日出工回報</h3>
      <p>各廠商於工務所群組回報後，依當日日期彙整顯示。</p>
      <p>門禁人數不代替出工回報。</p></div></td></tr>`;
    document.getElementById('wfMeta').textContent = '工種 / 人數 / 施作項目';
    return;
  }
  // 廠商多的時候整版塞不下（牆上也沒有人會捲動），分頁自動輪播
  const PER = 6;
  const pages = Math.max(1, Math.ceil(w.rows.length / PER));
  const pg = wfPage % pages;
  body.innerHTML = w.rows.slice(pg * PER, pg * PER + PER).map(r => `<tr>
    <td>${esc(r.building || '—')}</td>
    <td>${esc(r.vendor)}</td>
    <td title="${esc(r.trade || '')}">${esc(r.trade || '—')}</td>
    <td>${r.headcount == null ? '—' : r.headcount}</td>
    <td>${esc(r.supervisor || '—')}</td>
    <td title="${esc(r.tasks || '')}">${esc(r.tasks || '—')}</td></tr>`).join('');
  document.getElementById('wfMeta').textContent =
    pages > 1 ? `第 ${pg + 1}／${pages} 頁・共 ${w.rows.length} 家` : '工種 / 人數 / 施作項目';
}
