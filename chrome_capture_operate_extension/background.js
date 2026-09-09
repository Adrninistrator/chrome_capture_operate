/**
 * chrome_capture_operate Cookie 推送后台（MV3 Service Worker）
 *
 * 推送时机：
 *  - 启动时（onStartup / onInstalled）
 *  - chrome.cookies.onChanged 变更时（节流间隔可配，默认 10 秒，最小 10 秒；
 *    间隔内累积的变更打标记，由定时 alarm 补推）
 *  - Authorization 变化时（webRequest.onSendHeaders 观察请求头中的
 *    Authorization，观察地址先过推送范围管道，值与上次观察值不同时
 *    才标脏触发）
 *  - 定时推送（间隔可配，默认 5 分钟，最小 1 分钟）
 *
 * 变更推送的触发条件（prompt.md：仅范围内变更且值有真实差异才推）：
 *  - onChanged 回调先按 changeInfo.cookie 做范围预判——范围外变更直接
 *    忽略，连节流标记都不打；
 *  - 待推送快照与"上次成功推送的快照"（storage.session，生命周期与浏览器
 *    会话一致）diff：key 为 domain/path/name/分区，比 value；无新增/
 *    删除/值变化（同值重写不算）则跳过推送；
 *  - 推送失败保留旧快照作基准，服务端未确认的变更下次重推；
 *  - 启动时/定时推送/手动推送不受 diff 限制（需求：必推时机）。
 *
 * 推送范围（可配，storage.local.push_scope）：
 *  - all：全部允许
 *  - none：全部禁止（默认；不推送任何 Cookie/Authorization）
 *  - list：按清单推送——allow 清单任一匹配才推送（空=全部推送），
 *    deny 清单任一匹配即不推送（空=全部推送）；deny 优先于 allow。
 *    清单项为域名或 IP，支持 * 通配符（如 *.baidu.com）。
 *
 * 推送记录：storage.local.push_history，保留最近 20 次（时间/原因/地址/数量/成功失败）。
 *
 * 收藏并打开页面（storage.local.page_collections，全部在插件内实现——
 * 不放 Python 端的原因：Python 无法枚举其他 Chrome 的标签页，
 * 采集只能由插件发起，放 Python 使用不便；数据丢失风险用导出
 * 收藏页面信息功能防范）：收藏把当前全部标签页（忽略本扩展页面与
 * 非 http/https）记为一条新收藏；批量打开按 ~0.5 秒一个依次开；
 * 管理在 options 的"收藏并打开页面" TAB。
 * "Chrome启动时自动打开"勾选存 storage.local.auto_open_id（单值，
 * 同时只允许勾选一条；新收藏创建时若无勾选则默认勾选它；收藏
 * 删除/删空/导入时清除——导入会重建 id 无法对应）；onStartup 到点
 * 按批量打开相同方式依次打开。
 *
 * 会话保持（可配，storage.local.keepalive_items）：
 *  - 目标网站长时间不访问登录会话会过期，定时执行的固化脚本随之返回
 *    未登录/401；插件按每项配置的间隔刷新 Chrome 中已打开的对应页面，
 *    以真实导航保持会话（全部 Cookie 携带、Set-Cookie 落回 Cookie 存储
 *    → onChanged → 原有节流推送，服务端零改动）；
 *  - 每项启用配置独立创建 chrome.alarms 周期任务（ka_<host>，周期=该项
 *    间隔分钟），到点即刷新，不记录/不计算距上次刷新的时间——曾按分钟
 *    tick+时间差判定：tick 相位与刷新时刻天然错开数百毫秒（59.9s<60s
 *    被判未到间隔而跳过），1 分钟间隔实际变成每 2 分钟才刷新；
 *    非活动标签页直接刷新，多页取第一个非活动标签页，当前活动标签页注入 confirm
 *    弹窗确认（取消后下个周期再触发）；无已打开的匹配页面时本次不刷新
 *    （下个周期再检查；依赖页面保持打开）；
 *  - 全程日志输出到 Service Worker console（chrome://extensions → 本插件
 *    → "Service Worker"点击检查可见）：当前配置、找到的标签页（地址/
 *    活动状态）、刷新决策与结果（kaLog 统一前缀）。
 *
 */
const DEFAULT_TARGET = "http://127.0.0.1:33445/api/cookies/push";
const DEFAULT_HEARTBEAT_MIN = 5;
const DEFAULT_THROTTLE_SEC = 10;
const MIN_THROTTLE_SEC = 10;
const MIN_HEARTBEAT_MIN = 1;
// 会话保持：新配置项默认间隔 30 分钟
const DEFAULT_KA_INTERVAL_MIN = 30;

let lastPushAt = 0;
let dirtySinceThrottle = false;

// ---- 变更推送的"值比较"基准（storage.session，会话级内存）----
// SNAPSHOT_KEY：上次成功推送的 cookie 快照（map: ckKey -> value）。
// AUTH_MAP_KEY：观察到的 Authorization 最近值（map: host -> 值）。
// Service Worker 被杀不丢（storage.session），浏览器重启清零——重启后
// 首次推送必发（与"启动时推送"重叠，语义一致）。
const SNAPSHOT_KEY = "last_push_snapshot";
const AUTH_MAP_KEY = "auth_map";

/** cookie 的 diff 键 -> 待比较值（与 ckKey 同维：分区/子域等全含）。 */
function cookieDiffEntry(c) {
  return [ckKey(c), String(c.value || "")];
}

/** 两快照是否有真实差异（新增/删除/值变化；同值重写不算）。 */
function snapshotDiffers(prev, next) {
  if (!prev) return true;
  const keys = new Set([...Object.keys(prev), ...Object.keys(next)]);
  for (const k of keys) {
    if (prev[k] !== next[k]) return true;
  }
  return false;
}

// 点击插件图标：在 Chrome 中以新标签页打开设置页面（prompt.md 页面要求，
// 不使用 popup 小窗；options.html 内部以 参数配置/推送记录/会话保持 三个 TAB 展示）
chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: chrome.runtime.getURL("options.html") });
});

/** 会话保持配置项的域名或IP归一化：去协议/路径/参数/端口，转小写。
 *  端口去除是因为 tabs.query 的 URL pattern 不支持端口（按任意端口匹配）。 */
function normalizeHost(s) {
  let h = String(s || "").trim().toLowerCase();
  h = h.replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
  h = h.split("/")[0].split("?")[0].split("#")[0];
  h = h.split(":")[0];
  return /^[a-z0-9][a-z0-9.-]*$/.test(h) ? h : "";
}

async function getConfig() {
  const {
    push_target,
    throttle_sec,
    heartbeat_min,
    push_scope,
    allow_list,
    deny_list,
    keepalive_items,
  } = await chrome.storage.local.get([
    "push_target",
    "throttle_sec",
    "heartbeat_min",
    "push_scope",
    "allow_list",
    "deny_list",
    "keepalive_items",
  ]);
  return {
    target: push_target || DEFAULT_TARGET,
    // 节流间隔：默认 10 秒，最小 10 秒（非法值回退默认）
    throttleMs: Math.max(
      MIN_THROTTLE_SEC,
      parseInt(throttle_sec, 10) || DEFAULT_THROTTLE_SEC
    ) * 1000,
    // 定时推送：默认 5 分钟，最小 1 分钟（非法值回退默认）
    heartbeatMin: Math.max(
      MIN_HEARTBEAT_MIN,
      parseFloat(heartbeat_min) || DEFAULT_HEARTBEAT_MIN
    ),
    // 默认 none（全部禁止）：未配置或非法值回退 none（prompt.md：默认选择全部禁止）
    scope: push_scope === "all" || push_scope === "list" ? push_scope : "none",
    allow: Array.isArray(allow_list) ? allow_list.filter(Boolean) : [],
    deny: Array.isArray(deny_list) ? deny_list.filter(Boolean) : [],
    // 会话保持：host 归一化；间隔非法值回退默认（分钟，最小 1）；默认禁用
    kaItems: (Array.isArray(keepalive_items) ? keepalive_items : [])
      .map((it) => ({
        host: normalizeHost(it && it.host),
        intervalMin: Math.max(
          1,
          Math.round(parseFloat(it && it.interval_min) || DEFAULT_KA_INTERVAL_MIN)
        ),
        enabled: !!(it && it.enabled),
      }))
      .filter((it) => it.host),
  };
}

async function getTarget() {
  const cfg = await getConfig();
  return cfg.target;
}

async function recordHistory(entry) {
  const { push_history } = await chrome.storage.local.get("push_history");
  const list = Array.isArray(push_history) ? push_history : [];
  list.unshift(entry);
  await chrome.storage.local.set({ push_history: list.slice(0, 20) });
}

async function setBadge(ok) {
  try {
    await chrome.action.setBadgeBackgroundColor({
      color: ok ? "#187a3c" : "#b3392e",
    });
    await chrome.action.setBadgeText({ text: ok ? "" : "!" });
  } catch (e) { /* 忽略 */ }
}

/** 清单项（支持 * 通配符）是否匹配 cookie 域名/IP。
 *  规则：* 匹配单个域段（不含点）；*.baidu.com 匹配子域与裸域 baidu.com。 */
function matchHost(pattern, host) {
  if (!pattern || !host) return false;
  const p = pattern.toLowerCase();
  const h = host.toLowerCase();
  if (p === h) return true;
  if (p.startsWith("*.")) {
    // *.baidu.com：匹配任意子域（api.baidu.com）与裸域（baidu.com）
    const suffix = p.slice(1); // ".baidu.com"
    return h.endsWith(suffix) || h === p.slice(2);
  }
  if (p.includes("*")) {
    // 其他位置的 * 匹配单个域段（如 192.168.* 匹配 192.168.1）
    const re = new RegExp(
      "^" + p.split("*").map((s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&"))
        .join("[^.]*") + "$"
    );
    return re.test(h);
  }
  return false;
}

/** cookie 是否在推送范围内（scope/allow/deny 判定，deny 优先）。 */
function cookieAllowed(cookie, cfg) {
  if (cfg.scope === "none") return false;
  if (cfg.scope !== "list") return true; // all
  const host = (cookie.domain || "").replace(/^\./, "");
  if (cfg.deny.some((p) => matchHost(p, host))) return false;
  if (cfg.allow.length === 0) return true; // 空 allow = 全部推送
  return cfg.allow.some((p) => matchHost(p, host));
}

/** cookie 去重键：domain/path/name + 分区键（分区与未分区视为不同条目）。 */
function ckKey(c) {
  const pk = c.partitionKey || {};
  return [c.storeId || "", c.domain || "", c.path || "/", c.name || "",
    pk.topLevelSite || "", pk.hasCrossSiteAncestors ? "1" : "0"].join("\n");
}

/** 读取全部 cookie（含 Partitioned 分区 cookie）。
 *  官方文档：默认情况下所有 chrome.cookies API 方法都针对"未分区"cookie
 *  运行——getAll({}) 拿不到带 Partitioned 属性的 cookie，而登录流程常以
 *  分区形式写入会话 cookie（如火山 signin 下发的 digest/userInfo，
 *  DevTools 可见但 getAll({}) 查不到）。这里先取未分区全集，再补读分区：
 *  1) 先试空分区键（部分版本等价于"全部分区"）；
 *  2) 再按候选顶级站点逐一读（分区键的 topLevelSite 无法枚举，只能按
 *     已知 cookie 域名、allow 清单及其常见 console/signin/sso 等前缀推导）。 */
async function getAllCookiesWithPartitions(cfg) {
  const base = await chrome.cookies.getAll({});
  const merged = [...base];
  const seen = new Set(base.map((c) => ckKey(c)));
  const addAll = (list) => {
    for (const c of list || []) {
      const k = ckKey(c);
      if (!seen.has(k)) { seen.add(k); merged.push(c); }
    }
  };
  // 1) 空分区键探测：若实现支持"全部分区"，一步到位
  try { addAll(await chrome.cookies.getAll({ partitionKey: {} })); }
  catch (e) { /* 忽略 */ }
  // 2) 候选顶级站点逐一读取（host_permissions 为 <all_urls>，无需额外授权）
  const sites = new Set();
  for (const c of base) {
    if (c.domain && cookieAllowed(c, cfg)) {
      sites.add("https://" + c.domain.replace(/^\./, "").toLowerCase());
    }
  }
  for (const p of cfg.allow) {
    const root = p.replace(/^\*\./, "").replace(/^\./, "").toLowerCase();
    if (!root || root.includes("*")) continue; // IP 通配等无法推导前缀
    for (const sub of ["", "www.", "console.", "signin.", "passport.",
      "sso.", "login.", "account.", "auth.", "portal."]) {
      sites.add("https://" + sub + root);
    }
  }
  for (const site of sites) {
    for (const pk of [{ topLevelSite: site },
      { topLevelSite: site, hasCrossSiteAncestor: true }]) {
      try { addAll(await chrome.cookies.getAll({ partitionKey: pk })); }
      catch (e) { /* Chrome <119 忽略未知字段 → 重复项已被去重 */ }
    }
  }
  return merged;
}

async function pushAll(reason, opts = {}) {
  const cfg = await getConfig();
  const target = cfg.target;
  const { my_profile } = await chrome.storage.local.get("my_profile");
  const profile = parseInt(my_profile, 10) || 0;
  // 变更推送（reason 含"变更"）走值比较；必推时机（启动/定时/手动）不走
  const checkDiff = !!opts.checkDiff || /变更/.test(reason);
  let cookies = [];
  let authorizations = [];
  let success = false;
  let skipped = 0;
  try {
    const all = await getAllCookiesWithPartitions(cfg);
    cookies = all.filter((c) => {
      const ok = cookieAllowed(c, cfg);
      if (!ok) skipped += 1;
      return ok;
    });
    // Authorization 一并推送（观察到的范围内主机的最近值）
    authorizations = await getAuthList(cfg);
    // 值比较：与上次成功推送的快照无真实差异则跳过（不更新 lastPushAt、
    // 不记推送记录——什么都没发；基准保留，下次变更仍会触发）
    if (checkDiff) {
      const { [SNAPSHOT_KEY]: prev } = await chrome.storage.session.get(
        SNAPSHOT_KEY);
      const next = Object.fromEntries(cookies.map(cookieDiffEntry));
      const { [AUTH_MAP_KEY]: authSnap = {} } =
        await chrome.storage.session.get(AUTH_MAP_KEY);
      const authNext = Object.fromEntries(
        authorizations.map((a) => [a.host, a.value]));
      if (!snapshotDiffers(prev || {}, next)
          && !snapshotDiffers(authSnap || {}, authNext)) {
        return { success: true, count: cookies.length, skipped,
                 skippedNoDiff: true };
      }
    }
    lastPushAt = Date.now();
    dirtySinceThrottle = false;
    const resp = await fetch(target, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, cookies, authorizations, profile }),
    });
    success = resp.ok;
    // 推送成功才更新快照基准（失败的变更保留下次重推）
    if (success) {
      await chrome.storage.session.set({
        [SNAPSHOT_KEY]: Object.fromEntries(cookies.map(cookieDiffEntry)),
        [AUTH_MAP_KEY]: Object.fromEntries(
          authorizations.map((a) => [a.host, a.value])),
      });
    }
  } catch (e) {
    success = false;
  }
  await recordHistory({
    time: new Date().toLocaleString("zh-CN", { hour12: false }),
    reason,
    address: target,
    count: cookies.length,
    authCount: authorizations.length,
    domains: [...new Set(
      cookies.map((c) => (c.domain || "").replace(/^\./, "")))],
    // Authorization 所属地址（与 Cookie 的 domains 分开展示，便于排查
    // "auth 推没推到哪台主机"）
    authDomains: [...new Set(authorizations.map((a) => a.host))],
    keys: cookies.map((c) => c.name),
    profile,
    success,
  });
  await setBadge(success);
  return { success, count: cookies.length, authCount: authorizations.length,
           skipped };
}

/** 节流推送：距上次 >= 间隔立即推；否则打标记等补偿 */
// ---------- Authorization 观察（webRequest.onSendHeaders 只读） ----------

/** 主机是否在推送范围内（与 cookie 同一 scope/allow/deny 管道）。
 *  host 为请求的精确主机（Authorization 不跨域携带，按精确匹配语义，
 *  不做 cookie 的子域后缀匹配）。 */
function hostAllowed(host, cfg) {
  if (cfg.scope === "none") return false;
  if (cfg.scope !== "list") return true; // all
  if (cfg.deny.some((p) => matchHost(p, host))) return false;
  if (cfg.allow.length === 0) return true; // 空 allow = 全部推送
  return cfg.allow.some((p) => matchHost(p, host));
}

/** 当前观察到的范围内 Authorization 列表（推送 payload 用）。 */
async function getAuthList(cfg) {
  const { [AUTH_MAP_KEY]: map = {} } =
    await chrome.storage.session.get(AUTH_MAP_KEY);
  return Object.entries(map)
    .map(([host, value]) => ({ host, value }))
    .filter((a) => a.value && hostAllowed(a.host, cfg));
}

/** 观察请求头中的 Authorization：范围内主机的值变化时标脏触发推送。
 *  只读观察（onSendHeaders 不改请求）；同值重复请求不触发。
 *  不带 Authorization 的请求直接忽略——同一站点多数接口不带该头
 *  （如仅登录/鉴权接口带），把 map[host] 记成空串会让"从未有值"的主机
 *  也出现在 Authorization 所属地址里（无效噪音）；值真实消失的场景由
 *  推送整体替换语义自然处理（服务端按快照全量替换）。 */
function observeAuthHeaders(details) {
  (async () => {
    try {
      const headers = details.requestHeaders || [];
      const auth = headers.find(
        (h) => h.name && h.name.toLowerCase() === "authorization");
      if (!auth || !auth.value) return; // 本请求未带 Authorization
      const host = normalizeHost(
        new URL(details.url).hostname) || details.url;
      const value = auth.value;
      const cfg = await getConfig();
      if (!hostAllowed(host, cfg)) return; // 范围外：连标脏都不做
      const { [AUTH_MAP_KEY]: map = {} } =
        await chrome.storage.session.get(AUTH_MAP_KEY);
      if (map[host] === value) return; // 值未变（重复请求同值）
      map[host] = value;
      await chrome.storage.session.set({ [AUTH_MAP_KEY]: map });
      pushThrottled("Authorization变更");
    } catch (e) { /* URL 解析失败等忽略 */ }
  })();
}

chrome.webRequest.onSendHeaders.addListener(
  observeAuthHeaders,
  { urls: ["<all_urls>"] },
  ["requestHeaders"]
);

async function pushThrottled(reason) {
  const cfg = await getConfig();
  if (Date.now() - lastPushAt >= cfg.throttleMs) {
    await pushAll(reason, { checkDiff: true });
  } else {
    dirtySinceThrottle = true;
    // 补偿推送（alarm 实际触发可能晚于设定，由浏览器调度）
    await chrome.alarms.create("flush", { delayInMinutes: 0.5 });
  }
}

/** 会话保持日志：统一前缀输出到 Service Worker console */
function kaLog(...args) {
  console.log(
    "[会话保持]",
    new Date().toLocaleString("zh-CN", { hour12: false }),
    ...args
  );
}

/** 会话保持：刷新 host 对应的已打开页面。
 *  返回值：true=已刷新；"cancelled"=用户在确认弹窗中取消；false=无匹配页面
 *  或注入失败。多页时取第一个非活动标签页；全部为活动标签页时对第一个弹窗确认。 */
async function refreshHostTab(host) {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: "*://" + host + "/*" });
  } catch (e) {
    kaLog(host, "：查询标签页失败：", e && e.message);
    return false; // 非法 pattern（归一化后不应出现）
  }
  // 找到的标签页：地址与活动状态（tab.active）
  kaLog(
    host, "：找到", tabs.length, "个标签页",
    tabs.map((t) => `#${t.id} ${t.active ? "活动" : "非活动"} ${t.url}`)
  );
  if (!tabs.length) return false;
  const hidden = tabs.filter((t) => !t.active);
  if (hidden.length) {
    const t = hidden[0];
    kaLog(host, "：刷新非活动标签页 #" + t.id, t.url);
    try {
      await chrome.tabs.reload(t.id);
    } catch (e) {
      kaLog(host, "：刷新标签页 #" + t.id + " 失败：", e && e.message);
      return false; // 标签页在查询后被关闭等
    }
    return true;
  }
  // 唯一页面为活动标签页（或多个页面全部为活动标签页）：弹窗确认后再刷新
  const t = tabs[0];
  kaLog(host, "：全部标签页均为活动标签页，弹窗确认 #" + t.id, t.url);
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: t.id },
      func: kaConfirmDialog,
      args: [host],
    });
    if (results && results[0] && results[0].result === true) {
      kaLog(host, "：用户确认刷新 #" + t.id, t.url);
      await chrome.tabs.reload(t.id);
      return true;
    }
    kaLog(host, "：用户取消了刷新（下个间隔才会再次询问）");
    return "cancelled";
  } catch (e) {
    kaLog(host, "：注入确认弹窗失败（下个 tick 重试）：", e && e.message);
    return false;
  }
}

/** 注入当前活动标签页的确认弹窗（chrome.scripting.executeScript 的 func，
 *  在页面隔离世界执行，confirm 会阻塞该页面并弹原生对话框） */
function kaConfirmDialog(host) {
  return window.confirm(
    "chrome_capture_operate 会话保持：即将刷新当前活动的 " + host +
      " 标签页以保持登录会话，刷新可能使未保存的输入丢失。是否刷新？"
  );
}

/** 会话保持 alarm 同步：每个启用项一个 ka_<host> 周期任务（周期=间隔分钟）。
 *  新增建、间隔变化重建、删除/禁用清除；不依赖任何"上次刷新时间"状态，
 *  周期由浏览器 alarm 调度保证（ensureAlarms 统一调用）。 */
async function syncKaAlarms() {
  const cfg = await getConfig();
  const active = new Map();
  for (const it of cfg.kaItems) {
    if (it.enabled) active.set(it.host, it.intervalMin);
  }
  const all = await chrome.alarms.getAll();
  const existing = new Set(
    all.filter((a) => a.name.startsWith("ka_")).map((a) => a.name)
  );
  for (const a of all) {
    if (!a.name.startsWith("ka_")) continue;
    const host = a.name.slice(3);
    if (!active.has(host)) {
      await chrome.alarms.clear(a.name); // 项已删除或已禁用
      continue;
    }
    if (Math.abs(a.periodInMinutes - active.get(host)) > 0.001) {
      await chrome.alarms.clear(a.name); // 间隔变化：重建（无更新接口）
      chrome.alarms.create(a.name, { periodInMinutes: active.get(host) });
    }
  }
  for (const [host, itv] of active) {
    const name = "ka_" + host;
    if (!existing.has(name)) {
      chrome.alarms.create(name, { periodInMinutes: itv });
    }
  }
  kaLog(
    "定时任务已同步：",
    active.size
      ? [...active].map(([h, i]) => `${h}（每${i}分钟）`).join("；")
      : "无启用的配置"
  );
}

chrome.runtime.onStartup.addListener(() => {
  ensureAlarms();
  pushAll("启动时");
  autoOpenOnStartup();
});
chrome.runtime.onInstalled.addListener(() => {
  ensureAlarms();
  pushAll("启动时");
});

chrome.cookies.onChanged.addListener((changeInfo) => {
  // 范围预判（prompt.md：仅范围内变更才触发）：范围外变更直接忽略，
  // 连节流标记都不打。推送前的快照 diff 由 pushAll(checkDiff) 负责。
  if (!changeInfo || !changeInfo.cookie) return;
  getConfig().then((cfg) => {
    if (cookieAllowed(changeInfo.cookie, cfg)) {
      pushThrottled("cookie变更");
    }
  });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "heartbeat") {
    pushAll("定时推送");
  } else if (alarm.name === "flush" && dirtySinceThrottle) {
    pushAll("cookie变更(补推)", { checkDiff: true });
  } else if (alarm.name.startsWith("ka_")) {
    // 到点即刷新：周期由 alarm 保证，不计算距上次刷新的时间
    const host = alarm.name.slice(3);
    kaLog(host, `（间隔 ${Math.round(alarm.periodInMinutes)} 分钟）：定时触发`);
    refreshHostTab(host).then((r) => {
      if (r === true) {
        kaLog(host, "：本次刷新完成");
      } else if (r === "cancelled") {
        kaLog(host, "：用户取消，下个间隔再次触发");
      }
      // 无匹配页面/失败的场景已在 refreshHostTab 内记录
    });
  }
});

async function ensureAlarms() {
  const cfg = await getConfig();
  const hb = await chrome.alarms.get("heartbeat");
  // 心跳间隔变化或不存在时重建（chrome.alarms 无更新接口，先清后建）
  if (!hb || Math.abs(hb.periodInMinutes - cfg.heartbeatMin) > 0.001) {
    await chrome.alarms.clear("heartbeat");
    chrome.alarms.create("heartbeat", { periodInMinutes: cfg.heartbeatMin });
  }
  // 会话保持：每项启用配置独立周期任务（新增建/间隔变化重建/删除禁用清除）
  await syncKaAlarms();
}

// 弹窗"立即推送"走这里，保证推送逻辑与记录单点一致
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "push_now") {
    pushAll("手动推送").then((r) => sendResponse({ ok: r.success }));
    return true; // 异步 sendResponse
  }
});


// ---------- 收藏并打开页面（storage.local.page_collections，全部在插件内实现） ----------

async function getCollections() {
  const { page_collections } = await chrome.storage.local.get(
    "page_collections");
  return Array.isArray(page_collections) ? page_collections : [];
}

/** 收藏当前所有打开的页面（忽略本插件自己的页面）。
 *  返回新收藏记录（含 id/时间/页面列表），无可用页面时返回 null。 */
async function collectPages() {
  const tabs = await chrome.tabs.query({});
  const pages = tabs
    .filter((t) => {
      if (t.url && t.url.startsWith(chrome.runtime.getURL(""))) return false;
      return /^https?:\/\//.test(t.url || "");
    })
    .map((t) => ({ title: t.title || t.url, url: t.url }));
  if (!pages.length) return null;
  const rec = {
    id: "c" + Date.now(),
    time: new Date().toLocaleString("zh-CN", { hour12: false }),
    pages,
  };
  const list = await getCollections();
  list.unshift(rec);
  await chrome.storage.local.set({ page_collections: list });
  // 新收藏默认勾选"Chrome启动时自动打开"（仅当前没有勾选其他收藏时）
  const { auto_open_id } = await chrome.storage.local.get("auto_open_id");
  if (!auto_open_id) {
    await chrome.storage.local.set({ auto_open_id: rec.id });
    bkLog("新收藏默认勾选启动自动打开：", rec.time);
  }
  bkLog("收藏页面：", pages.length, "个页面已记录");
  return rec;
}

/** 删除一条收藏记录（按 id）。 */
async function removeCollection(id) {
  const list = await getCollections();
  const next = list.filter((r) => r.id !== id);
  await chrome.storage.local.set({ page_collections: next });
  await clearAutoOpenIf(id); // 被删的恰是勾选自动打开的那条时清除勾选
  return list.length !== next.length;
}

/** 从某条收藏中批量删除多个页面（按 id + 页面下标数组，倒序删防错位）。
 *  删空后整条收藏自动移除。返回删除的页面数。 */
async function removePagesFromCollection(id, indexes) {
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec) return 0;
  const before = rec.pages.length;
  for (const i of [...indexes].sort((a, b) => b - a)) {
    if (i >= 0 && i < rec.pages.length) rec.pages.splice(i, 1);
  }
  const next = rec.pages.length ? list : list.filter((r) => r.id !== id);
  await chrome.storage.local.set({ page_collections: next });
  if (!rec.pages.length) await clearAutoOpenIf(id); // 收藏删空自动移除
  return before - rec.pages.length;
}

/** 编辑某条收藏中单个页面的标题与地址（按 id + 页面下标）。
 *  地址必须 http(s) 开头；返回是否更新。 */
async function updatePageInCollection(id, index, title, url) {
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec || index < 0 || index >= rec.pages.length) return false;
  const u = String(url || "").trim();
  if (!/^https?:\/\//.test(u)) return false;
  rec.pages[index] = { title: String(title || "").trim() || u, url: u };
  await chrome.storage.local.set({ page_collections: list });
  return true;
}

/** 向某条收藏中追加一个页面（标题 + 地址）。返回是否成功。 */
async function addPageToCollection(id, title, url) {
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec) return false;
  const u = String(url || "").trim();
  if (!/^https?:\/\//.test(u)) return false;
  rec.pages.push({ title: String(title || "").trim() || u, url: u });
  await chrome.storage.local.set({ page_collections: list });
  return true;
}

/** 修改某条收藏的名称。返回是否成功。 */
async function renameCollection(id, name) {
  const n = String(name || "").trim();
  if (!n) return false;
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec) return false;
  rec.name = n;
  await chrome.storage.local.set({ page_collections: list });
  return true;
}

/** 调整某条收藏中页面的顺序（from 下标移到 to 下标）。
 *  页面打开顺序 = 列表顺序，调整顺序即决定批量打开的先后。 */
async function movePageInCollection(id, from, to) {
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec) return false;
  const n = rec.pages.length;
  if (from < 0 || from >= n || to < 0 || to >= n) return false;
  const [page] = rec.pages.splice(from, 1);
  rec.pages.splice(to, 0, page);
  await chrome.storage.local.set({ page_collections: list });
  return true;
}

/** 勾选"Chrome启动时自动打开"的收藏 id 存 storage.local.auto_open_id。
 *  单值存储天然保证"同时只允许勾选一条"（勾选另一条即替换）。
 *  id 为空/undefined 时清除勾选。 */
async function setAutoOpenId(id) {
  if (id) {
    const list = await getCollections();
    if (!list.some((r) => r.id === id)) return false;
    await chrome.storage.local.set({ auto_open_id: id });
  } else {
    await chrome.storage.local.remove("auto_open_id");
  }
  return true;
}

/** 收藏被删除/删空时，若它正是勾选了启动自动打开的那条，清除勾选。 */
async function clearAutoOpenIf(id) {
  const { auto_open_id } = await chrome.storage.local.get("auto_open_id");
  if (auto_open_id && auto_open_id === id) {
    await chrome.storage.local.remove("auto_open_id");
    bkLog("勾选启动自动打开的收藏已删除，清除勾选");
  }
}

/** Chrome 启动时自动打开勾选的收藏（onStartup 触发，与"启动时"推送
 *  同一入口；打开方式与手动批量打开一致：按列表顺序 ~0.5s 间隔、
 *  后台标签页，避免瞬时开太多）。 */
async function autoOpenOnStartup() {
  try {
    const { auto_open_id } = await chrome.storage.local.get("auto_open_id");
    if (!auto_open_id) return;
    const rec = (await getCollections()).find((r) => r.id === auto_open_id);
    if (!rec || !rec.pages.length) return;
    bkLog("Chrome 启动自动打开收藏：", rec.name || rec.time,
          "共", rec.pages.length, "个页面");
    for (let i = 0; i < rec.pages.length; i++) {
      await chrome.tabs.create({ url: rec.pages[i].url, active: false });
      if (i < rec.pages.length - 1) {
        await new Promise((r) => setTimeout(r, 500));
      }
    }
  } catch (e) {
    bkLog("Chrome 启动自动打开失败：", e && e.message);
  }
}

/** 批量依次打开某条收藏的全部页面（间隔打开，避免瞬时开太多标签页）。 */
async function openCollection(id) {
  const list = await getCollections();
  const rec = list.find((r) => r.id === id);
  if (!rec || !rec.pages.length) return 0;
  bkLog("批量打开收藏：", rec.time, "共", rec.pages.length, "个页面");
  for (let i = 0; i < rec.pages.length; i++) {
    await chrome.tabs.create({ url: rec.pages[i].url, active: false });
    if (i < rec.pages.length - 1) {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  return rec.pages.length;
}

/** 导入收藏数据（整体替换）。校验结构：数组、每条含页面数组、
 *  页面地址 http(s)。导入后重排 id 防与现存冲突。返回导入条数。 */
async function importCollections(data) {
  if (!Array.isArray(data)) return 0;
  const list = [];
  for (const rec of data) {
    if (!rec || !Array.isArray(rec.pages) || !rec.pages.length) continue;
    const pages = rec.pages
      .filter((p) => p && typeof p.url === "string"
        && /^https?:\/\//.test(p.url))
      .map((p) => ({
        title: String(p.title || p.url),
        url: String(p.url),
      }));
    if (!pages.length) continue;
    list.push({
      id: "c" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
      time: String(rec.time || new Date().toLocaleString("zh-CN",
        { hour12: false })),
      name: String(rec.name || ""),
      pages,
    });
  }
  await chrome.storage.local.set({ page_collections: list });
  // 导入会重建全部 id，原勾选的 id 无法对应——清除，需人工重新勾选
  await chrome.storage.local.remove("auto_open_id");
  bkLog("导入收藏：", list.length, "条记录");
  return list.length;
}

/** 收藏功能日志：统一前缀输出到 Service Worker console */
function bkLog(...args) {
  console.log(
    "[收藏页面]",
    new Date().toLocaleString("zh-CN", { hour12: false }),
    ...args
  );
}

// options 页通过消息调用收藏能力（保证读写 storage 与日志单点一致）
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const done = (r) => {
    try { sendResponse({ ok: true, result: r }); }
    catch (e) { /* 接收方已关闭时忽略 */ }
  };
  if (msg && msg.type === "bk_collect") {
    collectPages().then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_list") {
    Promise.all([getCollections(),
                 chrome.storage.local.get("auto_open_id")])
      .then(([list, { auto_open_id }]) =>
        sendResponse({ ok: true, result: list,
                       auto_open_id: auto_open_id || "" }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_set_auto_open") {
    setAutoOpenId(msg.id).then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_remove") {
    removeCollection(msg.id).then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_remove_page") {
    // 单个删除：index 是数字，removePagesFromCollection 期望数组——
    // 必须包一层，否则函数内 [...indexes] 对数字展开抛 TypeError
    // （promise 被吞、storage 不写入、页面不删除）
    removePagesFromCollection(msg.id, [msg.index])
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_remove_pages") {
    removePagesFromCollection(msg.id, msg.indexes)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_update_page") {
    updatePageInCollection(msg.id, msg.index, msg.title, msg.url)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_add_page") {
    addPageToCollection(msg.id, msg.title, msg.url)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_rename") {
    renameCollection(msg.id, msg.name)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_move_page") {
    movePageInCollection(msg.id, msg.from, msg.to)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_open") {
    openCollection(msg.id).then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg && msg.type === "bk_import") {
    importCollections(msg.collections)
      .then(done).catch(() => sendResponse({ ok: false }));
    return true;
  }
});

// 配置变更：立即按新间隔重建心跳（options 保存后发消息）
chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "config_changed") {
    ensureAlarms();
  }
});

// Service Worker 被唤醒时确保心跳存在
ensureAlarms();
