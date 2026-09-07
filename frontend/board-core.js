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

/**
 * 畫 12 小時制的作業循環錶盤（SVG），時段畫成外圈弧形、目前時段加亮。
 * 樣式類別（dial-*）定義在 site-board.css。
 * 秒針每秒重畫整個 SVG 成本很低（十幾個節點），不做增量更新。
 */
function renderDial(el, schedule, now = new Date()) {
  const C = 100, RF = 78, RS = 90;         // 圓心、錶面半徑、時段弧半徑
  const angle = min => (min % 720) / 720 * 2 * Math.PI - Math.PI / 2;
  const pt = (r, a) => `${(C + r * Math.cos(a)).toFixed(1)} ${(C + r * Math.sin(a)).toFixed(1)}`;

  // 時段弧。錶盤是 12 小時制，超過 12 小時的時段畫滿一圈就好
  const active = scheduleAt(schedule, now).current;
  const arcs = (schedule || []).map(s => {
    const a0 = minOf(s.start), a1raw = minOf(s.end);
    const span = ((a1raw - a0) % 1440 + 1440) % 1440;
    const a1 = a0 + Math.min(span, 719);
    const large = (a1 - a0) % 720 > 360 ? 1 : 0;
    const cls = s === active ? ' active' : '';
    return `<path class="dial-sector${cls}" stroke-width="9" d="M ${pt(RS, angle(a0))} A ${RS} ${RS} 0 ${large} 1 ${pt(RS, angle(a1))}"/>`;
  }).join('');

  const nums = Array.from({ length: 12 }, (_, i) => {
    const n = i === 0 ? 12 : i;
    const a = angle(i * 60);
    return `<text class="dial-number" x="${(C + 64 * Math.cos(a)).toFixed(1)}" y="${(C + 64 * Math.sin(a)).toFixed(1)}">${n}</text>`;
  }).join('');

  // angle() 的刻度是「720 分鐘一圈」：時針直接餵當日分鐘數，
  // 分針與秒針把 60（分／秒）換算成 720 的比例即可
  const t = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
  const hand = (r, a, cls, w) => {
    const x = (C + r * Math.cos(a)).toFixed(1);
    const y = (C + r * Math.sin(a)).toFixed(1);
    return `<line class="${cls}" stroke-width="${w}" x1="${C}" y1="${C}" x2="${x}" y2="${y}"/>`;
  };
  const hands = hand(38, angle(t), 'dial-hand', 4)
    + hand(56, angle((t % 60) * 12), 'dial-hand', 2.5)
    + hand(62, angle(now.getSeconds() * 12), 'dial-second', 1);

  el.innerHTML = `<svg viewBox="0 0 200 200" role="img" aria-label="作業循環時鐘">
    <circle class="dial-face" cx="${C}" cy="${C}" r="${RF}" stroke-width="1.5"/>
    <circle class="dial-track" cx="${C}" cy="${C}" r="${RS}" stroke-width="9" opacity=".35"/>
    ${arcs}${nums}${hands}<circle cx="${C}" cy="${C}" r="3.5" fill="currentColor"/></svg>`;
}

/** 公告是否在效期內（起訖留空視為不限） */
function noticeActive(n, todayIso) {
  if (n.start_date && todayIso < n.start_date) return false;
  if (n.end_date && todayIso > n.end_date) return false;
  return true;
}
