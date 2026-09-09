// 工地看板共用：日期格式、作業循環時段運算、時鐘錶盤繪製。
// dashboard.html（看板本體）與 board-settings.html（維護頁）共用。
// 依賴 common.js（esc、API）。

/** 民國日期字串：115/09/08（一） */
function rocDateLine(d = new Date()) {
  const wd = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()];
  const p2 = n => String(n).padStart(2, '0');
  return `${d.getFullYear() - 1911}/${p2(d.getMonth() + 1)}/${p2(d.getDate())}（${wd}）`;
}

/** HH:MM → 當日第幾分鐘 */
function minOf(hhmm) {
  const [h, m] = String(hhmm || '0:0').split(':').map(Number);
  return (h || 0) * 60 + (m || 0);
}

/**
 * 目前時刻落在哪個作業時段、下一個時段是什麼。
 * 時段允許跨夜（end < start），與後端 site_board 的驗證同一套語意。
 * @returns {{current: object|null, next: object|null}}
 */
function scheduleAt(schedule, now = new Date()) {
  const rows = (schedule || []).slice().sort((a, b) => minOf(a.start) - minOf(b.start));
  const t = now.getHours() * 60 + now.getMinutes();
  const inSlot = s => {
    const a = minOf(s.start), b = minOf(s.end);
    return a < b ? (t >= a && t < b) : (t >= a || t < b);
  };
  const current = rows.find(inSlot) || null;
  const next = rows.find(s => minOf(s.start) > t) || rows[0] || null;
  return { current, next: next === current ? null : next };
}

// 時段的語意分類 → 顏色，仿工地標準「安全作業循環」時鐘圖：
// 綠＝安全作業、藍＝午休、黃＝休息、紅＝會議／教育／檢討、灰＝檢點／整理。
// 依 label 關鍵字判定，工地自訂的時段名只要含這些詞就會自動上對的色。
const SLOT_COLORS = {
  work: '#2E8B57', lunch: '#2874A6', rest: '#C7960C',
  meeting: '#B03A2E', check: '#7B8794', other: '#9AA5B1',
};

function slotType(label) {
  const s = label || '';
  if (/午休/.test(s)) return 'lunch';
  if (/休息/.test(s)) return 'rest';
  if (/早會|會議|預知|檢討|宣導|教育|訓練/.test(s)) return 'meeting';
  if (/檢點|檢查|整理|整頓|清掃|清潔|確認|結束|收尾/.test(s)) return 'check';
  if (/作業/.test(s)) return 'work';
  return 'other';
}

/**
 * 畫「安全作業循環」時鐘（SVG）——仿工地張貼的標準流程時鐘圖：
 * 外圈是彩色時段環（每個時段一段、依語意上色、標作業名與起訖時間點），
 * 內圈是 12 小時傳統錶面（數字＋時分秒針）；目前時段以白框加亮。
 * viewBox 固定、等比縮放，畫面縮放不影響比例。
 * 秒針每秒重畫整張圖（節點少、成本低），不做增量更新。
 */
function renderDial(el, schedule, now = new Date()) {
  const C = 100, RI = 58, RO = 90;          // 圓心、時段環內／外半徑
  const RM = (RI + RO) / 2;
  const angle = min => (min % 720) / 720 * 2 * Math.PI - Math.PI / 2;
  const xy = (r, a) => [C + r * Math.cos(a), C + r * Math.sin(a)];
  const pt = (r, a) => xy(r, a).map(v => v.toFixed(1)).join(' ');

  const rows = (schedule || []).slice().sort((a, b) => minOf(a.start) - minOf(b.start));
  const active = scheduleAt(schedule, now).current;

  // 彩色時段扇形（甜甜圈的一段：外弧→內弧封閉），中央放作業名
  const sectors = rows.map(s => {
    const start = minOf(s.start);
    const span = Math.min(((minOf(s.end) - start) % 1440 + 1440) % 1440, 719);
    const a0 = angle(start), a1 = angle(start + span);
    const large = span > 360 ? 1 : 0;
    const color = SLOT_COLORS[slotType(s.label)];
    const on = s === active;
    const d = `M ${pt(RO, a0)} A ${RO} ${RO} 0 ${large} 1 ${pt(RO, a1)} `
            + `L ${pt(RI, a1)} A ${RI} ${RI} 0 ${large} 0 ${pt(RI, a0)} Z`;
    // 標籤只放得下才放：太窄的時段（如 5 分鐘早會）只標時間點，不塞字
    let label = '';
    if (span >= 24) {
      const [lx, ly] = xy(RM, angle(start + span / 2));
      label = `<text class="dial-label" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}">${esc(s.label || '')}</text>`;
    }
    return `<path class="dial-slot${on ? ' on' : ''}" d="${d}" fill="${color}"/>${label}`;
  }).join('');

  // 時段邊界的時間點，標在環外緣
  const marks = [], seen = new Set();
  rows.forEach(s => [s.start, s.end].forEach(hhmm => {
    const m = minOf(hhmm);
    if (seen.has(m)) return;
    seen.add(m);
    const [tx, ty] = xy(RO + 6, angle(m));
    marks.push(`<text class="dial-time" x="${tx.toFixed(1)}" y="${ty.toFixed(1)}">${esc(hhmm)}</text>`);
  }));

  // 內圈傳統錶面：數字與指針
  const nums = Array.from({ length: 12 }, (_, i) => {
    const n = i === 0 ? 12 : i;
    const [nx, ny] = xy(RI - 11, angle(i * 60));
    return `<text class="dial-number" x="${nx.toFixed(1)}" y="${ny.toFixed(1)}">${n}</text>`;
  }).join('');

  // angle() 的刻度是「720 分鐘一圈」：時針餵當日分鐘數，分／秒針把
  // 60 換算成 720 的比例
  const t = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
  const hand = (r, a, cls, w) => {
    const [x, y] = xy(r, a);
    return `<line class="${cls}" stroke-width="${w}" x1="${C}" y1="${C}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
  };
  const hands = hand(RI - 24, angle(t), 'dial-hand', 3)
    + hand(RI - 8, angle((t % 60) * 12), 'dial-hand', 2)
    + hand(RI - 4, angle(now.getSeconds() * 12), 'dial-second', 1);

  el.innerHTML = `<svg viewBox="-8 -8 216 216" role="img" aria-label="安全作業循環時鐘">
    ${rows.length ? '' : `<circle cx="${C}" cy="${C}" r="${RM}" fill="none" class="dial-track" stroke-width="${RO - RI}" opacity=".25"/>`}
    ${sectors}
    <circle class="dial-face" cx="${C}" cy="${C}" r="${RI}" stroke-width="1"/>
    ${marks.join('')}${nums}${hands}
    <circle cx="${C}" cy="${C}" r="2.8" fill="var(--slate-900)"/></svg>`;
}

/** 公告是否在效期內（起訖留空視為不限） */
function noticeActive(n, todayIso) {
  if (n.start_date && todayIso < n.start_date) return false;
  if (n.end_date && todayIso > n.end_date) return false;
  return true;
}
