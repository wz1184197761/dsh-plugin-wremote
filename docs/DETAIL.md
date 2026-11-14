# dsh-wremote

把本机 **wremote** 投屏服务（微信单窗口 MJPEG，默认 `127.0.0.1:8098`）接进 DSH：
侧栏一个实时画面面板，点击 / 拖动 / 滚动 / 键盘 / 文本直接回传到远端窗口；
同时给模型一组工具，可以自己看画面再操作。

本插件**不重写**采集、窗口提顶、几何任何一环，只做一层桥。

---

## 它桥的是什么

wremote（`C:\Users\11841\apps\wremote\server.py`）对外只有一个 HTTP 页面和一条 WebSocket：

| 方向 | 内容 |
| --- | --- |
| 下行 | 裸 JPEG 二进制帧（每帧一条 message），偶尔夹一条 `{"t":"state","src":"mon","hint":"…"}` JSON |
| 上行 | `{"t":"geom","w","h"}` 或 `{"t":"geom","w","h","take":true}` / `{"t":"m","a":"down\|up\|move\|wheel\|rdown\|rup\|md\|mu","x","y","d"}` / `{"t":"k","vk","up"}` / `{"t":"text","s"}` / `{"t":"ping"}` |

`x` / `y` 是**帧像素**（左上为原点）。服务端自己按 `frame_size()` 换算成屏幕坐标，
并在按下前把微信主窗顶到 topmost 带最前，所以插件和面板都不做坐标数学。

## 面板

侧栏图标「投屏」→ 主列面板：

- 画面：`<img src="/api/wremote/stream">`，同源 MJPEG。浏览器原生支持
  `multipart/x-mixed-replace`，不需要 canvas、不需要在前端解码、不需要开 WebSocket。
- 点击 / 双击 / 右键 / 拖动 / 滚轮：指针事件翻译成 `down` / `move` / `up` / `scroll`。
  按下后产生过位移才算拖动（没位移的按下-抬起就是一次点击）。
- 键盘：只有白名单键（方向键、回车、Esc、Tab、退格、Delete、PageUp/Down、Home/End、F1–F12）
  透传，避免抢走宿主自己的快捷键。
- 文本框：走服务端的 unicode 通道（`{"t":"text"}`），中文、emoji 都能进，不依赖输入法。
  发送前先用鼠标点到远端输入框里。
- 存图：`/api/wremote/frame.jpg` 取当前一帧存成文件。
- 全屏：走**浏览器原生 Fullscreen API**（`stage.requestFullscreen()`），不是
  `position:fixed`。理由两条：面板祖先只要带 `transform` / `filter`，`fixed` 元素就被困在
  那个包含块里、盖不满视口（曾经的黑边来源）；而且 Esc 只有浏览器接管全屏才退得出来。
  全屏中右上角两个浮层按钮——`撑满` / `保持比例` 切换与 `退出全屏`；左下角提示 Esc。
  浮层必须是 stage 的后代，原生全屏只渲染全屏元素及其子树。
- 全屏坐标：contain 模式下画面居中、四周可能留黑边（画面宽高比与屏幕不同）。所以
  指针换算分两支：`full && !fill` 时先算 `scale = min(rect.w/vw, rect.h/vh)`、
  `padX/padY`，落在黑边上的点击直接丢弃（不发远端）；其余情况（面板内、全屏 fill）
  元素框就是画面，直接按框换算。

## HTTP 路由

都在 `/api/wremote` 下，同源、无鉴权（跟宿主 Web 一致）。

| 路由 | 方法 | 作用 |
| --- | --- | --- |
| `/status` | GET | 上游连接、收帧序号 / 帧率 / 画面尺寸、配置回显 |
| `/frame.jpg` | GET | 当前一帧（还没收到帧时 503） |
| `/stream` | GET | MJPEG 长连接，每条连接 `acquire()` 一次 |
| `/input` | POST | `click` / `double` / `move` / `down` / `up` / `scroll` / `key` / `text` |
| `/geom` | POST | `{w,h,take?}` 协商源窗口尺寸，见下一节；`sendGeom:false` 时回 `ok:false,skipped:true` |

## 工具

| 工具 | 作用 |
| --- | --- |
| `wremote_status` | 上游是否连上、收帧序号、帧率、画面尺寸、最近一次服务端提示 |
| `wremote_screenshot` | 抓当前画面，存成附件返回（先看再点，别瞎猜坐标） |
| `wremote_click` | 点一下，坐标是帧像素；`double` 走双击，`button` 选左/右/中 |
| `wremote_scroll` | 滚轮，`delta` 正数向上、负数向下，一格 120（Windows WHEEL_DELTA） |
| `wremote_type` | 往当前焦点控件输入文本（unicode，不依赖输入法） |
| `wremote_key` | 按一个虚拟键（VK 码），例如 13=Enter、27=Esc、116=F5 |

工具挂在 **Agent 作用域**（`agent.ctx.tools`），不是全局 `ctx.tools` ——
否则每个会话都会带上这六个工具。

## 几何协商：只在全屏时发 `geom`

服务端 `state` 是**全连接共享**的，`geom` 会让发它的那条连接成为 `geom_owner`，
owner 会触发**真实 resize 微信客户区**，然后重建整条采集链（停源 + 新建 + 等首帧）。

所以插件的策略是「按需协商」：

- **普通面板不发**。侧栏只有几百像素宽，按它改源窗口等于把用户的桌面微信缩成一条，
  代价远大于收益。面板照常等比缩放，留白由 `.wrm-stage` 居中吸收。
- **进全屏发一次 `take:true`**。全屏时显示区就是整块屏幕，按它重建源窗口，画面就
  1:1 铺满、没有等比留边。`take:true` 是主动接管 `geom_owner` —— 否则一个残留的
  浏览器页面（`http://host:8098/`）会永久占着主人位，面板全屏怎么点都改不动源窗口。
- **退出全屏把进入前的画面尺寸发回去复原**，这条**不带** `take`：如果别人已经接管，
  硬抢会把人家正在看的尺寸一起带走。复原只是「请求」，认不认由上游的 owner 规则决定。

尺寸一律是**物理像素**（`getBoundingClientRect()` × `devicePixelRatio`）：上游
`resize_client` 收的是物理像素，高分屏不乘会让窗口小于屏幕、缩放后画面发虚。
进全屏后 120ms 才量（等布局铺开），全屏内改窗口大小按 350ms 去抖重发。

`config.sendGeom` 关掉就退回「只等比缩放、全屏会留黑边」的老行为。

**排障判据**：上游 `server.log` 里 `[geom] recv WxH take=… owner=self|other` 是
「帧到了没、自己是不是主人」，`[geom] resize client A -> B (want WxH)` 是「真的改了」。
只有前一条没有后一条，说明帧到了但没改窗口 —— 通常是 `want` 与当前客户区尺寸相同
（`resize_client` 判等后直接 `return False`，不打日志）。

## 连接生命周期

- `autoConnect: true` 时插件加载即建立一条上游连接，面板打开就能立刻出画面。
- 每个 MJPEG 观众、每次工具调用都 `acquire()` 一次；全部释放后按 `idleGraceMs`
  （默认 30s）主动断开，不长期占着 8098。
- 上游断开后按 `reconnectMs`（默认 4s）重连，`<img>` 的 `onError` 会重建一次流。

## 配置

`cordis.patch.yml` 里本插件那一条的 `config`：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | wremote 所在主机 |
| `port` | `8098` | `launch.py --port` 的值 |
| `path` | `/` | ws 路径，`ws_serve` 不区分路径 |
| `autoConnect` | `true` | 加载即连 |
| `reconnectMs` | `4000` | 断线重连间隔 |
| `streamRetryMs` | `1200` | `<img>` 重建间隔 |
| `firstFrameTimeoutMs` | `6000` | 工具抓图等首帧的上限 |
| `idleGraceMs` | `30000` | 无观众后多久断开上游 |
| `sendGeom` | `true` | 全屏时是否协商几何，见上一节 |

## 依赖

零外部依赖。WebSocket 客户端是 `lib/wsclient.js` 里自带的 200 行实现
（握手 / 掩码 / 分片 / ping-pong / close）。不走 `ws` 包是因为插件以 `link:` 装进
profile，解析路径从包的真实目录出发，拿不到 profile 的 `node_modules`。

## 客户端注册约定

`client.js` 顶层的 `window.__ModuleLoader__.load({ id })` 必须写**完整包名**
（本包是 `@local/dsh-wremote`）。宿主 `ClientModuleSystem.register()` 用
`stripClientSuffix(registration.id)` 作工厂键，而 `arrive(row)` 用 boot graph 行的
`id`（也就是 `package.json` 的 `name`）去 `factories` 里查；两者不等时 `arrive()`
报 `loaded without registering "<id>" via __ModuleLoader__.load`，面板与图标**永远不会
materialize**，表现为「工具能用、侧栏没有入口」。`stripClientSuffix` 只剥结尾的
`/client`，不会补 scope。

## 排查

1. 面板一直「等待画面…」→ 先 `curl http://127.0.0.1:8098/` 看服务在不在，
   再看 `wremote_status` 返回的 `upstream.lastError`。
2. 有画面但点了没反应 → 那是 wremote 侧的 topmost 带问题，不在本插件。
   看 `server.log` 里该条 `[in] down ... raise=N`：`raise=3` 说明重排失败。
3. 画面尺寸和面板不一致 → 正常。`<img>` 按 `max-width/max-height` 等比缩放，
   点击换算用的是 `getBoundingClientRect()` 与帧像素的比值。
4. 侧栏根本没有「微信投屏」图标 → 见上一节，先核对 `load({ id })` 是不是完整包名；
   再用 `curl http://127.0.0.1:39397/api/wremote/status` 看 `upstream.refs`：
   `1` 表示只有自动连接、没有观众（面板没在拉流），`2` 表示面板的 `<img>` 已挂上。
   `refs` 会随每条 `/stream` 增减，断开后按 `idleGraceMs` 回收上游。
5. 全屏后四周还有黑边 → 先看 `[geom]` 那两条日志：`[geom] recv … take=True` 与
   `[geom] resize client A -> B` 都在，说明源窗口已经按屏幕重建，剩下的边只可能是
   `sendGeom` 被关掉、或它不是 `geom_owner`。真要临时铺满就点右上角「撑满」。
   本机曾见到的黑边来源：微信客户区 1876x1032 ≈ 1.82:1，而屏幕 2560x1080 ≈ 2.37:1，
   等比缩放必然左右留边。协商生效后这个比例差就消失了。
6. 全屏退不出来 → Esc 由浏览器处理（`onKeyDown` 在全屏态对 27 直接放行），
   另有右上角「退出全屏」按钮。若浏览器不支持 Fullscreen API，进入全屏会直接报错
   而不是切一个假全屏态（假全屏态会重新出现黑边 + 没有退出按钮）。
7. 全屏尺寸没变 / 画面还是旧的 → `curl -X POST -H "content-type: application/json"
   --data-binary '{"w":1920,"h":1080,"take":true}' http://127.0.0.1:39397/api/wremote/geom`
   （**必须 `--data-binary`**，`-d` 会把引号转义坏、报 `body 不是合法 JSON`），
   然后隔 3~4 秒看 `/api/wremote/status` 的 `stream.size` 有没有变。
