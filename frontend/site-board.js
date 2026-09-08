// 工地看板主邏輯。資料來源：
//   /api/board-sites          工地清單（選單）
//   /api/board-data/{id}      整頁資料（每分鐘輪詢一次）
// 五個區塊：安全佈告輪播、無災害紀錄、緊急連絡人、作業循環與環境、本日出工。
// 依賴 common.js（esc、siteOptions 不用）、board-core.js（日期、錶盤、時段）。

const POLL_MS = 60000;        // 資料輪詢
const NOTICE_MS = 12000;      // 公告輪播
const PAGE_MS = 10000;        // 聯絡人分頁輪播
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

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
(async () => {
  const brand = (await renderBrandLite()) || {};
  document.getElementById('org').textContent =
    (brand.org_short ? brand.org_short + '　' : '') + (brand.war_room_name || '工地安全戰情室');

  let sites = [];
  try {
    sites = await API.get('/api/board-sites');
  } catch (e) {
    setStatus('讀不到工地清單：' + e.message, true);
    return;
  }
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

  document.getElementById('fullscreen').onclick = () =>
    document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  document.getElementById('prevNotice').onclick = () => showNotice(noticeIdx - 1);
  document.getElementById('nextNotice').onclick = () => showNotice(noticeIdx + 1);
  document.getElementById('pauseNotice').onclick = e => {
    noticePaused = !noticePaused;
    e.target.textContent = noticePaused ? '恢復輪播' : '暫停輪播';
  };

  tick();
  setInterval(tick, 1000);
  load();
  setInterval(load, POLL_MS);
  setInterval(() => { if (!noticePaused) showNotice(noticeIdx + 1); }, NOTICE_MS);
  setInterval(() => {
    contactPage++; wfPage++;
    renderContacts(); if (DATA) renderWorkforce();
  }, PAGE_MS);
})();

/** 品牌設定；失敗回 null（看板照常，只是表頭少字） */
async function renderBrandLite() {
  try { return await (await fetch('/api/branding', { credentials: 'same-origin' })).json(); }
  catch (e) { return null; }
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
  if (!SITE_ID) return;
  let d;
  try {
    d = await API.get('/api/board-data/' + SITE_ID);
  } catch (e) {
    // 雲端填報站沒有 board-data（環境與出工資料只在地端）；其他就是斷線
    setStatus('資料讀取失敗：' + e.message + '（看板資料由工地檢視器／地端提供）', true);
    return;
  }
  DATA = d;
  setStatus(`資料更新於 ${d.generated_at.replace('T', ' ')}`);
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
  if (st && st.heat && st.heat.level >= 2) {
    list.push({
      kicker: `環境自動警示 · 熱危害${esc(st.heat.name || '')}`,
      title: `熱指數 ${st.metrics.heat_index != null ? st.metrics.heat_index.toFixed(1) : '—'} °C，${esc(st.heat.principle || '請加強防護')}`,
      body: (st.heat.measures || []).slice(0, 4).map(m => '・' + m).join('\n'),
      cls: st.heat.level >= 3 ? 'critical' : 'warning',
      source: '依《高氣溫作業熱危害預防指引》自動判定',
    });
  }
  if (st && st.noise && st.noise.level >= 2) {
    list.push({
      kicker: `環境自動警示 · ${esc(st.noise.name || '噪音')}`,
      title: `噪音 ${st.metrics.noise != null ? st.metrics.noise.toFixed(0) : '—'} dB，${esc(st.noise.principle || '請採取聽力保護')}`,
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
  stage.innerHTML = `${n.kicker ? `<div class="notice-kicker">${n.kicker}</div>` : ''}
    <h3>${esc(n.title)}</h3>${n.body ? `<p>${esc(n.body)}</p>` : ''}`;
  document.getElementById('noticeSource').textContent = n.source || '';
  document.getElementById('noticePage').textContent = `第 ${noticeIdx + 1}／${notices.length} 則`;
}

// ---------------------------------------------------------------------------
// 02 無災害紀錄
// ---------------------------------------------------------------------------
function renderRecords() {
  const h = DATA.hours;
  document.getElementById('hoursTotal').innerHTML =
    `${h.total.toLocaleString()} <small>工時</small>`;
  document.getElementById('hoursNote').textContent = h.since
    ? `自 ${h.since} 起算，依出工回報累計（人數×8 小時）`
    : '依出工回報累計（人數×8 小時）；起算日請至看板管理設定';
  document.getElementById('lastMonth').textContent =
    h.last_month ? `${h.last_month.toLocaleString()} 工時` : '—';

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
