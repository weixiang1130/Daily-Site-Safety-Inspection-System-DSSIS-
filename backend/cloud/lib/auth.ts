// 身分驗證：密碼雜湊驗證與簽章 session cookie。
//
// 密碼雜湊格式與 Python 版 app/auth.py 完全相同：
//     pbkdf2_sha256$<iterations>$<salt>$<hex digest>
// salt 是十六進位字串，雜湊時以其 UTF-8 位元組作為 salt（與 Python 端一致），
// 因此兩種實作可以共用同一份使用者資料。

const enc = new TextEncoder();

function b64urlEncode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(s: string): Uint8Array {
  const p = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = p + "=".repeat((4 - (p.length % 4)) % 4);
  const bin = atob(pad);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** 定時比較，避免以耗時差異推測雜湊值。 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2_sha256") return false;
  const [, iterStr, salt, digest] = parts;
  const iterations = parseInt(iterStr, 10);
  if (!Number.isFinite(iterations) || iterations <= 0) return false;

  const key = await crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(salt), iterations, hash: "SHA-256" },
    key, 256,
  );
  return timingSafeEqual(toHex(bits), digest);
}

export async function hashPassword(password: string, iterations = 120_000): Promise<string> {
  const saltBytes = crypto.getRandomValues(new Uint8Array(16));
  const salt = [...saltBytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(salt), iterations, hash: "SHA-256" },
    key, 256,
  );
  return `pbkdf2_sha256$${iterations}$${salt}$${toHex(bits)}`;
}

// ---------------------------------------------------------------------------
// Session cookie：<payload>.<HMAC-SHA256>
// ---------------------------------------------------------------------------
export const SESSION_COOKIE = "safety_session";
const MAX_AGE = 12 * 3600;

export interface SessionUser {
  id: number;
  username: string;
  name: string;
  role: string;
  site_id: number | null;
  employee_no: string | null;
  exp: number;
}

// 未設定 SECRET_KEY 時的替代金鑰：每次冷啟動隨機產生。
// 不能用固定的預設字串——它印在公開 repo 裡，等於任何人都能離線偽造
// session（含 admin）。隨機金鑰的代價只是「忘記設定時登入常常失效」，
// 這種失敗會被使用者立刻回報，而偽造 session 不會。
let ephemeralSecret: string | null = null;

function secret(): string {
  const s = Netlify.env.get("SECRET_KEY");
  if (s) return s;
  if (!ephemeralSecret) {
    const buf = new Uint8Array(32);
    crypto.getRandomValues(buf);
    ephemeralSecret = Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
    console.error("[auth] 未設定 SECRET_KEY，已改用本次啟動的隨機金鑰；"
      + "session 無法跨函式實例存活。請在環境變數設定 SECRET_KEY。");
  }
  return ephemeralSecret;
}

async function sign(payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret()), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  return b64urlEncode(new Uint8Array(sig));
}

export async function createSession(user: Omit<SessionUser, "exp">): Promise<string> {
  const data: SessionUser = { ...user, exp: Math.floor(Date.now() / 1000) + MAX_AGE };
  const payload = b64urlEncode(enc.encode(JSON.stringify(data)));
  return `${payload}.${await sign(payload)}`;
}

export async function readSession(req: Request): Promise<SessionUser | null> {
  const cookie = req.headers.get("cookie") || "";
  const m = cookie.match(new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`));
  if (!m) return null;

  const [payload, sig] = decodeURIComponent(m[1]).split(".");
  if (!payload || !sig) return null;
  if (!timingSafeEqual(sig, await sign(payload))) return null;

  try {
    const user = JSON.parse(new TextDecoder().decode(b64urlDecode(payload))) as SessionUser;
    if (!user.exp || user.exp < Math.floor(Date.now() / 1000)) return null;
    return user;
  } catch {
    return null;
  }
}

export function sessionCookieHeader(value: string, maxAge = MAX_AGE): string {
  const secure = Netlify.env.get("HTTPS_ONLY") !== "false" ? "; Secure" : "";
  return `${SESSION_COOKIE}=${encodeURIComponent(value)}; Path=/; HttpOnly; ` +
    `SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

export function clearSessionCookie(): string {
  return sessionCookieHeader("", 0);
}
