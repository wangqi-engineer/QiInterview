import { create } from "zustand";
import { api } from "@/lib/api";

export interface MeOut {
  id: number;
  email: string | null;
  username: string | null;
}

export interface PubkeyOut {
  public_key_pem: string;
  fingerprint16: string;
  alg: string;
  hash: string;
}

export interface AuthState {
  me: MeOut | null;
  /** ``init`` 仅在 App 启动时调用一次。该字段标记"是否已经请求过 /me"； */
  initialized: boolean;
  /** 等价 ``me === null && initialized``。 */
  loading: boolean;
  init: () => Promise<void>;
  // P6 / 邮箱主身份：注册分两步。
  //   1) registerStart(email)        —— 后端寄出 6 位 OTP 到邮箱；响应 200 不
  //      暴露 email 是否已注册。
  //   2) registerVerify(email, code, password, username?)  —— OTP 校验通过后
  //      创建账号 + 写 cookie。``password`` 仍走 RSA-OAEP 加密。
  registerStart: (email: string) => Promise<void>;
  registerVerify: (
    email: string,
    code: string,
    password: string,
    username?: string,
  ) => Promise<MeOut>;
  // 登录改为邮箱 + 密码（密码仍走 RSA-OAEP）。老 username 入参签名直接删除，
  // 调用方编译期就会被定位到。
  login: (email: string, password: string) => Promise<MeOut>;
  logout: () => Promise<void>;
  // 找回密码两步：发送链接 → 用 token 重置（重置成功不自动登录，让 UI 跳到
  // /login 重走一次正式登录，避免社工攻击搭便车）。
  passwordResetStart: (email: string) => Promise<void>;
  passwordResetConfirm: (token: string, newPassword: string) => Promise<void>;
}

// ─────────────────────────────────────────────────────────────────────────────
// P5 / B1：RSA-OAEP 客户端加密辅助
// ─────────────────────────────────────────────────────────────────────────────

interface ImportedPubkey {
  fingerprint16: string;
  cryptoKey: CryptoKey;
}

let _pubkeyPromise: Promise<ImportedPubkey> | null = null;

const _SUBTLE_DEBUG_TAG = "[auth/rsa]";

/**
 * 把 PEM SubjectPublicKeyInfo 字符串（``-----BEGIN PUBLIC KEY-----`` 打头）
 * 解出原始 DER ``ArrayBuffer``。
 */
function pemSpkiToDer(pem: string): ArrayBuffer {
  const cleaned = pem
    .replace(/-----BEGIN PUBLIC KEY-----/g, "")
    .replace(/-----END PUBLIC KEY-----/g, "")
    .replace(/\s+/g, "");
  const bin = atob(cleaned);
  const len = bin.length;
  const buf = new ArrayBuffer(len);
  const view = new Uint8Array(buf);
  for (let i = 0; i < len; i++) view[i] = bin.charCodeAt(i);
  return buf;
}

function arrayBufferToBase64(buf: ArrayBuffer): string {
  const view = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < view.length; i++) s += String.fromCharCode(view[i]);
  return btoa(s);
}

async function _sha256Hex(input: ArrayBuffer | Uint8Array): Promise<string> {
  // 统一拷贝到一个独立 Uint8Array，规避 ``Uint8Array.buffer`` 在 lib.dom
  // 类型里被声明为 ``ArrayBufferLike`` 的尴尬（包含 SharedArrayBuffer，
  // 不被 ``crypto.subtle.digest`` 接受）。
  const view = input instanceof Uint8Array ? input : new Uint8Array(input);
  const buf = new Uint8Array(view.byteLength);
  buf.set(view);
  const digest = await crypto.subtle.digest("SHA-256", buf);
  const bytes = new Uint8Array(digest);
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return hex;
}

/** 取后端公钥并 import 成 RSA-OAEP / SHA-256 ``CryptoKey``。带 session
 * 内单例缓存，避免每次 register/login 都多打一次 ``/auth/pubkey``。 */
async function getImportedPubkey(): Promise<ImportedPubkey> {
  if (_pubkeyPromise) return _pubkeyPromise;
  _pubkeyPromise = (async () => {
    const resp = await api.get<PubkeyOut>("/auth/pubkey");
    const pem = resp.data?.public_key_pem || "";
    if (!pem) throw new Error("RSA pubkey response missing public_key_pem");
    const der = pemSpkiToDer(pem);
    const cryptoKey = await crypto.subtle.importKey(
      "spki",
      der,
      { name: "RSA-OAEP", hash: "SHA-256" },
      false,
      ["encrypt"],
    );
    return { fingerprint16: resp.data.fingerprint16 || "", cryptoKey };
  })().catch((e) => {
    // 让下一次再重新拉一次（避免一次网络抖动卡死所有后续登录）。
    _pubkeyPromise = null;
    throw e;
  });
  return _pubkeyPromise;
}

/**
 * RSA-OAEP(SHA-256) 加密明文密码 → 返回 base64 ASCII 字符串。
 *
 * 安全合规要求（与 B1 用例同步）：
 *   - **不返回明文**，明文仅在调用栈上短暂存在；
 *   - **只打指纹**：``console.debug`` 只输出 ``ciphertext sha-256 前 16 hex``
 *     + ``ciphertext.length``，绝不打明文 / 完整密文 / 私钥；
 *   - **失败抛错**：浏览器不支持 ``crypto.subtle`` / 公钥 import 失败 / OAEP
 *     报错 → 抛 ``Error``，由调用方决定 UI 文案。
 */
export async function encryptPassword(plain: string): Promise<string> {
  if (typeof plain !== "string" || plain.length === 0) {
    throw new Error("encryptPassword: plain must be a non-empty string");
  }
  if (typeof window === "undefined" || !window.crypto?.subtle) {
    throw new Error("Web Crypto API unavailable; refusing to send plaintext password");
  }
  const { fingerprint16, cryptoKey } = await getImportedPubkey();
  const data = new TextEncoder().encode(plain);
  const ctBuf = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, cryptoKey, data);
  const b64 = arrayBufferToBase64(ctBuf);

  // 只打指纹：前 16 hex 的 sha256(ciphertext)，加 ciphertext.length 用于
  // 排查"前端是不是真的在发密文"。绝不输出明文。
  try {
    const ctFp = (await _sha256Hex(ctBuf)).slice(0, 16);
    // eslint-disable-next-line no-console
    console.debug(`${_SUBTLE_DEBUG_TAG} encrypt done`, {
      pubkey_fp: fingerprint16,
      ct_fp16: ctFp,
      ct_b64_len: b64.length,
    });
  } catch {
    /* 日志失败不影响正常路径 */
  }

  return b64;
}

export const useAuth = create<AuthState>()((set) => ({
  me: null,
  initialized: false,
  loading: true,

  init: async () => {
    try {
      const resp = await api.get<MeOut>("/auth/me");
      set({ me: resp.data, initialized: true, loading: false });
    } catch {
      set({ me: null, initialized: true, loading: false });
    }
  },

  registerStart: async (email: string) => {
    await api.post("/auth/register/start", { email: email.trim() });
  },

  registerVerify: async (
    email: string,
    code: string,
    password: string,
    username?: string,
  ) => {
    const ciphertext = await encryptPassword(password);
    const body: Record<string, string> = {
      email: email.trim(),
      code: code.trim(),
      password: ciphertext,
    };
    const nick = (username ?? "").trim();
    if (nick) body.username = nick;
    const resp = await api.post<MeOut>("/auth/register/verify", body);
    set({ me: resp.data, initialized: true, loading: false });
    return resp.data;
  },

  login: async (email: string, password: string) => {
    const ciphertext = await encryptPassword(password);
    const resp = await api.post<MeOut>("/auth/login", {
      email: email.trim(),
      password: ciphertext,
    });
    set({ me: resp.data, initialized: true, loading: false });
    return resp.data;
  },

  logout: async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      set({ me: null, initialized: true, loading: false });
    }
  },

  passwordResetStart: async (email: string) => {
    await api.post("/auth/password-reset/start", { email: email.trim() });
  },

  passwordResetConfirm: async (token: string, newPassword: string) => {
    const ciphertext = await encryptPassword(newPassword);
    await api.post("/auth/password-reset/confirm", {
      token: token.trim(),
      new_password: ciphertext,
    });
  },
}));
