/**
 * dsh-wremote —— 浏览器半区。
 *
 * 注册两个座位：
 *   - `sidebar.panellist`：左侧栏图标入口
 *   - `main`：主列面板，里面是**微信投屏的实时画面**
 *
 * 画面来源：`<img src="/api/wremote/stream">` —— 同源 MJPEG，浏览器原生支持
 * multipart/x-mixed-replace，不需要 canvas、不需要解码、不需要在前端开 WebSocket。
 *
 * 输入回传：指针事件翻译成 `/api/wremote/input` 的 down / move / up / scroll，
 * 键盘翻译成 `key`，输入框内容翻译成 `text`。坐标一律是**帧像素**（以服务端
 * 推来的那一帧为基准），由宿主插件转发给 8098，服务端自己按 frame_size 换算屏幕
 * 坐标 —— 前端不做任何跨尺寸数学，避免「按页面比例算」那类整体偏移。
 *
 * 几何协商：**只有全屏时才发** `POST /api/wremote/geom`，把全屏元素的物理像素尺寸
 * 交给上游，让它把源窗口客户区改成同尺寸 —— 画面与屏幕同比例，全屏里就不留黑边。
 * 退出全屏时把进入前的尺寸发回去复原。普通面板不发：侧栏只有几百像素宽，
 * 把桌面微信窗口缩成一条的代价太大（完整契约见 index.js 文件头）。
 *
 * 样式全部走宿主主题 token（`--dsw-alias-*`），浅色/深色自动跟随。
 */

window.__ModuleLoader__.load({
  id: "@local/dsh-wremote",
  factory(require) {
    const React = require("react");

    /** 侧栏与主面板共用的 cell key。 */
    const PANEL_KEY = "wremote";
    /** 宿主路由前缀。 */
    const API = "/api/wremote";
    /** 语言命名空间。 */
    const NS = "wremote";

    const DICTS = {
      zh: {
        "nav.label": "投屏",
        "panel.title": "微信投屏",
        "panel.subtitle": "本机 wremote 单窗口画面，点击直接回传",
        "status.on": "已连接",
        "status.off": "未连接",
        "status.wait": "等待画面…",
        "action.snapshot": "存图",
        "action.fullscreen": "全屏",
        "action.exitFullscreen": "退出全屏",
        "action.fill": "撑满",
        "action.contain": "保持比例",
        "action.reconnect": "重连画面",
        "meta.fps": "帧率",
        "meta.size": "画面尺寸",
        "meta.src": "采集通道",
        "meta.hint": "服务端提示",
        "input.title": "输入文本",
        "input.placeholder": "输入中文/emoji，回车发送（先点画面把光标落到输入框）",
        "input.send": "发送",
        "input.clear": "清空",
        "keys.escHint": "按 Esc 或点右上角退出全屏",
        "keys.title": "常用键",
        "keys.hint": "点画面获得焦点后，方向键 / 回车 / Esc / Tab / 退格会直接发给远端。",
        "empty.title": "还没有画面",
        "empty.hint": "确认台式机的 wremote 服务在跑（127.0.0.1:8098）。画面会在连上后自动出现。",
      },
      en: {
        "nav.label": "Mirror",
        "panel.title": "WeChat mirror",
        "panel.subtitle": "Local wremote single-window view with click back-channel",
        "status.on": "Connected",
        "status.off": "Disconnected",
        "status.wait": "Waiting for frames…",
        "action.snapshot": "Save frame",
        "action.fullscreen": "Fullscreen",
        "action.exitFullscreen": "Exit fullscreen",
        "action.fill": "Fill",
        "action.contain": "Keep ratio",
        "action.reconnect": "Reconnect stream",
        "meta.fps": "FPS",
        "meta.size": "Frame size",
        "meta.src": "Capture path",
        "meta.hint": "Server hint",
        "input.title": "Send text",
        "input.placeholder": "Type text, press Enter to send (click the view first to focus the remote input)",
        "input.send": "Send",
        "input.clear": "Clear",
        "keys.escHint": "Press Esc or click the top-right button to exit fullscreen",
        "keys.title": "Keys",
        "keys.hint": "After clicking the view, arrows / Enter / Esc / Tab / Backspace go to the remote window.",
        "empty.title": "No frames yet",
        "empty.hint": "Make sure the desktop wremote service is running (127.0.0.1:8098). The view appears as soon as frames arrive.",
      },
    };

    /** 只透传这些键，避免把宿主快捷键（⌘/Ctrl+K 之类）抢走。 */
    const PASSTHROUGH = new Set([
      8, 9, 13, 27, 33, 34, 35, 36, 37, 38, 39, 40, 45, 46,
      112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123,
    ]);

    // ── 样式（走宿主主题 token）────────────────────────────────────────────
    function installStyles() {
      if (typeof document === "undefined") return () => {};
      const STYLE_ID = "dsh-wremote-style";
      if (document.getElementById(STYLE_ID) !== null) return () => {};
      const el = document.createElement("style");
      el.id = STYLE_ID;
      el.textContent = `
        .wrm-root{display:flex;flex-direction:column;height:100%;min-height:0;
          gap:10px;padding:14px;box-sizing:border-box;
          color:var(--dsw-alias-label-primary,#e5e5e5);}
        .wrm-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;flex:0 0 auto;}
        .wrm-title{font-size:15px;font-weight:600;line-height:1.3;}
        .wrm-sub{font-size:12px;color:var(--dsw-alias-label-secondary,#9aa0a6);margin-top:2px;}
        .wrm-spacer{flex:1 1 auto;}
        .wrm-chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;
          padding:3px 9px;border-radius:999px;
          background:var(--dsw-alias-bg-secondary,rgba(255,255,255,.06));
          border:1px solid var(--dsw-alias-border-secondary,rgba(255,255,255,.1));}
        .wrm-dot{width:7px;height:7px;border-radius:50%;background:#8b8f96;}
        .wrm-dot.on{background:#3ecf8e;}
        .wrm-dot.wait{background:#e8b339;}
        .wrm-btn{font:inherit;font-size:12px;padding:5px 11px;border-radius:7px;cursor:pointer;
          color:var(--dsw-alias-label-primary,#e5e5e5);
          background:var(--dsw-alias-bg-secondary,rgba(255,255,255,.06));
          border:1px solid var(--dsw-alias-border-primary,rgba(255,255,255,.16));}
        .wrm-btn:hover:not(:disabled){background:var(--dsw-alias-bg-tertiary,rgba(255,255,255,.1));}
        .wrm-btn:disabled{opacity:.45;cursor:not-allowed;}
        .wrm-stage{position:relative;flex:1 1 auto;min-height:0;border-radius:10px;overflow:auto;
          background:#0a0a0c;border:1px solid var(--dsw-alias-border-secondary,rgba(255,255,255,.1));
          display:flex;align-items:center;justify-content:center;}
        .wrm-stage:focus{outline:2px solid var(--dsw-alias-brand-primary,#4d6bfe);outline-offset:-2px;}
        .wrm-stage img{max-width:100%;max-height:100%;width:auto;height:auto;display:block;
          -webkit-user-drag:none;user-select:none;touch-action:none;cursor:crosshair;}
        .wrm-stage.dragging img{cursor:grabbing;}
        .wrm-empty{text-align:center;padding:24px;max-width:520px;}
        .wrm-empty h3{margin:0 0 8px;font-size:15px;font-weight:600;}
        .wrm-empty p{margin:0;font-size:13px;line-height:1.6;
          color:var(--dsw-alias-label-secondary,#9aa0a6);}
        .wrm-meta{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;flex:0 0 auto;
          color:var(--dsw-alias-label-secondary,#9aa0a6);}
        .wrm-meta b{display:block;font-weight:600;font-size:12px;margin-top:1px;
          color:var(--dsw-alias-label-primary,#e5e5e5);}
        .wrm-err{font-size:12px;color:#e5534b;white-space:pre-wrap;word-break:break-word;
          max-height:80px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
        .wrm-row{display:flex;gap:6px;align-items:center;flex:0 0 auto;}
        .wrm-text{flex:1 1 auto;min-width:120px;font:inherit;font-size:13px;padding:6px 9px;
          border-radius:7px;color:var(--dsw-alias-label-primary,#e5e5e5);
          background:var(--dsw-alias-bg-secondary,rgba(255,255,255,.06));
          border:1px solid var(--dsw-alias-border-primary,rgba(255,255,255,.16));}
        /* 原生全屏：stage 本身变成 :fullscreen 元素，铺满屏；图片等比 contain 居中，
           黑边只可能来自画面宽高比与屏幕不同——那是画面本身，不是布局。 */
        .wrm-full{background:#000;border:none;border-radius:0;padding:0;overflow:hidden;}
        .wrm-full img{width:100%;height:100%;max-width:100%;max-height:100%;
          object-fit:contain;}
        /* 撑满：画面拉伸填满整屏，不留边（会变形）。 */
        .wrm-full.wrm-fill img{object-fit:fill;}
        .wrm-exit,.wrm-fit{position:absolute;top:12px;z-index:10000;font:inherit;
          font-size:12px;padding:6px 12px;border-radius:999px;cursor:pointer;
          color:#fff;background:rgba(0,0,0,.62);border:1px solid rgba(255,255,255,.32);
          opacity:.45;transition:opacity .15s;}
        .wrm-exit{right:12px;}
        .wrm-fit{right:112px;}
        .wrm-exit:hover,.wrm-fit:hover{opacity:1;}
        .wrm-esc{position:absolute;left:12px;bottom:12px;z-index:10000;font-size:12px;
          padding:4px 10px;border-radius:999px;pointer-events:none;
          color:#fff;background:rgba(0,0,0,.62);}
      `;
      document.head.appendChild(el);
      return () => { el.remove(); };
    }

    // ── 面板 ───────────────────────────────────────────────────────────────
    function WremotePanel(props) {
      const t = props.t ?? ((key) => DICTS.zh[key] ?? key);

      const [status, setStatus] = React.useState(null);
      const [error, setError] = React.useState("");
      const [full, setFull] = React.useState(false);
      /** 全屏态下画面是「保持比例（contain）」还是「撑满（fill）」。 */
      const [fill, setFill] = React.useState(false);
      const [streamKey, setStreamKey] = React.useState(0);
      const [draft, setDraft] = React.useState("");
      const [dragging, setDragging] = React.useState(false);
      const imgRef = React.useRef(null);
      const stageRef = React.useRef(null);
      const pressed = React.useRef(false);
      /** 最近一次发送 move 的时间（节流用）。 */
      const lastMoveAt = React.useRef(0);
      /** 本次按下是否已经产生过实际位移 —— 没位移的按下-抬起就是一次点击。 */
      const moved = React.useRef(false);

      /** 全屏浮层按钮压在画面上。指针事件本来就该落在最上层的按钮上、不会命中
          底下的 <img>，但按钮是 stage 的子节点 —— 这里顺手挡住冒泡，
          万一以后给浮层加 pointer-events:none 之类的样式，也不会把点击漏给远端。 */
      const stopStage = React.useCallback((ev) => {
        ev.stopPropagation();
      }, []);

      const refresh = React.useCallback(async () => {
        try {
          const res = await fetch(`${API}/status`, { cache: "no-store" });
          setStatus(await res.json());
        } catch (e) {
          setStatus({ ok: false, upstream: { connected: false }, stream: {}, error: String(e) });
        }
      }, []);

      React.useEffect(() => {
        refresh();
        const id = setInterval(refresh, 2000);
        return () => clearInterval(id);
      }, [refresh]);

      const post = React.useCallback(async (body) => {
        try {
          const res = await fetch(`${API}/input`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body ?? {}),
          });
          const json = await res.json().catch(() => ({}));
          if (json?.ok === false) setError(json.error ?? "未知错误");
          else setError("");
          return json;
        } catch (e) {
          setError(String(e));
          return null;
        }
      }, []);

      /** 几何协商状态：`sent` 是最后一次真正发成功的尺寸（去重，避免 resize 风暴），
          `restore` 是进入全屏前的画面尺寸 —— 退出全屏时发回去把源窗口复原。 */
      const geomRef = React.useRef({ sent: "", restore: null });

      /** 把物理像素尺寸交给上游，让它按这个尺寸重建源窗口。
          失败一律吞掉：没连上、自己不是 geom_owner、配置关了 sendGeom —— 都不该让面板弹错。
          订阅不上的时候把 `sent` 清空，下一次触发还能再试。
          `take` 为真表示「这块屏现在是我在看」——进全屏时用它从别的连接手里接管 owner，
          否则一个残留的浏览器页面会把 owner 永久占住，全屏怎么点都改不动源窗口。 */
      const postGeom = React.useCallback(async (w, h, take = false) => {
        const key = `${w}x${h}${take ? "!" : ""}`;
        if (geomRef.current.sent === key) return;
        geomRef.current.sent = key;
        try {
          const res = await fetch(`${API}/geom`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(take ? { w, h, take: true } : { w, h }),
          });
          const json = await res.json().catch(() => ({}));
          if (json?.ok !== true) geomRef.current.sent = "";
        } catch {
          geomRef.current.sent = "";
        }
      }, []);

      /** 全屏元素的物理像素尺寸。上游 resize_client 收的是物理像素，
          高分屏必须乘 devicePixelRatio，否则窗口比屏幕小、画面发虚。 */
      const screenSize = React.useCallback(() => {
        const el = stageRef.current;
        if (el === null) return null;
        const rect = el.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const w = Math.round(rect.width * dpr);
        const h = Math.round(rect.height * dpr);
        if (!(w >= 200 && h >= 200 && w <= 4000 && h <= 4000)) return null;
        return { w, h };
      }, []);

      /** 把指针位置换成帧像素。
          普通面板里 img 用 max-width/max-height 自适应，元素框就是画出来的图，直接按框换算。
          全屏时元素框铺满整屏、图由 object-fit 决定怎么画在那块框里，必须先把框折算成
          「实际画出画面的矩形」：contain 居中留边（要减去 pad，落在边上的点击算无效），
          fill 拉伸铺满（框即画面）。 */
      const toFrame = React.useCallback((ev) => {
        const img = imgRef.current;
        if (img === null) return null;
        const rect = img.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return null;
        const size = status?.stream?.size ?? null;
        const vw = size?.w ?? img.naturalWidth ?? rect.width;
        const vh = size?.h ?? img.naturalHeight ?? rect.height;
        if (!(vw > 0) || !(vh > 0)) return null;

        const px = ev.clientX - rect.left;
        const py = ev.clientY - rect.top;

        if (full && !fill) {
          // contain：等比缩放后居中，四周可能留边。
          const scale = Math.min(rect.width / vw, rect.height / vh);
          const drawW = vw * scale;
          const drawH = vh * scale;
          const padX = (rect.width - drawW) / 2;
          const padY = (rect.height - drawH) / 2;
          if (px < padX || py < padY || px > padX + drawW || py > padY + drawH) {
            return null; // 点在黑边上，别往远端发
          }
          const x = (px - padX) / scale;
          const y = (py - padY) / scale;
          if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
          return {
            x: Math.max(0, Math.min(vw - 1, Math.round(x))),
            y: Math.max(0, Math.min(vh - 1, Math.round(y))),
          };
        }

        // 其余情况（面板内、全屏 fill）：元素框即画面。
        const x = (px / rect.width) * vw;
        const y = (py / rect.height) * vh;
        if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
        return {
          x: Math.max(0, Math.min(vw - 1, Math.round(x))),
          y: Math.max(0, Math.min(vh - 1, Math.round(y))),
        };
      }, [status, full, fill]);

      const onPointerDown = React.useCallback((ev) => {
        if (ev.button === 2) return; // 右键走 contextmenu，避免 down/up 重复
        stageRef.current?.focus();
        const at = toFrame(ev);
        if (at === null) return;
        pressed.current = true;
        moved.current = false;
        setDragging(true);
        try { ev.currentTarget.setPointerCapture(ev.pointerId); } catch { /* ignore */ }
        void post({ action: "down", ...at, button: ev.button === 1 ? "middle" : "left" });
      }, [post, toFrame]);

      const onPointerMove = React.useCallback((ev) => {
        if (!pressed.current) return;
        const now = Date.now();
        // 拖动时按 ~60Hz 上限发 move：服务端靠位移 1:1 平移窗口，发太快只是白占带宽。
        if (now - lastMoveAt.current < 16) return;
        const at = toFrame(ev);
        if (at === null) return;
        lastMoveAt.current = now;
        moved.current = true;
        void post({ action: "move", ...at });
      }, [post, toFrame]);

      const onPointerUp = React.useCallback((ev) => {
        if (!pressed.current) return;
        pressed.current = false;
        setDragging(false);
        const at = toFrame(ev);
        if (at === null) return;
        void post({ action: "up", ...at, button: ev.button === 1 ? "middle" : "left" });
      }, [post, toFrame]);

      const onContextMenu = React.useCallback((ev) => {
        ev.preventDefault();
        const at = toFrame(ev);
        if (at !== null) void post({ action: "click", ...at, button: "right" });
      }, [post, toFrame]);

      /** 进/出原生全屏。用 Fullscreen API 而不是 position:fixed——
          祖先只要带 transform/filter，fixed 就被困在面板里（黑边的来源），
          而且 Esc 只能由浏览器接管才退得出来。 */
      const enterFull = React.useCallback(async () => {
        const el = stageRef.current;
        if (el === null) return;
        // 没有 Fullscreen API 就不进「假全屏」：界面切成全屏态但元素其实没铺满，
        // 又会回到「四周黑边 + 没有退出按钮」的老样子。
        if (!el.requestFullscreen && !el.webkitRequestFullscreen) {
          setError("这个浏览器不支持全屏（Fullscreen API 不可用）");
          return;
        }
        // 记下进全屏前的画面尺寸，退出时发回去让桌面窗口复原。
        const cur = status?.stream?.size ?? null;
        if (cur) geomRef.current.restore = { w: cur.w, h: cur.h };
        try {
          if (el.requestFullscreen) await el.requestFullscreen();
          else el.webkitRequestFullscreen();
          setFull(true);
        } catch (e) {
          setError(`进入全屏失败：${String(e?.message ?? e)}`);
        }
      }, [status]);

      const exitFull = React.useCallback(async () => {
        try {
          if (document.fullscreenElement ?? document.webkitFullscreenElement) {
            if (document.exitFullscreen) await document.exitFullscreen();
            else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
          }
        } catch { /* ignore */ }
        setFull(false);
      }, []);

      // 全屏状态以浏览器为准：用户按 Esc / 系统手势退出时也要把 UI 收回来。
      React.useEffect(() => {
        if (typeof document === "undefined") return () => {};
        const sync = () => {
          const on = (document.fullscreenElement ?? document.webkitFullscreenElement) === stageRef.current
            && stageRef.current !== null;
          setFull(on);
        };
        document.addEventListener("fullscreenchange", sync);
        document.addEventListener("webkitfullscreenchange", sync);
        return () => {
          document.removeEventListener("fullscreenchange", sync);
          document.removeEventListener("webkitfullscreenchange", sync);
        };
      }, []);

      // 全屏时协商几何：把屏幕物理像素交给上游，源窗口改成同尺寸 → 不留边。
      // 退出全屏时把进全屏前的尺寸发回去复原。都只是「请求」，上游不认（自己不是
      // geom_owner / sendGeom 关了）就静默跳过，面板照常等比缩放。
      React.useEffect(() => {
        if (full) {
          // 刚进全屏元素才铺开，等一帧让布局落定再量尺寸。
          // take=true：全屏是用户明确「按我这块屏显示」的动作，要能接管 owner。
          const kick = setTimeout(() => {
            const s = screenSize();
            if (s !== null) void postGeom(s.w, s.h, true);
          }, 120);
          // 全屏里改窗口大小（多显示器拖动 / 系统缩放变化）后重量一次。
          // 去抖 350ms，与上游 client.html 一致：避免 resize 风暴把服务端 resize 打乱。
          let timer = null;
          const onResize = () => {
            if (timer !== null) clearTimeout(timer);
            timer = setTimeout(() => {
              const s = screenSize();
              if (s !== null) void postGeom(s.w, s.h, true);
            }, 350);
          };
          window.addEventListener("resize", onResize);
          return () => {
            clearTimeout(kick);
            if (timer !== null) clearTimeout(timer);
            window.removeEventListener("resize", onResize);
          };
        }
        // 退出全屏不 take：如果别的连接已经接管了 owner，硬抢会把人家正在看的
        // 尺寸一起带走。复原只是「请求」，认不认由上游的 owner 规则决定。
        const back = geomRef.current.restore;
        if (back !== null) {
          geomRef.current.restore = null;
          void postGeom(back.w, back.h);
        }
        return () => {};
      }, [full, postGeom, screenSize]);

      const onWheel = React.useCallback((ev) => {
        const at = toFrame(ev);
        if (at === null) return;
        ev.preventDefault();
        // 浏览器 deltaY 一档约 100，Windows 一格是 120：按符号给整格，快滚给两格。
        const steps = Math.abs(ev.deltaY) >= 220 ? 2 : 1;
        const delta = (ev.deltaY > 0 ? -1 : 1) * 120 * steps;
        void post({ action: "scroll", ...at, delta });
      }, [post, toFrame]);

      const onKeyDown = React.useCallback((ev) => {
        // 全屏时 Esc 归浏览器（退出全屏），不发给远端，否则永远退不出来。
        if (ev.keyCode === 27 && (document.fullscreenElement ?? document.webkitFullscreenElement)) {
          return;
        }
        if (!PASSTHROUGH.has(ev.keyCode)) return;
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.repeat) return;
        void post({ action: "key", vk: ev.keyCode });
      }, [post]);

      const sendText = React.useCallback(async () => {
        const text = draft;
        if (text.length === 0) return;
        const json = await post({ action: "text", text });
        if (json?.ok === true) setDraft("");
      }, [draft, post]);

      const saveFrame = React.useCallback(async () => {
        try {
          const res = await fetch(`${API}/frame.jpg?t=${Date.now()}`, { cache: "no-store" });
          if (!res.ok) {
            setError(`抓图失败 HTTP ${res.status}`);
            return;
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = `wremote-${Date.now()}.jpg`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
          setError(String(e));
        }
      }, []);

      const stream = status?.stream ?? {};
      const upstream = status?.upstream ?? {};
      const connected = upstream.connected === true;
      // wremote 用 np.array_equal 去重：画面不动就不推帧，所以 ageMs 变大
      // 不等于「坏了」。判活只看连上没有 + 有没有出过帧。
      const hasFrame = (stream.seq ?? 0) > 0;
      const phase = connected && hasFrame ? "on" : connected ? "wait" : "off";
      const phaseText = {
        on: t("status.on"),
        wait: t("status.wait"),
        off: t("status.off"),
      }[phase];

      const size = stream.size ?? null;
      const fps = stream.fps ?? 0;

      const stage = React.createElement("div", {
        className: `${full ? "wrm-stage wrm-full" : "wrm-stage"}${full && fill ? " wrm-fill" : ""}${dragging ? " dragging" : ""}`,
        ref: stageRef,
        tabIndex: 0,
        title: t("keys.hint"),
        onKeyDown,
        onWheel,
        onContextMenu,
      },
      hasFrame || connected
        ? React.createElement("img", {
            key: streamKey,
            ref: imgRef,
            src: `${API}/stream?t=${streamKey}`,
            alt: t("panel.title"),
            draggable: false,
            onPointerDown,
            onPointerMove,
            onPointerUp,
            onPointerCancel: onPointerUp,
            onError: () => {
              // MJPEG 断了（服务重启 / 上游关闭）：稍后重建一次 <img> 重新拉流
              setTimeout(() => setStreamKey((k) => k + 1), 1200);
            },
          })
        : React.createElement("div", { className: "wrm-empty" },
            React.createElement("h3", null, t("empty.title")),
            React.createElement("p", null, t("empty.hint"))),
      full
        // 退出按钮必须在全屏元素内部：原生全屏只渲染该元素及其后代。
        // stopPropagation 是为了不让指针事件冒泡到 stage（stage 上挂着 onWheel）。
        ? React.createElement("button", {
            className: "wrm-exit",
            onPointerDown: stopStage,
            onClick: () => void exitFull(),
          }, t("action.exitFullscreen"))
        : null,
      full
        ? React.createElement("button", {
            className: "wrm-fit",
            onPointerDown: stopStage,
            onClick: () => setFill((v) => !v),
          }, fill ? t("action.contain") : t("action.fill"))
        : null,
      full
        ? React.createElement("div", { className: "wrm-esc" }, t("keys.escHint"))
        : null);

      return React.createElement("div", { className: "wrm-root" },
        React.createElement("div", { className: "wrm-head" },
          React.createElement("div", null,
            React.createElement("div", { className: "wrm-title" }, t("panel.title")),
            React.createElement("div", { className: "wrm-sub" }, t("panel.subtitle"))),
          React.createElement("div", { className: "wrm-spacer" }),
          React.createElement("span", { className: "wrm-chip" },
            React.createElement("span", { className: `wrm-dot ${phase}` }),
            phaseText),
          React.createElement("button", {
            className: "wrm-btn", onClick: saveFrame, disabled: !hasFrame,
          }, t("action.snapshot")),
          React.createElement("button", {
            className: "wrm-btn",
            onClick: () => setStreamKey((k) => k + 1),
          }, t("action.reconnect")),
          React.createElement("button", {
            className: "wrm-btn",
            onClick: () => void (full ? exitFull() : enterFull()),
          }, full ? t("action.exitFullscreen") : t("action.fullscreen"))),
        error !== "" ? React.createElement("div", { className: "wrm-err" }, error) : null,
        stage,
        React.createElement("div", { className: "wrm-meta" },
          React.createElement("div", null, t("meta.fps"),
            React.createElement("b", null, `${fps}`)),
          React.createElement("div", null, t("meta.size"),
            React.createElement("b", null, size ? `${size.w}×${size.h}` : "—")),
          React.createElement("div", null, t("meta.src"),
            React.createElement("b", null, stream.src !== "" ? stream.src : "—")),
          React.createElement("div", null, t("meta.hint"),
            React.createElement("b", null, stream.hint !== "" ? stream.hint : "—"))),
        React.createElement("div", { className: "wrm-row" },
          React.createElement("input", {
            className: "wrm-text",
            value: draft,
            placeholder: t("input.placeholder"),
            onChange: (ev) => setDraft(ev.target.value),
            onKeyDown: (ev) => {
              // 输入框里的回车直接发送，别透传给远端（那会变成远端的新建行）
              if (ev.key === "Enter") {
                ev.preventDefault();
                void sendText();
              }
            },
          }),
          React.createElement("button", {
            className: "wrm-btn", onClick: sendText, disabled: draft.length === 0,
          }, t("input.send")),
          React.createElement("button", {
            className: "wrm-btn", onClick: () => setDraft(""), disabled: draft.length === 0,
          }, t("input.clear"))));
    }

    // ── 侧栏图标 ───────────────────────────────────────────────────────────
    function SidebarIcon(props) {
      const size = props?.size ?? 18;
      // 两个叠起来的窗口，跟「投屏 / 镜像」的语义一致
      return React.createElement("svg", {
        width: size, height: size, viewBox: "0 0 24 24",
        fill: "none", stroke: "currentColor", strokeWidth: 1.7,
        strokeLinecap: "round", strokeLinejoin: "round",
      },
      React.createElement("rect", { x: 3, y: 4, width: 13, height: 10, rx: 2 }),
      React.createElement("path", { d: "M8 18h13V8" }),
      React.createElement("circle", { cx: 9.5, cy: 9, r: 0.9, fill: "currentColor", stroke: "none" }));
    }

    return {
      inject: ["slots", "locale"],
      apply(ctx) {
        ctx.effect(installStyles, "wremote: stylesheet");
        ctx.effect(() => ctx.locale.register(NS, "zh", DICTS.zh), "wremote: dict zh");
        ctx.effect(() => ctx.locale.register(NS, "en", DICTS.en), "wremote: dict en");

        ctx.slots.inject("sidebar.panellist", () => ctx.slots.register({
          name: "sidebar.panellist",
          id: PANEL_KEY,
          order: 34,
          label: () => (ctx.locale.getLocale?.()?.id === "en"
            ? DICTS.en["nav.label"]
            : DICTS.zh["nav.label"]),
          locale: NS,
        }, SidebarIcon));

        ctx.slots.inject("main", () => ctx.slots.register({
          name: "main",
          key: PANEL_KEY,
          locale: NS,
        }, WremotePanel));
      },
    };
  },
});
