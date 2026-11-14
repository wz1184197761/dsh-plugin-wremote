/**
 * dsh-wremote —— 把本机 wremote 投屏服务接进 DSH。
 *
 * wremote（C:\Users\11841\apps\wremote\server.py）对外只有一个 HTML 页面和一条
 * WebSocket：`/` 出页面，ws 双向收发。下行是裸 JPEG 二进制帧（偶尔夹一条
 * `{"t":"state",...}` JSON），上行是 `{"t":"geom"|"m"|"k"|"text"|"ping"}`。
 *
 * 本插件不重写采集、提顶、几何任何一环，只做一层壳：
 *   1) 用一条常驻（惰性建立）的 ws 连接订阅 JPEG 帧，缓存最新一帧；
 *   2) 把最新帧重新封成 multipart/x-mixed-replace 供侧栏 <img> 显示；
 *   3) 把面板上的点击/滚动/键盘翻译成 wremote 的上行消息发回去；
 *   4) 给 agent 一组工具（状态、抓图、点击、滚动、键盘、输入文本）。
 *
 * geom 契约：上游收到 `{"t":"geom","w":W,"h":H}` 会把源窗口客户区改成 W×H，
 * 之后推下来的帧就是这个尺寸 —— 「客户端显示区多大，源窗口就多大」，页面因此
 * 能 1:1 绘制、零缩放。state 是全连接共享的，所以上游要选一条连接当 `geom_owner`：
 * 首个发 geom 的连接是 owner，它断开后才释放给后来者；带 `take:true` 的 geom 可以
 * 主动接管（全屏时面板才是用户正在看的那块屏，不能被残留的浏览器页面挡住）。
 *
 * 本插件只在**面板全屏**时才发 geom：那时显示区就是整块屏幕，按屏幕尺寸重建源
 * 窗口，全屏里就不再等比留边（黑边）。普通面板不发 —— 侧栏只有几百像素宽，
 * 把用户桌面上的微信窗口缩成一条，代价远大于收益。退出全屏时把进入前的尺寸
 * 发回去复原。所以 config 里 `sendGeom` 现在是默认开的，关掉它就退回
 * 「面板只等比缩放、全屏会留边」的老行为。
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { connectWebSocket } from "./lib/wsclient.js";

export const name = "wremote";
export const inject = ["webServer", "agents"];

const API = "/api/wremote";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const BOUNDARY = "wremote-frame";
const MAX_FPS_SAMPLES = 240;
/** 按下到抬起之间的间隔：服务端在 down 里还要 sleep 0.02（重排）+ 0.09（激活），
 *  抬起排在这之后处理才不会被 Qt 当成同一次激活点击吞掉。 */
const CLICK_HOLD_MS = 120;

const DEFAULTS = {
  host: "127.0.0.1",
  port: 8098,
  path: "/",
  autoConnect: true,
  reconnectMs: 4000,
  streamRetryMs: 1200,
  firstFrameTimeoutMs: 6000,
  /** 没有任何观众/工具在用之后，隔多久断开上游连接。 */
  idleGraceMs: 30000,
  /** 面板全屏时是否把屏幕尺寸发给上游、让源窗口按它重建（见文件头 geom 契约）。
   *  关掉 = 只等比缩放，全屏会留黑边。 */
  sendGeom: true,
};

function safeGet(ctx, key) {
  try {
    if (ctx === undefined || ctx === null || typeof ctx.get !== "function") return undefined;
    return ctx.get(key);
  } catch {
    return undefined;
  }
}

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  try {
    res.writeHead(status, {
      "content-type": "application/json; charset=utf-8",
      "content-length": Buffer.byteLength(text),
      "cache-control": "no-store",
    });
    res.end(text);
  } catch {
    /* 连接已经断了，忽略 */
  }
}

function readBody(req, limit = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error(`body 超过 ${limit} 字节`));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function toJsonSchema(schema) {
  const properties = {};
  for (const [key, spec] of Object.entries(schema?.properties ?? {})) {
    const { required: _ignored, ...rest } = spec ?? {};
    properties[key] = rest;
  }
  const out = { type: "object", properties, additionalProperties: false };
  if (Array.isArray(schema?.required) && schema.required.length > 0) {
    out.required = schema.required.filter((key) => key in properties);
  }
  return out;
}

/**
 * 从 JPEG 里读出真实尺寸。
 * 面板要把点击坐标从「显示框像素」换算成「帧像素」，而 wremote 的帧尺寸会随
 * 窗口 resize 变（1876x1032 是当前值，但不是常量），所以必须每帧现读。
 */
function jpegSize(buf) {
  if (!Buffer.isBuffer(buf) || buf.length < 4) return null;
  if (buf[0] !== 0xff || buf[1] !== 0xd8) return null;
  let i = 2;
  while (i + 9 < buf.length) {
    if (buf[i] !== 0xff) {
      i += 1;
      continue;
    }
    const marker = buf[i + 1];
    if (marker === 0xff) {
      i += 1;
      continue;
    }
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
      i += 2;
      continue;
    }
    const len = buf.readUInt16BE(i + 2);
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      return { h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7) };
    }
    i += 2 + len;
  }
  return null;
}

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

export function apply(ctx, rawConfig = {}) {
  const config = { ...DEFAULTS };
  for (const [key, value] of Object.entries(rawConfig ?? {})) {
    if (value !== undefined && value !== null) config[key] = value;
  }
  const log = (...args) => console.log("[wremote]", ...args);

  const webServer = safeGet(ctx, "webServer");
  if (webServer === undefined) {
    throw new Error("dsh-wremote: 需要 webServer 服务（只在 Web composition 可用）");
  }
  const agents = safeGet(ctx, "agents");

  const hub = {
    /** @type {ReturnType<typeof connectWebSocket> | undefined} */
    conn: undefined,
    connecting: false,
    wantOpen: false,
    refs: 0,
    idleTimer: undefined,
    reconnectTimer: undefined,
    latest: null,
    latestAt: 0,
    size: null,
    seq: 0,
    frames: 0,
    frameTimes: [],
    hint: "",
    src: "",
    listeners: new Set(),
    lastError: "",
  };

  const upstreamUrl = () => `ws://${config.host}:${config.port}${config.path || "/"}`;

  function notifyListeners(buf) {
    for (const listener of hub.listeners) {
      try {
        listener(buf);
      } catch (error) {
        log("listener 抛错（已忽略）:", error?.message ?? error);
      }
    }
  }

  function onFrame(buf) {
    hub.latest = buf;
    hub.latestAt = Date.now();
    hub.seq += 1;
    hub.frames += 1;
    const size = jpegSize(buf);
    if (size !== null) hub.size = size;
    hub.frameTimes.push(hub.latestAt);
    if (hub.frameTimes.length > MAX_FPS_SAMPLES) hub.frameTimes.shift();
    notifyListeners(buf);
  }

  function currentFps() {
    if (hub.frameTimes.length < 2) return 0;
    const now = Date.now();
    const recent = hub.frameTimes.filter((t) => now - t <= 2000);
    if (recent.length < 2) return 0;
    const span = (recent[recent.length - 1] - recent[0]) / 1000;
    if (span <= 0) return 0;
    return Math.round(((recent.length - 1) / span) * 10) / 10;
  }

  function scheduleReconnect() {
    if (!hub.wantOpen || hub.reconnectTimer !== undefined) return;
    hub.reconnectTimer = setTimeout(() => {
      hub.reconnectTimer = undefined;
      openUpstream();
    }, Math.max(300, config.reconnectMs));
    hub.reconnectTimer.unref?.();
  }

  function closeUpstream(reason) {
    hub.wantOpen = false;
    if (hub.reconnectTimer !== undefined) {
      clearTimeout(hub.reconnectTimer);
      hub.reconnectTimer = undefined;
    }
    const conn = hub.conn;
    hub.conn = undefined;
    hub.connecting = false;
    if (conn !== undefined) {
      try {
        conn.close();
      } catch {
        /* ignore */
      }
    }
    if (reason) log(`上游断开：${reason}`);
  }

  function openUpstream() {
    if (hub.conn !== undefined || hub.connecting) return;
    if (!hub.wantOpen) return;
    hub.connecting = true;
    const url = upstreamUrl();
    const conn = connectWebSocket(url, {
      onOpen() {
        hub.connecting = false;
        hub.lastError = "";
        log(`已连接 ${url}`);
        // 主动握手：服务端靠 pong 证明上行还通（隧道半开时 readyState 仍是 OPEN）
        conn.sendText(JSON.stringify({ t: "ping" }));
      },
      onBinary(payload) {
        onFrame(payload);
      },
      onText(text) {
        try {
          const msg = JSON.parse(text);
          if (msg?.t === "state") {
            hub.src = String(msg.src ?? "");
            hub.hint = String(msg.hint ?? "");
          } else if (msg?.t === "pong") {
            hub.hint = hub.hint || "";
          }
        } catch {
          /* 非 JSON 文本，忽略 */
        }
      },
      onClose(info) {
        hub.connecting = false;
        if (hub.conn === conn) hub.conn = undefined;
        const why = info?.reason ? String(info.reason) : `code=${info?.code}`;
        hub.lastError = why;
        if (hub.wantOpen) {
          log(`上游关闭（${why}），${Math.round(config.reconnectMs)}ms 后重连`);
          scheduleReconnect();
        }
      },
    });
    hub.conn = conn;
    // 握手失败时 wsclient 只回调 onClose，不会把 conn 交出来；用 connecting 兜底
    setTimeout(() => {
      hub.connecting = false;
    }, 5000).unref?.();
  }

  /** 借一次上游连接；返回释放函数（幂等）。 */
  function acquire(reason) {
    hub.refs += 1;
    if (hub.idleTimer !== undefined) {
      clearTimeout(hub.idleTimer);
      hub.idleTimer = undefined;
    }
    hub.wantOpen = true;
    if (hub.conn === undefined) openUpstream();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      hub.refs = Math.max(0, hub.refs - 1);
      if (hub.refs > 0) return;
      hub.idleTimer = setTimeout(() => {
        hub.idleTimer = undefined;
        if (hub.refs === 0) closeUpstream(`${reason} 之后无观众，主动断开`);
      }, Math.max(1000, config.idleGraceMs));
      hub.idleTimer.unref?.();
    };
  }

  function sendInput(payload) {
    const conn = hub.conn;
    if (conn === undefined || conn.state !== "open") return false;
    return conn.sendText(JSON.stringify(payload));
  }

  /** 把面板的显示尺寸交给上游，让源窗口按它重建（见文件头 geom 契约）。
   *  `take` 为真时顺带接管 owner —— 全屏是用户明确要求「按我这块屏显示」的动作，
   *  不该因为某个残留的浏览器页面还连着就被挡在门外。 */
  function sendGeom(w, h, take = false) {
    const conn = hub.conn;
    if (conn === undefined || conn.state !== "open") return false;
    const msg = take ? { t: "geom", w, h, take: true } : { t: "geom", w, h };
    return conn.sendText(JSON.stringify(msg));
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function waitFirstFrame(timeoutMs) {
    if (hub.latest !== null) return Promise.resolve(true);
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        hub.listeners.delete(onFrameOnce);
        resolve(false);
      }, Math.max(500, timeoutMs));
      const onFrameOnce = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        hub.listeners.delete(onFrameOnce);
        resolve(true);
      };
      hub.listeners.add(onFrameOnce);
    });
  }

  // ---------------------------------------------------------------- 输入动作

  const BUTTONS = {
    left: { down: "down", up: "up" },
    right: { down: "rdown", up: "rup" },
    middle: { down: "md", up: "mu" },
  };

  async function click(x, y, button = "left", holdMs = CLICK_HOLD_MS) {
    const pair = BUTTONS[button] ?? BUTTONS.left;
    if (!sendInput({ t: "m", a: pair.down, x, y })) return { ok: false, error: "上游未连接" };
    await sleep(Math.max(40, holdMs));
    const ok = sendInput({ t: "m", a: pair.up, x, y });
    return { ok, error: ok ? undefined : "抬起消息发送失败（连接已断）" };
  }

  async function doubleClick(x, y, button = "left") {
    const first = await click(x, y, button);
    if (!first.ok) return first;
    await sleep(90);
    return click(x, y, button);
  }

  // ------------------------------------------------------------------ 路由

  const route = (pathname, method, handler) =>
    ctx.effect(
      () =>
        webServer.register({
          kind: "exact",
          path: `${API}${pathname}`,
          async handler(req, res) {
            if (req.method !== method) {
              sendJson(res, 405, { ok: false, error: `需要 ${method}` });
              return;
            }
            try {
              await handler({ req, res });
            } catch (error) {
              if (!res.headersSent) {
                sendJson(res, 500, {
                  ok: false,
                  code: error?.code ?? "error",
                  error: String(error?.message ?? error),
                });
              } else {
                try {
                  res.end();
                } catch {
                  /* ignore */
                }
              }
            }
          },
        }),
      `wremote: ${method} ${pathname}`,
    );

  const statusOf = () => ({
    ok: true,
    upstream: {
      url: upstreamUrl(),
      connected: hub.conn !== undefined && hub.conn.state === "open",
      refs: hub.refs,
      lastError: hub.lastError,
    },
    stream: {
      seq: hub.seq,
      frames: hub.frames,
      fps: currentFps(),
      size: hub.size,
      lastFrameAt: hub.latestAt || null,
      ageMs: hub.latestAt ? Date.now() - hub.latestAt : null,
      src: hub.src,
      hint: hub.hint,
    },
    config: {
      host: config.host,
      port: config.port,
      path: config.path,
      autoConnect: config.autoConnect,
      streamRetryMs: config.streamRetryMs,
      sendGeom: config.sendGeom,
    },
  });

  route("/status", "GET", async ({ res }) => {
    sendJson(res, 200, statusOf());
  });

  /** 单帧快照（JPEG）。给 <img> 兜底和调试用，不含 MJPEG 长连接。 */
  route("/frame.jpg", "GET", async ({ res }) => {
    const release = acquire("frame.jpg");
    try {
      if (hub.latest === null) await waitFirstFrame(config.firstFrameTimeoutMs);
      if (hub.latest === null) {
        sendJson(res, 503, { ok: false, error: "还没收到任何帧（8098 上有客户端在跑吗？）" });
        return;
      }
      res.writeHead(200, {
        "content-type": "image/jpeg",
        "content-length": hub.latest.length,
        "cache-control": "no-store",
      });
      res.end(hub.latest);
    } finally {
      release();
    }
  });

  /**
   * MJPEG 流。每个观众持有一份 acquire()，全部断开后按 idleGraceMs 释放上游。
   * 先补发缓存帧，避免 <img> 首屏空白等到下一帧才有内容。
   */
  route("/stream", "GET", async ({ req, res }) => {
    const release = acquire("流");
    res.writeHead(200, {
      "content-type": `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      "cache-control": "no-store, no-cache, must-revalidate",
      pragma: "no-cache",
      connection: "close",
      "x-accel-buffering": "no",
    });
    let closed = false;
    let inFlight = false;
    let pending = null;

    const writeFrame = (buf) => {
      if (closed) return;
      try {
        res.write(
          Buffer.concat([
            Buffer.from(
              `--${BOUNDARY}\r\ncontent-type: image/jpeg\r\ncontent-length: ${buf.length}\r\n\r\n`,
              "utf8",
            ),
            buf,
            Buffer.from("\r\n", "utf8"),
          ]),
        );
      } catch {
        closed = true;
      }
    };

    const onFrame = (buf) => {
      // 慢观众不能把内存堆爆：只保留最新一帧，写不进去就丢弃中间的。
      if (inFlight) {
        pending = buf;
        return;
      }
      inFlight = true;
      writeFrame(buf);
      inFlight = false;
      if (pending !== null) {
        const next = pending;
        pending = null;
        writeFrame(next);
      }
    };

    const cleanup = () => {
      if (closed) return;
      closed = true;
      hub.listeners.delete(onFrame);
      try {
        res.end();
      } catch {
        /* ignore */
      }
      release();
    };

    res.on("close", cleanup);
    res.on("error", cleanup);
    req.on("error", cleanup);

    hub.listeners.add(onFrame);
    if (hub.latest !== null) writeFrame(hub.latest);
    else if (!hub.wantOpen) {
      /* acquire 已经拉起连接，这里只等帧 */
    }
  });

  /**
   * 输入回传。面板只发「帧像素」坐标，`m` 消息里的 x/y 就是帧像素，
   * 服务端自己按 frame_size() 换算成屏幕坐标 —— 插件不做任何坐标数学。
   */
  route("/input", "POST", async ({ req, res }) => {
    const raw = await readBody(req);
    let body;
    try {
      body = raw.length === 0 ? {} : JSON.parse(raw);
    } catch (error) {
      sendJson(res, 400, { ok: false, error: `body 不是合法 JSON: ${String(error?.message ?? error)}` });
      return;
    }
    const release = acquire("输入");
    try {
      if (hub.conn === undefined || hub.conn.state !== "open") {
        await sleep(Math.min(1200, config.reconnectMs));
      }
      if (hub.conn === undefined || hub.conn.state !== "open") {
        sendJson(res, 503, { ok: false, error: "上游未连接（8098 服务没起？）" });
        return;
      }
      const action = String(body.action ?? "");
      if (action === "click" || action === "double") {
        const x = Number(body.x);
        const y = Number(body.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
          sendJson(res, 400, { ok: false, error: "click 需要数字 x / y" });
          return;
        }
        const result =
          action === "double"
            ? await doubleClick(x, y, String(body.button ?? "left"))
            : await click(x, y, String(body.button ?? "left"));
        sendJson(res, result.ok ? 200 : 503, { ...result, x, y, action });
        return;
      }
      // 拖动要拆成 down / move… / up 三段，不能复用 click（那会把 mid-drag 的
      // 位移夹在服务端的一次 hold 里）。服务端在 down 里锁 drag 基准点，靠后续
      // move 的位移 1:1 平移窗口，所以顺序不能乱、move 必须在 down 之后。
      if (action === "move" || action === "down" || action === "up") {
        const x = Number(body.x);
        const y = Number(body.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
          sendJson(res, 400, { ok: false, error: `${action} 需要数字 x / y` });
          return;
        }
        const pair = BUTTONS[String(body.button ?? "left")] ?? BUTTONS.left;
        const a = action === "move" ? "move" : action === "down" ? pair.down : pair.up;
        const ok = sendInput({ t: "m", a, x, y });
        sendJson(res, ok ? 200 : 503, { ok, x, y, action, a });
        return;
      }
      if (action === "scroll") {
        const x = Number(body.x ?? 0);
        const y = Number(body.y ?? 0);
        const delta = Math.round(Number(body.delta ?? 0));
        if (!Number.isFinite(delta) || delta === 0) {
          sendJson(res, 400, { ok: false, error: "scroll 需要非零数字 delta" });
          return;
        }
        const ok = sendInput({ t: "m", a: "wheel", x, y, d: delta });
        sendJson(res, ok ? 200 : 503, { ok, x, y, delta, action });
        return;
      }
      if (action === "key") {
        const vk = Math.round(Number(body.vk));
        if (!Number.isFinite(vk) || vk <= 0) {
          sendJson(res, 400, { ok: false, error: "key 需要正整数 vk" });
          return;
        }
        const okDown = sendInput({ t: "k", vk, up: false });
        await sleep(30);
        const okUp = sendInput({ t: "k", vk, up: true });
        sendJson(res, okDown && okUp ? 200 : 503, { ok: okDown && okUp, vk, action });
        return;
      }
      if (action === "text") {
        const text = String(body.text ?? "");
        if (text.length === 0) {
          sendJson(res, 400, { ok: false, error: "text 不能为空" });
          return;
        }
        const ok = sendInput({ t: "text", s: text });
        sendJson(res, ok ? 200 : 503, { ok, bytes: Buffer.byteLength(text), action });
        return;
      }
      sendJson(res, 400, { ok: false, error: `未知 action: ${action || "(空)"}` });
    } finally {
      release();
    }
  });

  /**
   * 面板协商几何：面板全屏时把屏幕尺寸发上来，让上游按它重建源窗口，
   * 全屏里就不再等比留边（见文件头 geom 契约）。
   *
   * 只认「物理像素」尺寸：上游的 client_rect / resize_client 都是物理像素，
   * 面板侧要乘 devicePixelRatio 再发，否则高分屏上算出来的窗口小于屏幕，
   * 缩放后画面发虚。
   *
   * 上游只让 geom_owner 真正 resize：如果桌面浏览器页面已经占着所有权，
   * 这里发过去只会被忽略（不报错），响应里 `ownerHint` 会说明。
   */
  route("/geom", "POST", async ({ req, res }) => {
    const raw = await readBody(req);
    let body;
    try {
      body = raw.length === 0 ? {} : JSON.parse(raw);
    } catch (error) {
      sendJson(res, 400, { ok: false, error: `body 不是合法 JSON: ${String(error?.message ?? error)}` });
      return;
    }
    if (config.sendGeom !== true) {
      sendJson(res, 200, {
        ok: false,
        skipped: true,
        error: "本插件的 sendGeom 已关（只等比缩放，全屏会留边）",
      });
      return;
    }
    const w = Math.round(Number(body.w));
    const h = Math.round(Number(body.h));
    const take = body.take === true;
    if (!Number.isFinite(w) || !Number.isFinite(h) || w < 200 || h < 200 || w > 4000 || h > 4000) {
      sendJson(res, 400, { ok: false, error: "geom 需要 200..4000 之间的整数 w / h" });
      return;
    }
    const release = acquire("几何");
    try {
      if (hub.conn === undefined || hub.conn.state !== "open") {
        await sleep(Math.min(1200, config.reconnectMs));
      }
      if (hub.conn === undefined || hub.conn.state !== "open") {
        sendJson(res, 503, { ok: false, error: "上游未连接（8098 服务没起？）" });
        return;
      }
      const ok = sendGeom(w, h, take);
      sendJson(res, ok ? 200 : 503, {
        ok,
        w,
        h,
        take,
        // 源窗口重建 + 首帧要一两秒，调用方等下一轮 /status 看 stream.size 更可靠
        ownerHint: take
          ? "已带 take 发出，上游会把 owner 改成这条连接"
          : "上游只让 owner 真正改窗口；要接管请带 take:true（面板全屏时就是这么发的）",
      });
    } finally {
      release();
    }
  });

  // ------------------------------------------------------------------ 工具

  async function grabFrame(agent) {
    const release = acquire("工具抓图");
    try {
      if (hub.latest === null) await waitFirstFrame(config.firstFrameTimeoutMs);
      const buf = hub.latest;
      if (buf === null) return { ok: false, error: "6 秒内没收到任何帧，8098 上可能没有客户端在拉流" };
      const dir = path.join(os.tmpdir(), "dsh-wremote");
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, `frame-${stamp()}.jpg`);
      fs.writeFileSync(file, buf);
      // 图片先存进 DSH 附件库，结果里只放 attachment 引用。直接内联 base64 的
      // image 块会写进会话历史，下游 LLM 适配器按 block.attachment 取图时会崩，
      // 之后每轮都失败。
      const attachments = safeGet(agent?.ctx, "attachments") ?? safeGet(ctx, "attachments");
      let attachment;
      if (attachments !== undefined && typeof attachments.saveImage === "function") {
        try {
          attachment = await attachments.saveImage({
            data: new Uint8Array(buf),
            mediaType: "image/jpeg",
            name: path.basename(file),
          });
        } catch (error) {
          log("saveImage 失败，只返回路径:", error?.message ?? error);
        }
      }
      return {
        ok: true,
        path: file,
        bytes: buf.length,
        size: hub.size,
        seq: hub.seq,
        ...(attachment !== undefined ? { attachment } : {}),
      };
    } finally {
      release();
    }
  }

  function requireFrameSize() {
    if (hub.size === null) return undefined;
    return hub.size;
  }

  const TOOL_SPECS = [
    {
      name: "wremote_status",
      description:
        "查看本机微信投屏服务（wremote，默认 127.0.0.1:8098）的连接状态：是否连上、收帧序号、当前帧率、画面尺寸、最近一次服务端提示。排查「投屏没画面/点了没反应」时先看这个。",
      schema: { properties: {}, required: [] },
      async run() {
        return statusOf();
      },
    },
    {
      name: "wremote_screenshot",
      description:
        "抓取微信投屏的当前画面（JPEG）并作为图片返回，同时给出帧尺寸与保存路径。适合看清界面后再决定点哪里。",
      schema: { properties: {}, required: [] },
      async run(_input, _projectRoot, agent) {
        return grabFrame(agent);
      },
      render(_args, value) {
        if (value?.ok !== true) return [{ type: "text", text: JSON.stringify(value) }];
        const size = value.size ? `${value.size.w}x${value.size.h}` : "尺寸未知";
        const text = {
          type: "text",
          text: `微信投屏画面已抓取（${size}，${value.bytes} 字节），保存在 ${value.path}`,
        };
        if (value.attachment?.attachmentId === undefined) {
          return [{ ...text, text: `${text.text}。附件库不可用，请用 read_image 打开该路径查看。` }];
        }
        return [text, { type: "image", attachment: value.attachment }];
      },
    },
    {
      name: "wremote_click",
      description:
        "在微信投屏画面上点一下。坐标 x / y 是**帧像素**（以 wremote_screenshot 返回的 size 为基准，左上角为原点），不是屏幕坐标也不是面板像素。服务端会在按下前把微信主窗顶到最前，所以即使聊天窗/文章窗压在上面也能点中。",
      schema: {
        properties: {
          x: { type: "number", description: "帧像素 X，0 到帧宽-1" },
          y: { type: "number", description: "帧像素 Y，0 到帧高-1" },
          button: {
            type: "string",
            enum: ["left", "right", "middle"],
            description: "鼠标键，默认 left",
          },
          double: { type: "boolean", description: "是否双击，默认 false" },
        },
        required: ["x", "y"],
      },
      async run(input) {
        const x = Number(input.x);
        const y = Number(input.y);
        const release = acquire("工具点击");
        try {
          if (hub.conn === undefined || hub.conn.state !== "open") await sleep(800);
          if (hub.conn === undefined || hub.conn.state !== "open") {
            return { ok: false, error: "上游未连接，8098 服务没起？" };
          }
          const size = requireFrameSize();
          const result = input.double
            ? await doubleClick(x, y, String(input.button ?? "left"))
            : await click(x, y, String(input.button ?? "left"));
          return { ...result, x, y, frameSize: size ?? null };
        } finally {
          release();
        }
      },
    },
    {
      name: "wremote_scroll",
      description:
        "在微信投屏画面上滚动滚轮。x / y 是帧像素（决定滚到哪个控件上），delta 是滚轮增量：正数向上、负数向下，一格通常 120（Windows WHEEL_DELTA 单位）。",
      schema: {
        properties: {
          x: { type: "number", description: "帧像素 X" },
          y: { type: "number", description: "帧像素 Y" },
          delta: { type: "integer", description: "滚轮增量，正数向上、负数向下，一格 120" },
        },
        required: ["x", "y", "delta"],
      },
      async run(input) {
        const release = acquire("工具滚动");
        try {
          if (hub.conn === undefined || hub.conn.state !== "open") await sleep(800);
          if (hub.conn === undefined || hub.conn.state !== "open") {
            return { ok: false, error: "上游未连接，8098 服务没起？" };
          }
          const ok = sendInput({
            t: "m",
            a: "wheel",
            x: Number(input.x),
            y: Number(input.y),
            d: Math.round(Number(input.delta)),
          });
          return { ok, x: Number(input.x), y: Number(input.y), delta: Math.round(Number(input.delta)) };
        } finally {
          release();
        }
      },
    },
    {
      name: "wremote_type",
      description:
        "把一段文本输入到当前微信焦点控件里（走服务端的 SendInput unicode 通道，中文/emoji 都可以，不依赖输入法）。发送前请先用 wremote_click 把光标点进输入框。",
      schema: {
        properties: { text: { type: "string", description: "要输入的文本" } },
        required: ["text"],
      },
      async run(input) {
        const text = String(input.text ?? "");
        if (text.length === 0) return { ok: false, error: "text 不能为空" };
        const release = acquire("工具输入");
        try {
          if (hub.conn === undefined || hub.conn.state !== "open") await sleep(800);
          if (hub.conn === undefined || hub.conn.state !== "open") {
            return { ok: false, error: "上游未连接，8098 服务没起？" };
          }
          const ok = sendInput({ t: "text", s: text });
          return { ok, chars: [...text].length, bytes: Buffer.byteLength(text) };
        } finally {
          release();
        }
      },
    },
    {
      name: "wremote_key",
      description:
        "按一个虚拟键（Windows VK 码），例如回车 13、Esc 27、退格 8、Tab 9、方向键 37-40、F5 116。会先发按下再发抬起。组合键请用 wremote_hotkey 或分多次调用。",
      schema: {
        properties: {
          vk: { type: "integer", description: "Windows 虚拟键码，例如 13=Enter、27=Esc" },
          times: { type: "integer", description: "连按次数，默认 1" },
        },
        required: ["vk"],
      },
      async run(input) {
        const vk = Math.round(Number(input.vk));
        const times = Math.max(1, Math.min(20, Math.round(Number(input.times ?? 1))));
        if (!Number.isFinite(vk) || vk <= 0) return { ok: false, error: "vk 需要正整数" };
        const release = acquire("工具按键");
        try {
          if (hub.conn === undefined || hub.conn.state !== "open") await sleep(800);
          if (hub.conn === undefined || hub.conn.state !== "open") {
            return { ok: false, error: "上游未连接，8098 服务没起？" };
          }
          let ok = true;
          for (let i = 0; i < times; i += 1) {
            ok = sendInput({ t: "k", vk, up: false }) && ok;
            await sleep(30);
            ok = sendInput({ t: "k", vk, up: true }) && ok;
            if (i < times - 1) await sleep(60);
          }
          return { ok, vk, times };
        } finally {
          release();
        }
      },
    },
  ];

  function installTools(agent) {
    const scopedTools = safeGet(agent.ctx, "tools");
    if (scopedTools === undefined || typeof scopedTools.register !== "function") return undefined;
    const disposers = [];
    try {
      for (const spec of TOOL_SPECS) {
        disposers.push(
          scopedTools.register({
            name: spec.name,
            description: spec.description,
            parameters: toJsonSchema(spec.schema),
            output: {
              schema: { type: "object", additionalProperties: true },
              render: spec.render ?? ((_args, value) => [{ type: "text", text: JSON.stringify(value) }]),
            },
            async execute(input) {
              try {
                return await spec.run(input ?? {}, undefined, this);
              } catch (error) {
                return { ok: false, error: String(error?.message ?? error) };
              }
            },
          }),
        );
      }
    } catch (error) {
      for (const dispose of disposers) {
        try {
          dispose();
        } catch {
          /* ignore */
        }
      }
      log("注册工具失败:", error?.message ?? error);
      return undefined;
    }
    return () => {
      for (const dispose of disposers) {
        try {
          dispose();
        } catch {
          /* ignore */
        }
      }
    };
  }

  const installed = new Map();
  const pendingInstall = new Set();

  async function maybeInstall(agent) {
    if (agent === undefined || agent === null || installed.has(agent)) return;
    if (pendingInstall.has(agent)) return;
    pendingInstall.add(agent);
    try {
      const dispose = installTools(agent);
      if (dispose !== undefined) installed.set(agent, dispose);
    } finally {
      pendingInstall.delete(agent);
    }
  }

  if (agents !== undefined && typeof agents.list === "function") {
    for (const agent of agents.list()) void maybeInstall(agent);
    ctx.on("agent/created", ({ agent }) => {
      void maybeInstall(agent);
    });
    ctx.on("agent/disposed", ({ agent }) => {
      pendingInstall.delete(agent);
      installed.get(agent)?.();
      installed.delete(agent);
    });
    ctx.effect(
      () => () => {
        for (const dispose of installed.values()) {
          try {
            dispose();
          } catch {
            /* ignore */
          }
        }
        installed.clear();
        pendingInstall.clear();
      },
      "wremote: scoped tools",
    );
  } else {
    log("没有 agents 服务，工具不注册（面板仍可用）");
  }

  if (config.autoConnect) {
    // 面板还没打开也先拉起来：DSH 启动后第一次点开侧栏就能立刻出画面。
    // 30 秒没人用会自己断开（idleGraceMs），不会长期占着 8098。
    acquire("autoconnect");
  }

  ctx.effect(
    () => () => {
      hub.listeners.clear();
      closeUpstream("插件卸载");
    },
    "wremote: upstream",
  );

  log(
    `已加载：上游 ${upstreamUrl()}，api ${API}，面板 ${config.autoConnect ? "自动连接" : "手动连接"}`,
  );
}