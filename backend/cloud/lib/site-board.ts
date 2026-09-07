// 與地端 app/site_board.py 維持相同的設定契約。
export function validateBoard(value: any) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('設定格式錯誤');
  const fields: Record<string, [number, Record<string, number>]> = {
    contacts: [12, {role: 32, name: 64, phone: 40}],
    schedule: [24, {start: 5, end: 5, label: 40}],
    announcements: [20, {title: 80, body: 600, source: 100, url: 500, start_date: 10, end_date: 10}],
  };
  const out: Record<string, any> = {}, occupied = new Set<number>();
  for (const [key, [limit, columns]] of Object.entries(fields)) {
    const rows = value[key] ?? [];
    if (!Array.isArray(rows) || rows.length > limit) throw Error(`${key} 筆數超過上限或格式錯誤`);
    out[key] = rows.map(row => {
      if (!row || typeof row !== 'object' || Array.isArray(row)) throw Error('欄位格式錯誤');
      const clean: Record<string, string> = {};
      for (const [field, size] of Object.entries(columns)) {
        const text = row[field] ?? '';
        if (typeof text !== 'string' || [...text.trim()].length > size) throw Error(`${field} 格式或長度錯誤`);
        clean[field] = text.trim();
      }
      if (key === 'contacts' && (!clean.role || !clean.name || !clean.phone)) throw Error('聯絡人職務、姓名與電話必填');
      if (key === 'schedule') {
        if (!clean.label) throw Error('作業名稱必填');
        const times = ['start', 'end'].map(field => {
          if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(clean[field])) throw Error('時間請使用 HH:MM');
          const [h, m] = clean[field].split(':').map(Number); return h * 60 + m;
        });
        const [start, end] = times;
        if (start === end) throw Error('開始與結束時間不可相同');
        for (let m = start; m !== end; m = (m + 1) % 1440) {
          if (occupied.has(m)) throw Error('作業時段不可重疊'); occupied.add(m);
        }
      }
      if (key === 'announcements') {
        if (!clean.title || !clean.body) throw Error('公告標題與內容必填');
        for (const field of ['start_date', 'end_date']) {
          const d = clean[field];
          if (d && (!/^\d{4}-\d{2}-\d{2}$/.test(d) || !Number.isFinite(Date.parse(d)) || new Date(d).toISOString().slice(0, 10) !== d)) throw Error('公告日期格式錯誤');
        }
        if (clean.start_date && clean.end_date && clean.end_date < clean.start_date) throw Error('公告結束日期不可早於開始日期');
        if (clean.url) {
          let u: URL; try { u = new URL(clean.url); } catch { throw Error('來源網址格式錯誤'); }
          if (u.protocol !== 'https:' || !u.hostname || u.username || u.password) throw Error('來源網址須為 HTTPS 且不可含帳密');
        }
      }
      return clean;
    });
  }
  out.schedule.sort((a: any, b: any) => a.start.localeCompare(b.start));

  // 無災害紀錄的起算設定：起算日與起算前已累計的工時由工地填，
  // 之後的工時由出工回報自動累計（人數×8）
  const safety = value.safety ?? {};
  if (!safety || typeof safety !== 'object' || Array.isArray(safety)) throw Error('無災害設定格式錯誤');
  const start = safety.start_date ?? '', hours = safety.base_hours ?? '';
  if (typeof start !== 'string' || typeof hours !== 'string') throw Error('無災害設定格式錯誤');
  const s = start.trim(), h = hours.trim();
  if (s && (!/^\d{4}-\d{2}-\d{2}$/.test(s) || !Number.isFinite(Date.parse(s))
      || new Date(s).toISOString().slice(0, 10) !== s)) throw Error('無災害起算日格式錯誤');
  if (h && !/^\d{1,9}$/.test(h)) throw Error('起算前累計工時請填整數');
  out.safety = {start_date: s, base_hours: h};
  return out;
}

export function boardPayload(site: any) {
  const config = site.board_config ? JSON.parse(site.board_config) : {};
  config.contacts ??= []; config.schedule ??= []; config.announcements ??= [];
  config.safety ??= {start_date: '', base_hours: ''};
  return {site_id: site.id, site_code: site.code, site_name: site.name,
    revision: site.board_revision || 0, management_url: '', config};
}
