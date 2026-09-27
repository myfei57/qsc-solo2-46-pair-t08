const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function renderCards(entries) {
  const cards = $("overview-cards");
  cards.innerHTML = "";
  for (const [label, value, tone] of entries) {
    const card = document.createElement("div");
    card.className = "card" + (tone ? " " + tone : "");
    const labelNode = document.createElement("div");
    labelNode.className = "label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "value";
    valueNode.textContent = String(value);
    card.appendChild(labelNode);
    card.appendChild(valueNode);
    cards.appendChild(card);
  }
}

function setOnline(online) {
  const node = $("conn-status");
  node.textContent = online ? "已连接" : "连接中断";
  node.classList.toggle("offline", !online);
}

async function refresh() {
  let state;
  try {
    state = await api("/api/state");
  } catch (error) {
    setOnline(false);
    return;
  }
  setOnline(true);
  const data = state.data || {};
  const zone = data.zone || {};
  const slew = data.slew || {};
  const hoist = data.hoist || {};
  const wind = data.wind || {};
  const limit = data.limit || {};
  const cluster = data.cluster || {};
  const pair = cluster.nearest_pair || {};
  renderCards([
    ["塔吊数量", Object.keys(cluster.positions || {}).length],
    ["分区占用", (zone.occupants || []).length, zone.conflict ? "danger" : ""],
    ["冲突闩锁", zone.latch ? "锁定" : "释放", zone.latch ? "danger" : ""],
    ["回转状态", slew.moving ? "运转" : "停止", wind.active ? "warn" : ""],
    ["落钩状态", hoist.hook_lowered ? "已落钩" : "未落钩", hoist.hook_lowered ? "" : "warn"],
    ["制动状态", hoist.brake_engaged ? "抱闸" : "松闸"],
    ["风速", (wind.level || 0).toFixed(1) + " m/s", wind.active ? "danger" : ""],
    ["限位闩锁", limit.latch ? "锁定" : "释放", limit.latch ? "danger" : ""],
    ["最近间距", pair.distance ? pair.distance.toFixed(1) + " 米" : "—", pair.below_safe ? "danger" : ""],
    ["审计记录", data.audit || 0],
  ]);
  $("crane-data").textContent = JSON.stringify(cluster, null, 2);
  $("zone-data").textContent = JSON.stringify(zone, null, 2);
  $("hoist-data").textContent = JSON.stringify(hoist, null, 2);
  $("alarm-data").textContent = JSON.stringify({ wind, limit }, null, 2);
  $("config-data").textContent = JSON.stringify(data.config || {}, null, 2);
  const audit = await api("/api/audit?limit=15");
  $("audit-data").textContent = JSON.stringify(audit.data || [], null, 2);
  $("overview-log").textContent = JSON.stringify((audit.data || []).slice(0, 5), null, 2);
}

document.querySelectorAll("#nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    document.querySelectorAll("main section").forEach((section) => section.classList.add("hidden"));
    $("page-" + button.dataset.page).classList.remove("hidden");
  });
});

$("survey-btn").addEventListener("click", async () => {
  await post("/api/cluster/survey", {
    cranes: { "crane-1": { x: 0, y: 0 }, "crane-2": { x: 82, y: 1 } },
  });
  refresh();
});

$("reset-crane-btn").addEventListener("click", async () => {
  await post("/api/crane/reset");
  refresh();
});

$("occupy-a-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-1" });
  refresh();
});

$("occupy-b-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-2" });
  refresh();
});

$("clear-conflict-btn").addEventListener("click", async () => {
  await post("/api/zone/clear");
  refresh();
});

$("slew-start-btn").addEventListener("click", async () => {
  await post("/api/slew/start");
  refresh();
});

$("lower-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "lower", axis: "hoist" });
  refresh();
});

$("feedback-btn").addEventListener("click", async () => {
  await post("/api/hoist/feedback", { speed: 1.2 });
  refresh();
});

$("release-btn").addEventListener("click", async () => {
  await post("/api/hoist/release");
  refresh();
});

$("trolley-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "trolley", axis: "trolley" });
  refresh();
});

$("hoist-stop-btn").addEventListener("click", async () => {
  await post("/api/hoist/stop");
  refresh();
});

$("wind-alarm-btn").addEventListener("click", async () => {
  await post("/api/wind/alarm", { level: 14 });
  refresh();
});

$("wind-clear-btn").addEventListener("click", async () => {
  await post("/api/wind/clear");
  refresh();
});

$("limit-check-btn").addEventListener("click", async () => {
  await post("/api/limit/check");
  refresh();
});

$("slew-fault-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: true });
  refresh();
});

$("slew-recover-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: false });
  refresh();
});

$("config-save-btn").addEventListener("click", async () => {
  await post("/api/config", { site_name: "华东三号工地", safe_separation: 15 });
  refresh();
});

// ------------------------------------------------------------- equipment archive
async function refreshArchives() {
  const result = await api("/api/archives/crane?crane_id=crane-1");
  if (result.ok) {
    $("archive-current").textContent = JSON.stringify(result.data.current, null, 2);
    $("archive-history").textContent = JSON.stringify(result.data.history, null, 2);
  } else {
    $("archive-current").textContent = "尚未登记档案";
    $("archive-history").textContent = "[]";
  }
}

function archiveBody() {
  const number = (value) => (value === "" ? undefined : Number(value));
  return {
    crane_id: "crane-1",
    model: $("archive-model").value.trim(),
    serial_number: $("archive-serial").value.trim(),
    manufacturer: $("archive-manufacturer").value.trim(),
    effective_from: $("archive-effective").value.trim(),
    parameters: {
      rated_capacity_t: number($("archive-capacity").value),
      max_radius_m: number($("archive-radius").value),
      max_wind_speed_mps: number($("archive-wind").value),
    },
  };
}

// Drop blank fields so amend requests carry only what actually changes.
function compactArchiveBody() {
  const body = archiveBody();
  if (!body.model) delete body.model;
  if (!body.serial_number) delete body.serial_number;
  if (!body.manufacturer) delete body.manufacturer;
  if (!body.effective_from) delete body.effective_from;
  for (const key of Object.keys(body.parameters)) {
    if (body.parameters[key] === undefined) delete body.parameters[key];
  }
  if (Object.keys(body.parameters).length === 0) delete body.parameters;
  return body;
}

async function submitArchive(path) {
  const body = path === "/api/archives/amend" ? compactArchiveBody() : archiveBody();
  const result = await post(path, body);
  $("archive-effective").textContent = result.ok
    ? "已写入 rev " + result.data.seq
    : "操作失败：" + (result.error || "bad request");
  if (result.ok) await refreshArchives();
}

$("archive-register-btn").addEventListener("click", () => submitArchive("/api/archives/register"));
$("archive-amend-btn").addEventListener("click", () => submitArchive("/api/archives/amend"));
$("archive-replace-btn").addEventListener("click", () => submitArchive("/api/archives/replace"));

$("archive-effective-btn").addEventListener("click", async () => {
  const at = encodeURIComponent($("archive-at").value.trim());
  const result = await api("/api/archives/effective?crane_id=crane-1&at=" + at);
  $("archive-effective").textContent = JSON.stringify(
    result.ok ? result.data : { error: result.error },
    null,
    2
  );
});

refreshArchives();

setInterval(refresh, 3000);
refresh();
