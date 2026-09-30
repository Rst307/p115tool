"use strict";
(() => {
  const root = location.pathname.replace(/\/manage\/?$/, "");
  const $ = id => document.getElementById(id);
  let key = "", epoch = 0, view = "library", prefix = "/", offset = 0, tree = true;
  let jobsOffset = 0, groupsOffset = 0, busy = false, resolveConfirm = null;
  let connecting = false;
  const revisions = {library: 0, jobs: 0, groups: 0, overview: 0, detail: 0};
  const controllers = new Set(), pageSize = 50;
  const names = {NORMAL: "个人盘", SHARE: "虚拟分享", CACHE: "恢复缓存"};
  const el = (tag, text, cls) => {const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n;};
  const button = (text, action, cls) => {const n = el("button", text, cls); n.type = "button"; n.addEventListener("click", () => guarded(action)); return n;};
  const bytes = number => {let n = Number(number || 0), i = 0; const units = ["B", "KiB", "MiB", "GiB", "TiB"]; while (n >= 1024 && i < units.length - 1) {n /= 1024; i++;} return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;};
  const date = n => n ? new Date(n * 1000).toLocaleString() : "—";
  const badge = text => el("span", text, `badge ${text}`);
  const notice = text => {$("notice").textContent = text;};
  const messages = {400: "操作参数或确认文字不正确。", 401: "管理密钥无效或已变更，请重新连接。", 404: "对象已不存在，请刷新列表。", 409: "安全检查未通过。请先查看检查点并对账，不要重复远端写操作。", 422: "输入格式不正确，请检查 ID、路径及字段。", 503: "插件暂不可用。请检查插件是否启用。"};

  async function api(path, method = "GET", body, idempotent = false) {
    if (!key) throw new Error("请先连接工作台。");
    const generation = epoch, controller = new AbortController(); controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const headers = {"x-api-key": key};
      if (body !== undefined) headers["content-type"] = "application/json";
      if (idempotent) headers["idempotency-key"] = crypto.randomUUID();
      const response = await fetch(root + path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: "no-store", credentials: "omit", redirect: "error"});
      if (generation !== epoch) throw new Error("页面已锁定。");
      if (response.status === 401) {lock(); throw new Error(messages[401]);}
      if (!response.ok) throw new Error(messages[response.status] || `请求失败（HTTP ${response.status}）。不要盲目重复写操作。`);
      const data = await response.json();
      if (generation !== epoch) throw new Error("页面已锁定。");
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求已中断，操作结果可能不明。后台操作不会因关闭页面而撤销；请先查看任务并对账。");
      throw error;
    } finally {clearTimeout(timer); controllers.delete(controller);}
  }
  async function guarded(action) {
    try {await action();} catch (error) {
      const message = error.message || "操作失败，请刷新并查看任务。";
      notice(message);
      if ($("detail").open) $("operation-result").textContent = message;
    }
  }
  function lock() {
    key = ""; epoch++; controllers.forEach(c => c.abort()); controllers.clear();
    $("key").value = ""; $("workspace").hidden = true; $("login").hidden = false; $("lock").hidden = true;
    for (const id of ["media-table", "jobs-table", "groups-table", "overview", "logs-table", "detail-body", "media-actions", "operation-result", "breadcrumb"]) $(id).replaceChildren();
    $("detail").close(); finishConfirmation(null); $("key").focus();
  }
  function table(container, headings, rows) {
    const t = el("table"), head = el("thead"), tr = el("tr");
    headings.forEach(text => tr.append(el("th", text))); head.append(tr); t.append(head);
    const body = el("tbody");
    rows.forEach(cells => {const row = el("tr"); cells.forEach(value => {const td = el("td"); td.append(value instanceof Node ? value : el("span", value ?? "—")); row.append(td);}); body.append(row);});
    if (!rows.length) {const row = el("tr"), cell = el("td", "没有符合条件的记录。可返回上一级、清除筛选或提交目录扫描。"); cell.colSpan = headings.length; row.append(cell); body.append(row);}
    t.append(body); $(container).replaceChildren(t);
  }
  function crumbs() {
    const nav = $("breadcrumb"); nav.replaceChildren(button("媒体库", () => navigate("/")));
    let path = ""; prefix.split("/").filter(Boolean).forEach(part => {path += "/" + part; const target = path + "/"; nav.append(el("span", "/"), button(part, () => navigate(target)));});
    $("parent").disabled = prefix === "/";
  }
  async function navigate(path) {prefix = path; offset = 0; await loadLibrary();}
  async function loadLibrary() {
    const revision = ++revisions.library;
    const params = new URLSearchParams({prefix, offset, limit: pageSize, q: $("query").value});
    if ($("storage").value) params.set("storage", $("storage").value);
    if ($("status").value) params.set("status", $("status").value);
    const data = await api(`/media${tree ? "/tree" : ""}?${params}`);
    if (revision !== revisions.library) return;
    const rows = data.items.map(item => {
      if (tree) return [button(`${item.is_directory ? "▸  " : ""}${item.name}`, () => item.is_directory ? navigate(item.path) : detail(item.media_id), "entry"),
        item.is_directory ? `${item.media_count} 个媒体` : el("span", `#${item.media_id}`, "mono"), bytes(item.bytes),
        `个人盘 ${item.normal_count} / 分享 ${item.share_count} / 缓存 ${item.cache_count}`, item.abnormal_count ? el("span", `${item.abnormal_count} 项异常`, "abnormal") : "—"];
      return [button(item.file_name, () => detail(item.id), "entry"), el("span", item.virtual_path, "mono"), bytes(item.size), badge(item.storage_type), badge(item.status)];
    });
    table("media-table", ["目录 / 文件", tree ? "内容" : "虚拟路径", "容量", "存储位置", "状态"], rows);
    crumbs(); $("summary").textContent = `${tree ? "目录条目" : "媒体"}共 ${data.total} 项 · 搜索范围：${prefix}`;
    $("page-label").textContent = `${data.total ? offset + 1 : 0}–${Math.min(offset + pageSize, data.total)} / ${data.total}`;
    $("previous").disabled = offset === 0; $("next").disabled = offset + pageSize >= data.total;
    $("tree-mode").textContent = tree ? "切换文件列表" : "切换目录导航";
  }
  function finishConfirmation(result) {
    $("confirm").close(); $("confirmation").value = "";
    const fields = $("confirm-form").querySelector(".extra-fields");
    if (fields) {fields.querySelectorAll("input").forEach(input => input.value = ""); fields.remove();}
    $("confirm-form").onsubmit = null;
    const resolve = resolveConfirm; resolveConfirm = null; if (resolve) resolve(result);
  }
  function confirmation(title, message, expected = "", fields = []) {
    if (resolveConfirm) return Promise.resolve(null);
    $("confirm-title").textContent = title; $("confirm-message").textContent = message + (expected ? `\n请输入：${expected}` : "");
    $("confirmation-label").hidden = !expected; $("confirmation").required = !!expected; $("confirmation").value = "";
    const extra = el("div", null, "extra-fields");
    fields.forEach(field => {const label = el("label", field.label), input = el("input"); input.name = field.name; input.type = field.secret ? "password" : "text"; input.required = true; input.autocomplete = "off"; if (field.max) input.maxLength = field.max; label.append(input); extra.append(label);});
    $("confirmation-label").before(extra);
    return new Promise(resolve => {
      resolveConfirm = resolve;
      $("confirm-form").onsubmit = event => {
        event.preventDefault(); if (expected && $("confirmation").value !== expected) {$("confirmation").setCustomValidity("请完整输入指定确认文字。"); $("confirmation").reportValidity(); return;}
        const values = {}; extra.querySelectorAll("input").forEach(input => values[input.name] = input.value);
        finishConfirmation({confirmation: expected, ...values});
      };
      $("confirm").showModal(); (expected ? $("confirmation") : extra.querySelector("input") || $("confirm-cancel")).focus();
    });
  }
  async function write(title, operation, expected = "", message = "这会发起操作。请核对当前对象与任务检查点。", fields = []) {
    if (busy) return;
    const consent = await confirmation(title, message, expected, fields); if (!consent || !key) return;
    busy = true; const generation = epoch;
    try {const result = await operation(consent); if (generation !== epoch) return; $("operation-result").textContent = JSON.stringify(result, null, 2); notice(`${title}：请求已完成。后台任务请查看任务页；不要重复提交。`); await refresh();}
    finally {busy = false;}
  }
  const enqueue = (kind, payload, consent = {}) => api("/jobs", "POST", {kind, payload, confirmation: consent.confirmation || ""}, true);
  async function detail(id) {
    const revision = ++revisions.detail;
    const media = await api(`/media/${id}`), body = el("dl");
    if (revision !== revisions.detail) return;
    const labels = [["媒体 ID", media.id], ["标题", media.title], ["文件名", media.file_name], ["虚拟路径", media.virtual_path], ["大小", bytes(media.size)], ["存储位置", names[media.storage_type]], ["状态", media.status], ["源文件", media.source_deleted ? "已删除（虚拟映射保留）" : "保留"], ["TMDB", media.tmdb_id], ["STRM", media.strm_path], ["更新时间", date(media.updated_at)]];
    labels.forEach(([name, value]) => body.append(el("dt", name), el("dd", value ?? "—")));
    $("detail-title").textContent = media.title; $("detail-body").replaceChildren(body); $("operation-result").replaceChildren();
    const actions = $("media-actions"); actions.replaceChildren();
    const add = (title, action, cls) => actions.append(button(title, action, cls));
    const post = (path, body = {media_id: id}) => api(path, "POST", body);
    const read = async (path, body) => {$("operation-result").textContent = JSON.stringify(await post(path, body), null, 2);};
    add("生成 STRM", () => write("生成 STRM", () => enqueue("generate", {media_id: id})));
    add("整理预览（不写远端）", () => read("/organize/preview"));
    add("自动整理状态（本地）", () => read("/organize/auto/status"));
    add("提交自动整理", () => write("自动识别整理", () => enqueue("auto_organize", {media_id: id}), "", "将按插件配置识别、创建目录、移动及重命名。默认关闭，未开启或身份不明确时拒绝。"));
    add("整理结果对账", () => write("整理结果对账", () => post("/organize/reconcile"), "", "仅读取远端移动/重命名结果，不继续剩余写操作。"));
    add("续做自动整理", () => write("续做自动整理", c => post("/organize/auto", {media_id: id, resume: true, confirmation: c.confirmation}), `AUTO_RESUME:${id}`, "仅继续已经冻结计划的剩余步骤，不改变目标；未知目录创建不会强制重发。"));
    add("创建分享并验证（保留源）", () => write("创建分享并验证", () => enqueue("archive", {media_id: id, delete: false})));
    if (!media.source_deleted) add("归档并删除源文件…", () => write("归档并删除源文件", c => enqueue("archive", {media_id: id, delete: true}, c), `DELETE:${id}`, `媒体 #${id}：${media.file_name}\n验证分享与STRM后删除个人盘源文件至回收站。分享不等于备份！配置未允许删除时拒绝。`), "danger");
    add("验证分享（含 Range）", () => write("验证分享", () => post("/share/verify", {media_id: id, deep: true})));
    add("分享重新归档（不删源）", () => write("分享重新归档", () => post("/share/repair")));
    add("接入已创建分享…", () => write("接入已创建分享", c => post("/share/attach", {media_id: id, share_code: c.code, receive_code: c.password}), "", "用于创建结果不明时的人工接入。会验证身份，不重新创建分享。", [{name: "code", label: "分享代码", max: 64}, {name: "password", label: "提取码", secret: true, max: 4}]));
    add("接入重新归档结果…", () => write("接入重新归档结果", c => post("/share/repair/attach", {media_id: id, share_code: c.code, receive_code: c.password}), "", "只接入当前重新归档意图，不删除源。", [{name: "code", label: "分享代码", max: 64}, {name: "password", label: "提取码", secret: true, max: 4}]));
    add("源删除结果对账", () => write("源删除结果对账", () => post("/archive/reconcile"), "", "确认源是否缺失并复查分享与STRM。不会重复删除。"));
    add("提交恢复缓存", () => write("恢复缓存", () => enqueue("restore", {media_id: id})));
    add("转存结果对账", () => write("转存结果对账", () => post("/restore/reconcile"), "", "只读核对已有目录和转存结果，不重复创建目录或转存。"));
    add("查看缓存目录候选", () => read("/restore/folder/candidates"));
    add("认领缓存目录…", async () => {
      const input = await confirmation("选择候选缓存目录", "请先查看候选并人工核查目录。下一步需要完整确认指定 CID。", "", [{name: "folder", label: "候选目录 CID", max: 30}]);
      if (!input) return;
      if (!/^[1-9][0-9]*$/.test(input.folder)) throw new Error("目录 CID 必须是非零数字。");
      await write("认领缓存目录", c => post("/restore/folder/attach", {media_id: id, folder_id: input.folder, confirmation: c.confirmation}), `ADOPT_FOLDER:${id}:${input.folder}`, "认领已核查的指定目录，不会重建目录。确认后仍受完整身份门禁保护。");
    });
    add("缓存删除结果对账", () => write("缓存删除结果对账", () => post("/cache/reconcile")));
    if (media.storage_type === "CACHE") add("删除恢复缓存…", () => write("删除恢复缓存", () => api(`/cache/${id}`, "DELETE"), `DELETE_CACHE:${id}`, "仅删除已验证归属的缓存文件；播放租约、分享损坏或未知状态将阻止删除。"), "danger");
    if (!$("detail").open) $("detail").showModal();
  }
  async function loadJobs() {
    const revision = ++revisions.jobs;
    const rows = await api(`/jobs?limit=${pageSize}&offset=${jobsOffset}`);
    if (revision !== revisions.jobs) return;
    table("jobs-table", ["任务", "操作", "状态", "尝试", "更新时间", "操作入口"], rows.map(job => {
      const actions = el("div", null, "actions");
      if (job.state === "PENDING") actions.append(button("取消待执行", () => write("取消待执行任务", () => api(`/jobs/${job.id}`, "POST", {action: "cancel"}))));
      if (["FAILED", "NEEDS_ATTENTION", "CANCELLED"].includes(job.state) && ["health", "generate"].includes(job.kind)) actions.append(button("重试", () => write("重试安全任务", c => api(`/jobs/${job.id}`, "POST", {action: "retry", confirmation: c.confirmation}), `RETRY:${job.id}`)));
      if (job.state === "NEEDS_ATTENTION") actions.append(el("span", "到媒体详情使用专属对账；禁止盲目重试。", "muted"));
      return [`#${job.id}`, job.kind, badge(job.state), job.attempts, date(job.updated_at), actions];
    }));
    $("jobs-prev").disabled = jobsOffset === 0; $("jobs-next").disabled = rows.length < pageSize; $("jobs-page").textContent = `第 ${jobsOffset / pageSize + 1} 页`;
  }
  async function loadGroups() {
    const revision = ++revisions.groups;
    const rows = await api(`/share/groups?limit=${pageSize}&offset=${groupsOffset}`);
    if (revision !== revisions.groups) return;
    table("groups-table", ["分组", "状态", "冻结成员", "更新时间", "操作"], rows.map(group => {
      const members = el("div", null, "actions"), actions = el("div", null, "actions");
      group.media_ids.forEach(id => members.append(button(`#${id}`, () => detail(id), "entry")));
      actions.append(button("结果对账", () => write("分组结果对账", () => api("/share/group/reconcile", "POST", {group_id: group.id}))));
      actions.append(button("整组重新分享", () => write("整组重新分享", () => api("/share/group/repair", "POST", {group_id: group.id}), "", "验证所有成员后原子切换，不删除源文件。未知创建结果不会自动重发。")));
      actions.append(button("接入创建结果…", () => attachGroup(group, false)));
      if (group.repair_pending) actions.append(button("接入重新分享结果…", () => attachGroup(group, true)));
      const ids = [...group.media_ids].sort((a, b) => a - b);
      actions.append(button("整组验证并删除源…", () => write("整组验证并删除源", c => enqueue("archive_group", {media_ids: ids, delete: true}, c), `DELETE_GROUP:${ids.join(",")}`, "完整验证冻结成员后逐项删除个人盘源文件至回收站。请确认备份！中途失败须专属对账，不盲目重试。"), "danger"));
      return [`#${group.id} · ${group.label}`, badge(group.state), members, date(group.updated_at), actions];
    }));
    $("groups-prev").disabled = groupsOffset === 0; $("groups-next").disabled = rows.length < pageSize; $("groups-page").textContent = `第 ${groupsOffset / pageSize + 1} 页`;
  }
  const attachGroup = (group, repair) => write("接入分组分享结果", c => api(repair ? "/share/group/repair/attach" : "/share/group/attach", "POST", {group_id: group.id, share_code: c.code, receive_code: c.password}), "", "需与冻结成员逐一匹配，不再创建新分享。", [{name: "code", label: "分享代码", max: 64}, {name: "password", label: "提取码", secret: true, max: 4}]);
  async function loadOverview() {
    const revision = ++revisions.overview;
    const data = await api("/dashboard"), stats = el("div", null, "stats");
    if (revision !== revisions.overview) return;
    const cells = [["STRM 记录", data.counts.strm_records], ["分享数量", data.counts.shares], ["异常媒体", data.counts.abnormal], ["恢复缓存", bytes(data.cache.bytes)], ["账号观察", data.account.state], ["今日日期", data.today.date]];
    cells.forEach(([label, value]) => {const item = el("div", null, "stat"); item.append(el("span", label), el("strong", value ?? "未知")); stats.append(item);});
    const storage = el("p", data.storage.map(s => `${names[s.storage_type]} ${s.count} 项 / ${bytes(s.bytes)}`).join("　"));
    const account = el("p", "账号卡片只读取已有观察，不会在页面渲染时请求115。过期/未知状态不代表已登录。", "muted");
    const refreshAccount = button("刷新账号观察", () => write("刷新账号观察", () => api("/account/status?refresh=true")));
    $("overview").replaceChildren(stats, storage, account, refreshAccount);
    table("logs-table", ["时间", "媒体", "操作", "状态", "说明"], data.tasks.map(task => [date(task.created_at), task.media_id ? button(`#${task.media_id}`, () => detail(task.media_id), "entry") : "—", task.operation, task.state, task.detail]));
  }
  async function refresh() {if (!key) return; await ({library: loadLibrary, jobs: loadJobs, groups: loadGroups, overview: loadOverview})[view]();}
  async function setView(next) {view = next; document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === view))); ["library", "jobs", "groups", "overview"].forEach(name => $(`${name}-view`).hidden = name !== view); $("view-title").textContent = {library: "媒体库", jobs: "任务与恢复", groups: "分享分组", overview: "运行概览"}[view]; await refresh();}
  $("login-form").addEventListener("submit", event => {event.preventDefault(); guarded(async () => {
    if (connecting) return;
    connecting = true;
    key = $("key").value; $("key").value = ""; epoch++;
    try {await api("/dashboard"); $("login").hidden = true; $("workspace").hidden = false; $("lock").hidden = false; notice(""); await setView("library");}
    catch (error) {lock(); throw error;}
    finally {connecting = false;}
  });});
  $("lock").addEventListener("click", () => {lock(); notice("页面已锁定。已提交的后台任务不会因此取消。");});
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => guarded(() => setView(b.dataset.view))));
  $("refresh").addEventListener("click", () => guarded(refresh));
  $("filters").addEventListener("submit", e => {e.preventDefault(); offset = 0; guarded(loadLibrary);});
  $("all-library").addEventListener("click", () => guarded(() => navigate("/")));
  $("parent").addEventListener("click", () => guarded(() => navigate(prefix.replace(/\/$/, "").replace(/\/[^/]*$/, "") + "/")));
  $("tree-mode").addEventListener("click", () => {tree = !tree; offset = 0; guarded(loadLibrary);});
  $("previous").addEventListener("click", () => {offset = Math.max(0, offset - pageSize); guarded(loadLibrary);});
  $("next").addEventListener("click", () => {offset += pageSize; guarded(loadLibrary);});
  $("jobs-prev").addEventListener("click", () => {jobsOffset = Math.max(0, jobsOffset - pageSize); guarded(loadJobs);});
  $("jobs-next").addEventListener("click", () => {jobsOffset += pageSize; guarded(loadJobs);});
  $("groups-prev").addEventListener("click", () => {groupsOffset = Math.max(0, groupsOffset - pageSize); guarded(loadGroups);});
  $("groups-next").addEventListener("click", () => {groupsOffset += pageSize; guarded(loadGroups);});
  $("scan").addEventListener("click", () => guarded(() => write("提交目录扫描", () => enqueue("scan", {}), "", "按配置扫描115目录；开启自动整理/归档时会遵守对应开关，但本次扫描不授权自动删除。")));
  $("health").addEventListener("click", () => guarded(() => write("提交健康检查", () => enqueue("health", {deep: false}))));
  $("group-form").addEventListener("submit", e => {e.preventDefault(); guarded(async () => {const ids = $("group-ids").value.split(",").map(x => Number(x.trim())); if (!ids.length || ids.length > 1000 || ids.some(id => !Number.isSafeInteger(id) || id < 1) || new Set(ids).size !== ids.length) throw new Error("请输入不重复的正整数媒体 ID。"); await write("创建分组分享", () => enqueue("archive_group", {media_ids: ids, delete: false}));});});
  $("detail-close").addEventListener("click", () => $("detail").close());
  $("confirm-cancel").addEventListener("click", () => finishConfirmation(null));
  $("confirm").addEventListener("cancel", e => {e.preventDefault(); finishConfirmation(null);});
  $("confirmation").addEventListener("input", () => $("confirmation").setCustomValidity(""));
  window.addEventListener("pagehide", lock);
})();
