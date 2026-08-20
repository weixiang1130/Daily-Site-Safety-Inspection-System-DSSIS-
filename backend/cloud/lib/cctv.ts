// 監視器畫面代理
//
// 戰情室不能直接嵌入監視器主機，有三道限制：
//   1. 主機回應 X-Frame-Options: SAMEORIGIN，明確禁止被別的網站 iframe
//   2. 主機是 http，戰情室是 https，瀏覽器會擋掉混合內容
//   3. 快照需要 HTTP Digest 驗證，帳密不能放到前端
//
// 因此改由後端代理：伺服器端完成驗證、取回 JPEG，再從本站的 https 網域送出。
// 帳密只存在環境變數裡，不會出現在瀏覽器。
//
// 主機是大華（Dahua）NVR，快照端點為
//   GET /cgi-bin/snapshot.cgi?channel=N
// 回應 image/jpeg。前端每隔數秒重新請求一次即可當成準即時畫面；
// 真正的串流是 RTSP，瀏覽器原生播不了，不在這一版的範圍。
//
// 環境變數：
//   CCTV_API_URL    主機位址，例如 http://host:81
//   CCTV_USER       帳號
//   CCTV_PASS       密碼
//   CCTV_CHANNELS   要顯示的頻道，逗號分隔，例如 "4,7"。未設定時預設 1

const env = (k: string) => Netlify.env.get(k) || "";

export function cctvChannels(): number[] {
  const raw = env("CCTV_CHANNELS").trim();
  if (!raw) return [1];
  return raw.split(",").map((s) => parseInt(s.trim(), 10))
    .filter((n) => Number.isInteger(n) && n > 0);
}

export function cctvEnabled(): boolean {
  return Boolean(env("CCTV_API_URL") && env("CCTV_USER"));
}

/**
 * 取得指定頻道的快照。
 *
 * Digest 驗證需要先送一次未帶憑證的請求換取 nonce，因此固定是兩趟。
 * 監視器主機都在同一個區域網段，多一趟的延遲可以接受。
 */
export async function fetchSnapshot(channel: number): Promise<ArrayBuffer> {
  const base = env("CCTV_API_URL").replace(/\/+$/, "");
  if (!base) throw new Error("未設定 CCTV_API_URL");

  const path = `/cgi-bin/snapshot.cgi?channel=${channel}`;
  const url = `${base}${path}`;

  // Netlify 函式本身只有 10 秒，而一次取像要兩趟（換 nonce、帶憑證），
  // 遇到 401 還要再一趟。因此用一個總預算控制，寧可提早回報「逾時」，
  // 也不要讓平台把函式砍掉——那樣前端只會拿到一個沒有訊息的錯誤。
  const deadline = Date.now() + 8500;
  const left = () => deadline - Date.now();

  const get = (headers?: HeadersInit) => {
    const ms = Math.min(5000, Math.max(0, left()));
    if (ms < 300) throw new Error("取得畫面逾時（監視器主機回應太慢）");
    return fetch(url, { headers, signal: AbortSignal.timeout(ms) });
  };

  let first: Response;
  try {
    first = await get();
  } catch (e: any) {
    // 連線層級的失敗（DNS、逾時、被拒）在這裡就要講清楚，
    // 否則前端只拿得到一個沒有訊息的錯誤
    throw new Error(`連不上監視器主機（${e?.name === "TimeoutError"
      ? "逾時 8 秒" : e?.message || e}）`);
  }

  if (first.ok) return await ensureImage(first);      // 有些機型不需驗證
  if (first.status !== 401) {
    throw new Error(`監視器回應 ${first.status}`);
  }

  const challenge = first.headers.get("www-authenticate") || "";
  if (!challenge) throw new Error("監視器要求驗證但未提供 WWW-Authenticate");
  if (!env("CCTV_USER")) throw new Error("監視器需要驗證，但未設定 CCTV_USER");

  const auth = digestHeader(challenge, "GET", path,
    env("CCTV_USER"), env("CCTV_PASS"));

  const second = await get({ Authorization: auth });
  if (second.status === 401) {
    // nonce 可能已被主機作廢；重新取一次挑戰再試，仍失敗才視為帳密有問題。
    // 但預算不夠時就不要再試，免得整支函式被平台砍掉而失去錯誤訊息。
    if (left() < 2500) throw new Error("監視器拒絕驗證（時間不足，未重試）");
    const retryChallenge = second.headers.get("www-authenticate") || challenge;
    const retry = await get({
      Authorization: digestHeader(retryChallenge, "GET", path,
        env("CCTV_USER"), env("CCTV_PASS")),
    });
    if (retry.status === 401) throw new Error("監視器拒絕驗證，請確認帳號密碼");
    if (!retry.ok) throw new Error(`監視器回應 ${retry.status}`);
    return await ensureImage(retry);
  }
  if (!second.ok) throw new Error(`監視器回應 ${second.status}`);

  return await ensureImage(second);
}

/** 確認拿到的真的是影像，而不是登入頁或錯誤訊息。 */
async function ensureImage(r: Response): Promise<ArrayBuffer> {
  const type = r.headers.get("content-type") || "";
  const buf = await r.arrayBuffer();

  // 有些機型驗證失敗時仍回 200，內容卻是 HTML 或純文字錯誤訊息。
  // 直接把它當影像送到前端，畫面就只會是一片空白而查不出原因。
  if (!type.startsWith("image/")) {
    const head = new TextDecoder().decode(buf.slice(0, 120)).replace(/\s+/g, " ");
    throw new Error(`監視器回傳非影像內容（${type || "無 content-type"}：${head}）`);
  }
  if (buf.byteLength < 1024) {
    throw new Error(`監視器回傳的影像過小（${buf.byteLength} 位元組），可能不是有效畫面`);
  }
  return buf;
}

/** 依 RFC 2617 組出 Digest 的 Authorization 標頭。 */
function digestHeader(
  challenge: string, method: string, uri: string,
  user: string, pass: string,
): string {
  const get = (key: string) => {
    const m = new RegExp(`${key}="([^"]*)"`).exec(challenge)
      || new RegExp(`${key}=([^,\\s]+)`).exec(challenge);
    return m ? m[1] : "";
  };

  const realm = get("realm");
  const nonce = get("nonce");
  const opaque = get("opaque");
  const qop = get("qop");

  const ha1 = md5(`${user}:${realm}:${pass}`);
  const ha2 = md5(`${method}:${uri}`);

  // 沒有 qop 的舊式算法，回應只由三段組成
  if (!qop) {
    const response = md5(`${ha1}:${nonce}:${ha2}`);
    return `Digest username="${user}", realm="${realm}", nonce="${nonce}", `
      + `uri="${uri}", response="${response}"`
      + (opaque ? `, opaque="${opaque}"` : "");
  }

  const nc = "00000001";
  const cnonce = Math.random().toString(16).slice(2, 10).padEnd(8, "0");
  const response = md5(`${ha1}:${nonce}:${nc}:${cnonce}:auth:${ha2}`);

  return `Digest username="${user}", realm="${realm}", nonce="${nonce}", `
    + `uri="${uri}", qop=auth, nc=${nc}, cnonce="${cnonce}", `
    + `response="${response}"`
    + (opaque ? `, opaque="${opaque}"` : "");
}

// ---------------------------------------------------------------------------
// MD5
//
// Digest 驗證規定用 MD5，而 Web Crypto 只提供 SHA 系列，因此必須自備。
// 這是標準的 RFC 1321 實作，只用於驗證握手，未用於任何安全性用途。
// ---------------------------------------------------------------------------

const S = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
  5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
  4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
  6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
];

const K = Array.from({ length: 64 }, (_, i) =>
  Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) >>> 0);

function md5(input: string): string {
  const bytes = new TextEncoder().encode(input);

  // 補位：附加 0x80，補零至 56 (mod 64)，末 8 位元組放原始長度（位元、小端）
  const bitLen = bytes.length * 8;
  const padded = new Uint8Array((((bytes.length + 8) >> 6) + 1) * 64);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  new DataView(padded.buffer).setUint32(padded.length - 8, bitLen >>> 0, true);
  new DataView(padded.buffer)
    .setUint32(padded.length - 4, Math.floor(bitLen / 4294967296), true);

  let [a0, b0, c0, d0] = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476];
  const view = new DataView(padded.buffer);

  for (let chunk = 0; chunk < padded.length; chunk += 64) {
    const M = Array.from({ length: 16 }, (_, i) =>
      view.getUint32(chunk + i * 4, true));

    let [A, B, C, D] = [a0, b0, c0, d0];
    for (let i = 0; i < 64; i++) {
      let F: number, g: number;
      if (i < 16) { F = (B & C) | (~B & D); g = i; }
      else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16; }
      else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16; }
      else { F = C ^ (B | ~D); g = (7 * i) % 16; }

      F = (F + A + K[i] + M[g]) >>> 0;
      A = D; D = C; C = B;
      B = (B + rotl(F, S[i])) >>> 0;
    }
    a0 = (a0 + A) >>> 0; b0 = (b0 + B) >>> 0;
    c0 = (c0 + C) >>> 0; d0 = (d0 + D) >>> 0;
  }

  return [a0, b0, c0, d0].map(le32Hex).join("");
}

const rotl = (x: number, n: number) => ((x << n) | (x >>> (32 - n))) >>> 0;

/** 32 位元整數轉成小端序的十六進位字串 */
function le32Hex(n: number): string {
  let out = "";
  for (let i = 0; i < 4; i++) {
    out += ((n >>> (i * 8)) & 0xff).toString(16).padStart(2, "0");
  }
  return out;
}
