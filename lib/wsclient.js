/**
 * 极简 WebSocket 客户端（零依赖）。
 *
 * 为什么不用 `ws` 包：本插件以 `link:` 方式装进 profile，解析路径从包的真实
 * 目录出发，拿不到 profile 的 node_modules；也不想为 200 行协议再引入依赖。
 *
 * 只实现 wremote 用到的那一小撮：客户端握手、文本/二进制帧、ping/pong、close、
 * 分片续帧。不做扩展协商，不做压缩。
 */
import * as crypto from "node:crypto";
import * as http from "node:http";
import * as https from "node:https";

const OP_CONT = 0x0;
const OP_TEXT = 0x1;
const OP_BINARY = 0x2;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

function encodeFrame(opcode, payload) {
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.alloc(2);
    header[1] = 0x80 | len;
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[1] = 0x80 | 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[1] = 0x80 | 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  header[0] = 0x80 | opcode;
  const mask = crypto.randomBytes(4);
  for (let i = 0; i < len; i += 1) {
    payload[i] ^= mask[i & 3];
  }
  return Buffer.concat([header, mask, payload]);
}

/**
 * 建立到 ws://host:port/path 的连接。
 * @param {string} url
 * @param {{ onOpen?: Function, onText?: Function, onBinary?: Function, onClose?: Function }} handlers
 * @returns {{ sendText: Function, close: Function, get state(): string }}
 */
export function connectWebSocket(url, handlers = {}) {
  const target = new URL(url);
  const secure = target.protocol === "wss:";
  const key = crypto.randomBytes(16).toString("base64");
  const state = { value: "connecting" };
  let socket = null;
  let closedByUs = false;
  let buffer = Buffer.alloc(0);
  let fragments = [];
  let fragmentOpcode = 0;

  const req = (secure ? https : http).request({
    host: target.hostname,
    port: Number(target.port || (secure ? 443 : 80)),
    path: `${target.pathname || "/"}${target.search || ""}`,
    method: "GET",
    headers: {
      Connection: "Upgrade",
      Upgrade: "websocket",
      "Sec-WebSocket-Version": "13",
      "Sec-WebSocket-Key": key,
    },
  });

  req.setTimeout(0);
  // 第三个参数 head 是握手响应之后已经读进 Node 缓冲的字节。丢掉它解析就会从
  // 一帧的中间开始 —— 表现为收到 0 字节 / 十几字节的垃圾帧，之后整条流全错位。
  req.on("upgrade", (res, sock, head) => {
    const accept = crypto
      .createHash("sha1")
      .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    if (res.headers["sec-websocket-accept"] !== accept) {
      state.value = "closed";
      try { sock.destroy(); } catch { /* ignore */ }
      handlers.onClose?.({ code: 1002, reason: "bad handshake" });
      return;
    }
    socket = sock;
    state.value = "open";
    socket.setNoDelay(true);
    socket.on("data", onData);
    if (Buffer.isBuffer(head) && head.length > 0) onData(head);
    socket.on("close", () => {
      state.value = "closed";
      if (!closedByUs) handlers.onClose?.({ code: 1006, reason: "socket closed" });
    });
    socket.on("error", (error) => {
      state.value = "closed";
      handlers.onClose?.({ code: 1006, reason: String(error?.message ?? error) });
    });
    handlers.onOpen?.();
  });
  req.on("error", (error) => {
    state.value = "closed";
    handlers.onClose?.({ code: 1006, reason: String(error?.message ?? error) });
  });
  req.end();

  function onData(chunk) {
    buffer = buffer.length === 0 ? chunk : Buffer.concat([buffer, chunk]);
    for (;;) {
      if (buffer.length < 2) return;
      const b0 = buffer[0];
      const b1 = buffer[1];
      const fin = (b0 & 0x80) !== 0;
      const opcode = b0 & 0x0f;
      const masked = (b1 & 0x80) !== 0;
      let len = b1 & 0x7f;
      let offset = 2;
      if (len === 126) {
        if (buffer.length < 4) return;
        len = buffer.readUInt16BE(2);
        offset = 4;
      } else if (len === 127) {
        if (buffer.length < 10) return;
        len = Number(buffer.readBigUInt64BE(2));
        offset = 10;
      }
      let maskKey = null;
      if (masked) {
        if (buffer.length < offset + 4) return;
        maskKey = buffer.subarray(offset, offset + 4);
        offset += 4;
      }
      if (buffer.length < offset + len) return;
      let payload = buffer.subarray(offset, offset + len);
      if (maskKey !== null) {
        payload = Buffer.from(payload);
        for (let i = 0; i < payload.length; i += 1) payload[i] ^= maskKey[i & 3];
      }
      buffer = buffer.subarray(offset + len);

      if (opcode === OP_PING) {
        try { socket?.write(encodeFrame(OP_PONG, Buffer.from(payload))); } catch { /* ignore */ }
        continue;
      }
      if (opcode === OP_PONG) continue;
      if (opcode === OP_CLOSE) {
        try { socket?.write(encodeFrame(OP_CLOSE, Buffer.alloc(0))); } catch { /* ignore */ }
        try { socket?.destroy(); } catch { /* ignore */ }
        state.value = "closed";
        handlers.onClose?.({ code: 1000, reason: "peer close" });
        return;
      }
      if (opcode === OP_CONT) {
        fragments.push(payload);
        if (!fin) continue;
        payload = Buffer.concat(fragments);
        fragments = [];
        dispatch(fragmentOpcode, payload);
        continue;
      }
      if (!fin) {
        fragmentOpcode = opcode;
        fragments = [payload];
        continue;
      }
      dispatch(opcode, payload);
    }
  }

  function dispatch(opcode, payload) {
    if (opcode === OP_BINARY) {
      handlers.onBinary?.(payload);
    } else if (opcode === OP_TEXT) {
      handlers.onText?.(payload.toString("utf8"));
    }
  }

  return {
    get state() {
      return state.value;
    },
    sendText(text) {
      if (state.value !== "open" || socket === null) return false;
      try {
        socket.write(encodeFrame(OP_TEXT, Buffer.from(text, "utf8")));
        return true;
      } catch {
        return false;
      }
    },
    close() {
      closedByUs = true;
      state.value = "closed";
      if (socket !== null) {
        try { socket.write(encodeFrame(OP_CLOSE, Buffer.alloc(0))); } catch { /* ignore */ }
        try { socket.end(); } catch { /* ignore */ }
        try { socket.destroy(); } catch { /* ignore */ }
      }
    },
  };
}
