const state = {
  storeId: "STORE_BLR_001",
  timer: null,
};

const el = (id) => document.getElementById(id);

function formatPercent(value) {
  return value === null || value === undefined ? "n/a" : `${Number(value).toFixed(1)}%`;
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-IN").format(value || 0);
}

function formatTime(value) {
  if (!value) return "-";
  return new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

function renderMetrics(metrics) {
  el("updated-at").textContent = formatTime(metrics.timestamp);
  el("quality-score").textContent = Number(metrics.data_quality_score || 0).toFixed(2);
  el("total-visitors").textContent = formatNumber(metrics.total_visitors);
  el("unique-visitors").textContent = formatNumber(metrics.unique_visitors);
  el("conversion-rate").textContent = formatPercent(metrics.conversion_rate);
  el("queue-depth").textContent = metrics.current_queue_depth ?? "n/a";
}

function renderFunnel(data) {
  const maxCount = Math.max(...data.funnel.map((stage) => stage.visitor_count), 1);
  el("converted-visitors").textContent = `${formatNumber(data.converted_visitors)} converted`;
  el("funnel").innerHTML = data.funnel
    .map((stage) => {
      const width = Math.max((stage.visitor_count / maxCount) * 100, stage.visitor_count > 0 ? 6 : 0);
      return `
        <div class="funnel-row">
          <strong>${stage.stage}</strong>
          <div class="bar" aria-label="${stage.stage} ${stage.visitor_count}">
            <span style="--w:${width}%"></span>
          </div>
          <span>${formatNumber(stage.visitor_count)}</span>
        </div>
        <div class="funnel-row">
          <span></span>
          <small>${stage.next_stage ? `${stage.drop_off_pct.toFixed(1)}% drop to ${stage.next_stage}` : "Final conversion stage"}</small>
          <span></span>
        </div>
      `;
    })
    .join("");
}

function renderHeatmap(data) {
  const target = el("heatmap");
  if (!data.zones.length) {
    target.innerHTML = '<p class="empty">No zone visits yet.</p>';
    return;
  }
  target.innerHTML = data.zones
    .map((zone) => {
      const heat = Math.max(10, Math.min(85, zone.intensity_0_100));
      return `
        <div class="zone" style="--heat:${heat}%">
          <strong>${zone.zone_name}</strong>
          <span>${formatNumber(zone.visit_frequency)} visits</span>
          <span>${Math.round(zone.avg_dwell_ms)} ms avg dwell</span>
          <span>${Math.round(zone.intensity_0_100)} intensity</span>
        </div>
      `;
    })
    .join("");
}

function renderAnomalies(data) {
  const target = el("anomalies");
  if (!data.anomalies.length) {
    target.innerHTML = '<p class="empty">No active anomalies.</p>';
    return;
  }
  target.innerHTML = data.anomalies
    .map((anomaly) => {
      const severity = anomaly.severity.toLowerCase();
      return `
        <div class="anomaly ${severity}">
          <strong>${anomaly.anomaly_type}</strong>
          <p>${anomaly.message}</p>
          <small>${anomaly.suggested_action}</small>
        </div>
      `;
    })
    .join("");
}

function renderHealth(health) {
  el("api-status").textContent = health.status;
  el("db-status").textContent = health.db_status;
}

async function refreshDashboard() {
  const storeId = el("store-id").value.trim() || state.storeId;
  state.storeId = storeId;

  try {
    const [health, metrics, funnel, heatmap, anomalies] = await Promise.all([
      getJson("/health"),
      getJson(`/stores/${encodeURIComponent(storeId)}/metrics`),
      getJson(`/stores/${encodeURIComponent(storeId)}/funnel`),
      getJson(`/stores/${encodeURIComponent(storeId)}/heatmap`),
      getJson(`/stores/${encodeURIComponent(storeId)}/anomalies`),
    ]);
    renderHealth(health);
    renderMetrics(metrics);
    renderFunnel(funnel);
    renderHeatmap(heatmap);
    renderAnomalies(anomalies);
  } catch (error) {
    el("api-status").textContent = "unavailable";
    el("db-status").textContent = "-";
    el("ingest-result").textContent = `Refresh failed: ${error.message}`;
  }
}

async function seedStorePicker() {
  try {
    const data = await getJson("/stores");
    if (data.stores.length && el("store-id").value === "STORE_BLR_001") {
      el("store-id").value = data.stores[0].store_id;
    }
  } catch {
    /* Store discovery is optional for first run. */
  }
}

function makeEventPayload() {
  const eventType = el("event-type").value;
  const zoneId = el("zone-id").value || null;
  return {
    event_id: crypto.randomUUID(),
    store_id: state.storeId,
    camera_id: zoneId === "BILLING" ? "CAM_BILLING_01" : "CAM_1",
    visitor_id: el("visitor-id").value.trim() || `VIS_${crypto.randomUUID().slice(0, 8)}`,
    event_type: eventType,
    timestamp: new Date().toISOString(),
    zone_id: zoneId,
    dwell_ms: eventType === "ZONE_DWELL" ? 4500 : 0,
    is_staff: el("is-staff").checked,
    confidence: 0.94,
    metadata: eventType.startsWith("BILLING_QUEUE")
      ? { queue_depth: Math.floor(1 + Math.random() * 6) }
      : {},
  };
}

async function ingestEvent(event) {
  event.preventDefault();
  state.storeId = el("store-id").value.trim() || state.storeId;
  const payload = makeEventPayload();
  try {
    const result = await postJson("/events/ingest", { events: [payload] });
    el("ingest-result").textContent = `Event stored: ${result.successful}, duplicates: ${result.duplicates}`;
    await refreshDashboard();
  } catch (error) {
    el("ingest-result").textContent = `Event ingest failed: ${error.message}`;
  }
}

async function ingestPos() {
  state.storeId = el("store-id").value.trim() || state.storeId;
  try {
    const result = await postJson("/pos/ingest", {
      transactions: [{
        store_id: state.storeId,
        transaction_id: `TXN_${crypto.randomUUID().slice(0, 8)}`,
        timestamp: new Date().toISOString(),
        basket_value_inr: Math.round(450 + Math.random() * 3500),
      }],
    });
    el("ingest-result").textContent = `POS stored: ${result.successful}, duplicates: ${result.duplicates}`;
    await refreshDashboard();
  } catch (error) {
    el("ingest-result").textContent = `POS ingest failed: ${error.message}`;
  }
}

function configureAutoRefresh() {
  clearInterval(state.timer);
  if (el("auto-refresh").checked) {
    state.timer = setInterval(refreshDashboard, 5000);
  }
}

el("store-form").addEventListener("submit", (event) => {
  event.preventDefault();
  refreshDashboard();
});
el("event-form").addEventListener("submit", ingestEvent);
el("pos-button").addEventListener("click", ingestPos);
el("auto-refresh").addEventListener("change", configureAutoRefresh);

seedStorePicker().then(refreshDashboard).then(configureAutoRefresh);
