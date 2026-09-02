const DEFAULT_TARGET = "http://127.0.0.1:33445/api/cookies/push";
const DEFAULT_THROTTLE_SEC = 10;
const DEFAULT_HEARTBEAT_MIN = 5;
const DEFAULT_KA_INTERVAL_MIN = 30;
// 配置导入导出范围：仅配置参数键，push_history 等历史记录不在范围
const CONFIG_KEYS = [
  "push_target",
  "throttle_sec",
  "heartbeat_min",
  "push_scope",
  "allow_list",
  "deny_list",
  "keepalive_items",
];

function $(id) {
  return document.getElementById(id);
}

function currentScope() {
  const checked = document.querySelector('input[name=scope]:checked');
  return checked ? checked.value : "all";
}

// 标题显示插件构建时间（到秒，取 options.html data-build 属性）
(function showBuildTime() {
  const el = document.getElementById("buildTime");
  if (el) {
    el.textContent = "（构建时间 " + (el.dataset.build || "未知") + "）";
  }
})();

function updateScopeUI() {
  const scope = currentScope();
  $("scopeWarn").style.display = scope === "none" ? "" : "none";
  // 页面醒目橙色横幅提醒（持续显示，修改推送范围后消失）：
  // 当前Cookie推送范围为全部禁止，不会推送任何Cookie
  const bannerEl = $("scopeToast");
  if (scope === "none") {
    bannerEl.textContent = "当前选择的Cookie推送范围为全部禁止，不会推送任何Cookie，需要修改为全部允许，或按指定范围推送";
    // 不能设 display:""——CSS 里 #scopeToast 是 display:none，清空内联
    // style 后元素回退为隐藏；必须显式 display:block（横幅此前不显示的根因）
    bannerEl.style.display = "block";
  } else {
    bannerEl.style.display = "none";
  }
  $("listCfg").style.display = scope === "list" ? "" : "none";
}
document.querySelectorAll('input[name=scope]').forEach((r) => {
  r.addEventListener("change", updateScopeUI);
});

// TAB 切换：参数配置 / 推送记录
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    document
      .querySelectorAll(".tab-panel")
      .forEach((p) => p.classList.toggle("active", p.id === `panel-${btn.dataset.tab}`));
  });
});

async function load() {
  const {
    push_target,
    push_history,
    throttle_sec,
    heartbeat_min,
    push_scope,
    allow_list,
    deny_list,
    keepalive_items,
    my_profile,
  } = await chrome.storage.local.get([
    "push_target",
    "push_history",
    "throttle_sec",
    "heartbeat_min",
    "push_scope",
    "allow_list",
    "deny_list",
    "keepalive_items",
    "my_profile",
  ]);
  $("target").value = push_target || DEFAULT_TARGET;
  $("myProfile").value = parseInt(my_profile, 10) || 0;
  $("throttleSec").value = throttle_sec || DEFAULT_THROTTLE_SEC;
  $("heartbeatMin").value = heartbeat_min || DEFAULT_HEARTBEAT_MIN;
  // 默认 none（全部禁止）：未配置或非法值回退 none
  const scope =
    push_scope === "all" || push_scope === "list" ? push_scope : "none";
  document.querySelector(`input[name=scope][value=${scope}]`).checked = true;
  $("allowList").value = Array.isArray(allow_list) ? allow_list.join("\n") : "";
  $("denyList").value = Array.isArray(deny_list) ? deny_list.join("\n") : "";
  updateScopeUI();
  renderState(push_history || []);
  renderHistory(push_history || []);
  renderKaRows(keepalive_items);
}

function renderState(history) {
  const el = $("state");
  if (!history.length) {
    el.textContent = "状态：尚未推送过";
    el.className = "muted";
    return;
  }
  const last = history[0];
  el.textContent = `状态：上次推送${last.success ? "成功" : "失败"}（${last.time}，${last.count} 条）`;
  el.className = last.success ? "ok" : "bad";
}

function renderHistory(history) {
  $("history").innerHTML = history
    .map((h, i) => {
      const keys = Array.isArray(h.keys) ? h.keys : [];
      const keysHtml = keys.length
        ? keys.map((k) => `<span class="badge">${escHtml(String(k))}</span>`).join(" ")
        : '<span class="muted">无</span>';
      const domains = Array.isArray(h.domains) ? h.domains : [];
      h.domainsHtml = domains.length
        ? domains.map((d) => `<span class="badge">${escHtml(String(d))}</span>`).join(" ")
        : '<span class="muted">无</span>';
      return `<tr><td>${h.time}</td><td>${h.profile || 0}</td><td>${escHtml(String(h.reason))}</td>` +
        `<td>${escHtml(String(h.address))}</td>` +
        `<td>${h.count}</td><td class="${h.success ? "ok" : "bad"}">${
          h.success ? "成功" : "失败"
        }</td>` +
        `<td><button class="ghost bk-view" data-i="${i}" title="查看这次推送的Cookie对应的地址（域名或IP）、Cookie数量与key">查看</button></td>` +
        `<tr class="his-detail" style="display:none"><td colspan="7" style="background:#fafbfc">` +
        `<div class="muted">实例编号：${h.profile || 0}｜Cookie数量：${h.count}</div>` +
        `<div style="margin-top:4px">Cookie对应的地址（域名或IP）：${h.domainsHtml}</div>` +
        `<div style="margin-top:4px">Cookie key：${keysHtml}</div></td></tr>`;
    })
    .join("");
  // 查看按钮：切换该次推送的详情行
  $("history").querySelectorAll(".bk-view").forEach((btn) => {
    btn.onclick = () => {
      const row = btn.closest("tr");
      const detail = row.nextElementSibling;
      if (detail && detail.classList.contains("his-detail")) {
        const show = detail.style.display === "none";
        detail.style.display = show ? "" : "none";
        btn.textContent = show ? "收起" : "查看";
      }
    };
  });
}

// ---------- 参数配置：保存 / 恢复默认 / 立即推送 ----------

$("save").onclick = async () => {
  const v = $("target").value.trim();
  if (!/^https?:\/\//.test(v)) {
    alert("请输入合法的 http(s) 地址");
    return;
  }
  const throttle = parseInt($("throttleSec").value, 10);
  if (!throttle || throttle < 10) {
    alert("cookie变更推送间隔最小为 10 秒");
    $("throttleSec").value = DEFAULT_THROTTLE_SEC;
    return;
  }
  const hb = parseFloat($("heartbeatMin").value);
  if (!hb || hb < 1) {
    alert("定时推送间隔最小为 1 分钟");
    $("heartbeatMin").value = DEFAULT_HEARTBEAT_MIN;
    return;
  }
  const profile = parseInt($("myProfile").value, 10);
  if (isNaN(profile) || profile < 0) {
    alert("Chrome实例编号必须是不小于 0 的整数");
    $("myProfile").value = 0;
    return;
  }
  const scope = currentScope();
  const allow = $("allowList").value.split("\n").map((x) => x.trim()).filter(Boolean);
  const deny = $("denyList").value.split("\n").map((x) => x.trim()).filter(Boolean);
  await chrome.storage.local.set({
    push_target: v,
    throttle_sec: throttle,
    heartbeat_min: hb,
    push_scope: scope,
    allow_list: allow,
    deny_list: deny,
    my_profile: profile,
  });
  chrome.runtime.sendMessage({ type: "config_changed" });
  alert("已保存");
};

$("reset").onclick = async () => {
  await chrome.storage.local.remove([
    "push_target",
    "throttle_sec",
    "heartbeat_min",
    "push_scope",
    "allow_list",
    "deny_list",
    "my_profile",
  ]);
  chrome.runtime.sendMessage({ type: "config_changed" });
  load();
};

$("pushNow").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "push_now" });
  load();
};

$("pushNow2").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "push_now" });
  load();
};

$("refreshHis").onclick = () => load();


// ---------- 会话保持（行式编辑：域名或IP / 间隔分钟 / 启用 / 删除） ----------

function kaRowEl(item) {
  const div = document.createElement("div");
  div.className = "ka-row";
  div.innerHTML =
    '<input type="text" class="ka-host" placeholder="www.baidu.com 或 127.0.0.1（不含端口）" title="需要保持会话的域名或IP">' +
    '<input type="number" class="ka-interval" min="1" step="1" title="刷新间隔（分钟）"> 分钟' +
    '<label class="ka-en"><input type="checkbox" class="ka-enabled"> 启用</label>' +
    '<button class="ghost ka-del" title="删除本行配置">删除</button>';
  div.querySelector(".ka-host").value = item ? item.host || "" : "";
  div.querySelector(".ka-interval").value =
    item && item.interval_min ? item.interval_min : DEFAULT_KA_INTERVAL_MIN;
  div.querySelector(".ka-enabled").checked = item ? !!item.enabled : true;
  div.querySelector(".ka-del").onclick = () => div.remove();
  return div;
}

function renderKaRows(items) {
  const box = $("kaRows");
  box.innerHTML = "";
  const list = Array.isArray(items) ? items.filter(Boolean) : [];
  (list.length ? list : [null]).forEach((it) => box.appendChild(kaRowEl(it)));
}

function collectKaItems() {
  const items = [];
  for (const row of document.querySelectorAll("#kaRows .ka-row")) {
    const raw = row.querySelector(".ka-host").value.trim();
    if (!raw) continue; // 空行忽略
    // 与后台 normalizeHost 相同的归一化（去协议/路径/参数/端口）
    let h = raw.toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, "");
    h = h.split("/")[0].split("?")[0].split("#")[0].split(":")[0];
    if (!/^[a-z0-9][a-z0-9.-]*$/.test(h)) {
      throw new Error("域名或IP不合法：" + raw);
    }
    const itv = parseInt(row.querySelector(".ka-interval").value, 10);
    if (!itv || itv < 1) {
      throw new Error("刷新间隔最小为 1 分钟：" + raw);
    }
    items.push({
      host: h,
      interval_min: itv,
      enabled: row.querySelector(".ka-enabled").checked,
    });
  }
  const hosts = items.map((i) => i.host);
  if (new Set(hosts).size !== hosts.length) {
    throw new Error("存在重复的域名或IP");
  }
  return items;
}

$("kaAdd").onclick = () => $("kaRows").appendChild(kaRowEl(null));

$("kaSave").onclick = async () => {
  let items;
  try {
    items = collectKaItems();
  } catch (e) {
    alert(e.message);
    return;
  }
  await chrome.storage.local.set({ keepalive_items: items });
  // 通知后台按新配置重建会话保持 alarm
  chrome.runtime.sendMessage({ type: "config_changed" });
  alert("已保存");
};

$("kaReset").onclick = async () => {
  await chrome.storage.local.remove(["keepalive_items", "keepalive_last_refresh"]);
  chrome.runtime.sendMessage({ type: "config_changed" });
  load();
};

// ---------- 配置导入导出 ----------

$("exportCfg").onclick = async () => {
  const config = await chrome.storage.local.get(CONFIG_KEYS);
  const blob = new Blob(
    [
      JSON.stringify(
        {
          app: "chrome_capture_operate_extension",
          version: 1,
          exported_at: new Date().toLocaleString("zh-CN", { hour12: false }),
          config,
        },
        null,
        2
      ),
    ],
    { type: "application/json" }
  );
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "chrome_capture_operate_config.json";
  a.click();
  URL.revokeObjectURL(a.href);
};

$("importCfg").onclick = () => $("importFile").click();

$("importFile").onchange = async (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let obj;
  try {
    obj = JSON.parse(await file.text());
  } catch (err) {
    alert("导入失败：文件不是合法的 JSON");
    return;
  }
  // 兼容带 app 标识包裹的导出格式与裸键对象格式；仅接受已知配置键
  const config =
    obj && obj.config && typeof obj.config === "object" ? obj.config : obj;
  const picked = {};
  let n = 0;
  for (const k of CONFIG_KEYS) {
    if (config[k] !== undefined) {
      picked[k] = config[k];
      n += 1;
    }
  }
  if (!n) {
    alert("导入失败：文件中没有可识别的配置参数");
    return;
  }
  await chrome.storage.local.set(picked);
  chrome.runtime.sendMessage({ type: "config_changed" });
  alert("已导入 " + n + " 项配置");
  load();
};

load();

// ---------- 收藏并打开页面（全部在插件内实现，storage.local 持久） ----------

function bkCall(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (r) => resolve(r || { ok: false }));
  });
}

function escHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function bkRowEl(page, collId, index, total) {
  const div = document.createElement("div");
  div.className = "ka-row";
  div.innerHTML =
    '<label class="ka-en" style="flex:none" title="勾选后可用列表上方的“删除勾选”批量删除"><input type="checkbox" class="bk-ck"></label>' +
    '<span style="width:24px;text-align:center;color:#888;flex:none">' +
      (index + 1) + '</span>' +
    '<span class="bk-title" style="width:26%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' +
      escHtml(page.title) + '">' + escHtml(page.title) + '</span>' +
    '<span class="bk-url" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#666" title="' +
      escHtml(page.url) + '">' + escHtml(page.url) + '</span>' +
    '<span style="flex:none;color:#888" title="把这个页面移动到输入的序号位置（序号即批量打开的先后顺序）">移到 <input type="number" class="bk-move-to" min="1" step="1" style="width:52px" placeholder="序号"> 位</span>' +
    '<button class="ghost bk-move-go" title="移动到输入的序号位置（序号即批量打开的先后顺序，回车亦可）">移到</button>' +
    '<button class="ghost bk-open-one" title="单独打开这个页面">打开</button>' +
    '<button class="ghost bk-edit" title="编辑这个页面的标题与地址">编辑</button>' +
    '<button class="ghost bk-del-one" title="从这条收藏中删除这个页面">删除</button>';
  div.querySelector(".bk-open-one").onclick = () =>
    chrome.tabs.create({ url: page.url, active: false });
  // 移到指定序号（1 起）：序号即批量打开时的先后顺序，回车亦可
  const doMove = async () => {
    const v = parseInt(div.querySelector(".bk-move-to").value, 10);
    if (isNaN(v) || v < 1 || v > total) {
      alert("目标序号需为 1~" + total + " 的整数");
      return;
    }
    if (v - 1 === index) return; // 已在该位置
    const r = await bkCall({ type: "bk_move_page", id: collId,
                             from: index, to: v - 1 });
    if (!r.ok || !r.result) { alert("移动失败（插件需重新加载？）"); return; }
    loadCollections();
  };
  div.querySelector(".bk-move-go").onclick = doMove;
  div.querySelector(".bk-move-to").onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); doMove(); }
  };
  div.querySelector(".bk-del-one").onclick = async () => {
    const r = await bkCall({ type: "bk_remove_page", id: collId, index });
    if (!r.ok || !r.result) { alert("删除失败（插件需重新加载？）"); return; }
    loadCollections();
  };
  div.querySelector(".bk-edit").onclick = () => {
    div.innerHTML =
      '<span style="width:34%"><input type="text" class="bk-edit-title" value="' +
        escHtml(page.title) + '" title="页面标题" style="width:100%"></span>' +
      '<span style="flex:1"><input type="text" class="bk-edit-url" value="' +
        escHtml(page.url) + '" title="页面地址（http/https 开头）" style="width:100%"></span>' +
      '<button class="ghost bk-save" title="保存修改">保存</button>' +
      '<button class="ghost bk-cancel" title="放弃修改">取消</button>';
    div.querySelector(".bk-cancel").onclick = () => loadCollections();
    div.querySelector(".bk-save").onclick = async () => {
      const title = div.querySelector(".bk-edit-title").value.trim();
      const url = div.querySelector(".bk-edit-url").value.trim();
      if (!/^https?:\/\//.test(url)) {
        alert("地址必须以 http:// 或 https:// 开头");
        return;
      }
      const r = await bkCall({
        type: "bk_update_page", id: collId, index, title, url,
      });
      if (!r.ok || !r.result) { alert("保存失败（地址不合法或插件需重新加载）"); return; }
      loadCollections();
    };
  };
  return div;
}

const bkExpanded = new Set();
// 勾选"Chrome启动时自动打开"的收藏 id（单值，同时只允许一条；由 bk_list 返回）
let bkAutoOpenId = "";

function bkCollEl(rec) {
  const box = document.createElement("div");
  box.className = "scope-box";
  const expanded = bkExpanded.has(rec.id);
  const head = document.createElement("div");
  head.className = "ka-row";
  head.innerHTML =
    '<label class="ka-en" style="flex:none"><input type="checkbox" class="bk-auto-open" title="勾选后 Chrome 启动时自动打开这条收藏的全部页面（同时只能勾选一条收藏，勾选其他收藏会替换；打开方式与批量打开相同）"> 启动时自动打开</label>' +
    '<b style="flex:1" title="点击修改这条收藏的名称">' +
      escHtml(rec.name || rec.time) + '（' + rec.pages.length + ' 个页面）</b>' +
    '<button class="ghost bk-rename" title="修改这条收藏的名称">重命名</button>' +
    '<button class="bk-open-all" title="把这条收藏的全部页面批量依次打开（每个间隔约半秒，顺序为列表顺序）">批量打开</button>' +
    '<button class="ghost bk-toggle" title="展开或收起这条收藏的页面列表">' +
      (expanded ? "收起" : "查看") + '</button>' +
    '<button class="ghost bk-del" title="删除整条收藏记录">删除</button>';
  const list = document.createElement("div");
  list.style.display = expanded ? "" : "none";
  list.style.marginTop = "6px";
  rec.pages.forEach((p, i) =>
    list.appendChild(bkRowEl(p, rec.id, i, rec.pages.length)));
  const makeBar = () => {
    const bar = document.createElement("div");
    bar.className = "ka-row";
    bar.innerHTML =
      '<label class="ka-en"><input type="checkbox" class="bk-ck-all"> 全选</label>' +
      '<button class="ghost bk-del-sel" title="删除勾选的页面">删除勾选</button>' +
      '<button class="ghost bk-add-page" title="向这条收藏追加一个页面（输入标题与地址）">添加页面</button>';
    bar.querySelector(".bk-ck-all").onchange = (e) => {
      list.querySelectorAll(".bk-ck").forEach((c) => { c.checked = e.target.checked; });
    };
    bar.querySelector(".bk-del-sel").onclick = async () => {
      const indexes = [...list.querySelectorAll(".bk-ck")]
        .map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0);
      if (!indexes.length) { alert("请先勾选要删除的页面"); return; }
      if (!confirm("删除勾选的 " + indexes.length + " 个页面？")) return;
      await bkCall({ type: "bk_remove_pages", id: rec.id, indexes });
      loadCollections();
    };
    // 添加页面：标题与地址在同一输入行填写（不用浏览器原生弹出框，
    // 丑且要弹两次）；行出现在页面列表末尾，回车或点"添加"确认
    bar.querySelector(".bk-add-page").onclick = () => {
      if (list.querySelector(".bk-add-row")) {
        list.querySelector(".bk-add-row .bk-add-url").focus();
        return; // 已有添加行：聚焦不重复插入
      }
      const row = document.createElement("div");
      row.className = "ka-row bk-add-row";
      row.innerHTML =
        '<input type="text" class="bk-add-title" placeholder="页面标题（留空则使用地址）" title="新页面的标题" style="width:26%">' +
        '<input type="text" class="bk-add-url" placeholder="页面地址（需以 http:// 或 https:// 开头）" title="新页面的地址" style="flex:1">' +
        '<button class="ghost bk-add-save" title="把这个页面追加到本条收藏的末尾（回车亦可）">添加</button>' +
        '<button class="ghost bk-add-cancel" title="放弃本次添加">取消</button>';
      const doAdd = async () => {
        const title = row.querySelector(".bk-add-title").value.trim();
        const url = row.querySelector(".bk-add-url").value.trim();
        if (!/^https?:\/\//.test(url)) {
          alert("地址必须以 http:// 或 https:// 开头");
          return;
        }
        const r = await bkCall({ type: "bk_add_page", id: rec.id,
                                 title, url });
        if (!r.ok || !r.result) { alert("添加失败（地址不合法或插件需重新加载）"); return; }
        loadCollections();
      };
      row.querySelector(".bk-add-save").onclick = doAdd;
      row.querySelector(".bk-add-cancel").onclick = () => row.remove();
      row.querySelector(".bk-add-title").onkeydown =
      row.querySelector(".bk-add-url").onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); doAdd(); }
      };
      list.appendChild(row);
      row.querySelector(".bk-add-url").focus();
    };
    return bar;
  };
  let bar = null;
  const showBar = () => {
    if (!bar) { bar = makeBar(); list.insertBefore(bar, list.firstChild); }
  };
  if (expanded) showBar();

  head.querySelector(".bk-toggle").onclick = (e) => {
    const show = list.style.display === "none";
    list.style.display = show ? "" : "none";
    if (show) { bkExpanded.add(rec.id); showBar(); }
    else { bkExpanded.delete(rec.id); }
    e.target.textContent = show ? "收起" : "查看";
  };
  head.querySelector(".bk-del").onclick = async () => {
    if (!confirm("删除这条收藏（" + (rec.name || rec.time) + "）？")) return;
    await bkCall({ type: "bk_remove", id: rec.id });
    bkExpanded.delete(rec.id);
    loadCollections();
  };
  head.querySelector(".bk-open-all").onclick = async (e) => {
    e.target.disabled = true;
    const r = await bkCall({ type: "bk_open", id: rec.id });
    e.target.disabled = false;
    if (r.ok) alert("已批量打开 " + r.result + " 个页面");
  };
  const doRename = async () => {
    const cur = rec.name || rec.time;
    const n = prompt("修改收藏名称：", cur);
    if (n === null) return;
    if (!n.trim()) { alert("名称不能为空"); return; }
    const r = await bkCall({ type: "bk_rename", id: rec.id, name: n });
    if (!r.ok || !r.result) { alert("重命名失败"); return; }
    loadCollections();
  };
  // 启动时自动打开勾选（单值：勾选本条即替换其他条，取消仅清本条）
  const autoCk = head.querySelector(".bk-auto-open");
  autoCk.checked = rec.id === bkAutoOpenId;
  autoCk.onchange = async () => {
    const r = await bkCall({ type: "bk_set_auto_open",
                             id: autoCk.checked ? rec.id : null });
    if (!r.ok || !r.result) { alert("设置失败（插件需重新加载？）"); }
    loadCollections(); // 重渲染同步全部收藏的勾选状态
  };
  head.querySelector(".bk-rename").onclick = doRename;
  head.querySelector("b").onclick = doRename;
  box.appendChild(head);
  box.appendChild(list);
  return box;
}

async function loadCollections() {
  const box = $("bkList");
  if (!box) return;
  box.innerHTML = "";
  const r = await bkCall({ type: "bk_list" });
  const list = (r.ok && r.result) || [];
  bkAutoOpenId = (r.ok && r.auto_open_id) || "";
  if (!list.length) {
    box.innerHTML = '<div class="muted" style="padding:8px 0">暂无收藏记录——点击"收藏当前已打开页面"把当前打开的网页保存为一条收藏。</div>';
    return;
  }
  list.forEach((rec) => box.appendChild(bkCollEl(rec)));
}

$("bkCollect").onclick = async () => {
  const r = await bkCall({ type: "bk_collect" });
  if (!r.ok) { alert("收藏失败"); return; }
  if (!r.result) { alert("当前没有可收藏的网页（忽略本插件页面与新标签页）"); return; }
  alert("已收藏 " + r.result.pages.length + " 个页面");
  loadCollections();
};

$("bkExport").onclick = async () => {
  const r = await bkCall({ type: "bk_list" });
  const list = (r.ok && r.result) || [];
  if (!list.length) { alert("暂无收藏记录可导出"); return; }
  const blob = new Blob(
    [JSON.stringify({ app: "chrome_capture_operate_extension",
      kind: "page_collections", version: 1, collections: list }, null, 2)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "chrome_capture_operate_collections.json";
  a.click();
  URL.revokeObjectURL(a.href);
};

$("bkImport").onclick = () => $("bkImportFile").click();

$("bkImportFile").onchange = async (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let obj;
  try { obj = JSON.parse(await file.text()); }
  catch (err) { alert("导入失败：文件不是合法的 JSON"); return; }
  const data = Array.isArray(obj) ? obj
    : (obj && Array.isArray(obj.collections) ? obj.collections : null);
  if (!data) { alert("导入失败：文件中没有可识别的收藏数据"); return; }
  if (!confirm("导入将整体替换现有收藏记录，确定继续？")) return;
  const r = await bkCall({ type: "bk_import", collections: data });
  if (!r.ok) { alert("导入失败"); return; }
  alert("已导入 " + r.result + " 条收藏记录");
  loadCollections();
};

loadCollections();
