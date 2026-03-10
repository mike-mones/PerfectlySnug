// Overnight Temperature Tracker — Read-only dashboard
// Polls HA API for sensor history and live state, renders charts.

const HA_URL = "http://192.168.0.106:8123";
const POLL_INTERVAL = 30_000; // 30s live refresh

// Stage target temps (from controller v2 correlation analysis)
const STAGE_TARGETS = {
    deep: 82.0, core: 83.0, rem: 83.5,
    awake: 82.0, in_bed: 82.0, unknown: 83.0,
};
const STAGE_COLORS = {
    deep: "#58a6ff", core: "#3fb950", rem: "#bc8cff",
    awake: "#f0883e", in_bed: "#8b949e", unknown: "#484f58",
};

let zone = "left";
let token = "";
let pollTimer = null;
let currentView = "live"; // "live" or "model"

// Charts
let tempChart, settingChart, hrChart;
let modelTargetChart, modelNightChart;

// ─── HA API helpers ────────────────────────────────────
async function haGet(path) {
    if (!token) throw new Error("No token configured");
    const res = await fetch(`${HA_URL}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) {
        token = "";
        localStorage.removeItem("ha_token");
        document.getElementById("connectBtn").style.display = "";
        showTokenPrompt();
        throw new Error("Token expired or invalid — please re-enter");
    }
    if (!res.ok) throw new Error(`HA ${res.status}`);
    return res.json();
}

async function haState(entityId) {
    return haGet(`/api/states/${entityId}`);
}

async function haHistory(entityId, start, end) {
    const params = new URLSearchParams({
        filter_entity_id: entityId,
        end_time: end.toISOString(),
        minimal_response: "",
        no_attributes: "",
    });
    const data = await haGet(`/api/history/period/${start.toISOString()}?${params}`);
    return data[0] || [];
}

// ─── Entity IDs ────────────────────────────────────────
function bodyEntities() {
    return [
        `sensor.smart_topper_${zone}_side_body_sensor_left`,
        `sensor.smart_topper_${zone}_side_body_sensor_center`,
        `sensor.smart_topper_${zone}_side_body_sensor_right`,
    ];
}
function settingEntity() {
    return `sensor.smart_topper_${zone}_side_temperature_setpoint`;
}
function presetEntities() {
    return [
        `number.smart_topper_${zone}_side_bedtime_temperature`,
        `number.smart_topper_${zone}_side_sleep_temperature`,
        `number.smart_topper_${zone}_side_wake_temperature`,
    ];
}
function startLengthEntity() {
    return `number.smart_topper_${zone}_side_start_length_minutes`;
}
function wakeLengthEntity() {
    return `number.smart_topper_${zone}_side_wake_length_minutes`;
}
function progressEntity() {
    return `sensor.smart_topper_${zone}_side_run_progress`;
}

// ─── Day-based navigation ──────────────────────────────
// dayOffset=0 means "tonight" (8 PM today → now, or if before 8 PM, last night)
let dayOffset = 0;

function getRange() {
    const now = new Date();
    // Base "tonight" anchor: 8 PM today, or yesterday if before 8 PM
    const anchor = new Date(now);
    anchor.setHours(20, 0, 0, 0);
    if (now < anchor) anchor.setDate(anchor.getDate() - 1);

    const start = new Date(anchor);
    start.setDate(start.getDate() + dayOffset);
    const end = new Date(start);
    end.setDate(end.getDate() + 1); // 8 PM → 8 PM next day

    // Clamp end to now if this is the current night
    if (end > now) end.setTime(now.getTime());

    return { start, end };
}

function shiftDay(delta) {
    dayOffset += delta;
    // Don't go into the future
    if (dayOffset > 0) { dayOffset = 0; }
    updateDayLabel();
    fetchHistory();
}

function updateDayLabel() {
    const { start } = getRange();
    const label = document.getElementById("dayLabel");
    const nextBtn = document.getElementById("nextDay");
    if (dayOffset === 0) {
        label.textContent = "Tonight";
    } else {
        label.textContent = start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    }
    nextBtn.style.opacity = dayOffset >= 0 ? "0.3" : "1";
    nextBtn.style.pointerEvents = dayOffset >= 0 ? "none" : "auto";
}

// ─── Data fetching ─────────────────────────────────────
async function fetchAll() {
    const { start, end } = getRange();

    // Fetch history for all series in parallel
    const [bodyL, bodyC, bodyR, setpointHist, bedtimeHist, sleepHist, wakeHist, stageHist, hrHist, hrvHist, progressHist, startLenState, wakeLenState] =
        await Promise.all([
            haHistory(bodyEntities()[0], start, end),
            haHistory(bodyEntities()[1], start, end),
            haHistory(bodyEntities()[2], start, end),
            haHistory(settingEntity(), start, end),
            haHistory(presetEntities()[0], start, end),
            haHistory(presetEntities()[1], start, end),
            haHistory(presetEntities()[2], start, end),
            haHistory("input_text.apple_health_sleep_stage", start, end),
            haHistory("input_number.apple_health_hr_avg", start, end),
            haHistory("input_number.apple_health_hrv", start, end),
            haHistory(progressEntity(), start, end),
            haState(startLengthEntity()),
            haState(wakeLengthEntity()),
        ]);

    // Find active sleep window from run_progress (> 0)
    let runStart = null, runEnd = null, prevP = 0;
    for (const r of progressHist) {
        const p = parseFloat(r.state);
        if (isNaN(p)) continue;
        const t = new Date(r.last_changed);
        if (p > 0 && prevP === 0) runStart = t;
        if (p === 0 && prevP > 0) runEnd = t;
        prevP = p;
    }
    // If run is still active (no 0 at end), use last data point
    if (runStart && !runEnd && prevP > 0) {
        runEnd = new Date(progressHist[progressHist.length - 1].last_changed);
    }

    // Clip display window to the active run (with 10 min padding)
    const pad = 10 * 60000;
    const displayStart = runStart ? new Date(runStart.getTime() - pad) : start;
    const displayEnd = runEnd ? new Date(runEnd.getTime() + pad) : end;

    // Build body temp average series (smoothed with 5-min rolling average)
    const bodyRaw = mergeBodySensors(bodyL, bodyC, bodyR);
    const bodyAvg = rollingAverage(bodyRaw, 5 * 60000);

    // Build target temp series from stage history
    const targetSeries = stageHist.map((s) => ({
        x: new Date(s.last_changed),
        y: STAGE_TARGETS[s.state] || STAGE_TARGETS.unknown,
    }));

    // Setpoint series (actual active temperature setting in °F)
    const setpointSeries = setpointHist
        .filter(s => s.state !== 'unavailable' && s.state !== 'unknown')
        .map(s => ({ x: new Date(s.last_changed), y: parseFloat(s.state) }));

    // Preset offset series (-10 to +10) — these are the actual topper controls
    const parseSeries = (hist) => hist
        .filter(s => s.state !== 'unavailable' && s.state !== 'unknown')
        .map(s => ({ x: new Date(s.last_changed), y: parseFloat(s.state) }));
    const bedtimeSeries = parseSeries(bedtimeHist);
    const sleepSeries = parseSeries(sleepHist);
    const wakeSeries = parseSeries(wakeHist);

    // Stitch into one continuous schedule line
    const startLenMin = parseFloat(startLenState.state) || 60;
    const wakeLenMin = parseFloat(wakeLenState.state) || 30;
    const scheduleSeries = stitchSchedule(
        bedtimeSeries, sleepSeries, wakeSeries,
        runStart, runEnd, startLenMin, wakeLenMin
    );

    // HR + HRV series
    const hrSeries = hrHist
        .filter((s) => s.state !== "unknown" && s.state !== "unavailable")
        .map((s) => ({ x: new Date(s.last_changed), y: parseFloat(s.state) }));
    const hrvSeries = hrvHist
        .filter((s) => s.state !== "unknown" && s.state !== "unavailable")
        .map((s) => ({ x: new Date(s.last_changed), y: parseFloat(s.state) }));

    // Sleep stage background bands
    const stageBands = buildStageBands(stageHist, start, end);

    // Active segments (where run_progress > 0) for dimming inactive
    const activeBands = buildActiveBands(progressHist, start, end);

    return { bodyAvg, targetSeries, setpointSeries, scheduleSeries, hrSeries, hrvSeries, stageBands, activeBands, stageHist, start: displayStart, end: displayEnd };
}

// Build one continuous offset line from the three phase presets.
// Phase boundaries: bedtime = runStart → runStart + startLen,
//                   sleep   = runStart + startLen → runEnd - wakeLen,
//                   wake    = runEnd - wakeLen → runEnd.
// Within each phase, include any mid-phase manual changes from that entity's history.
function stitchSchedule(bedtimeSeries, sleepSeries, wakeSeries, runStart, runEnd, startLenMin, wakeLenMin) {
    if (!runStart) return [];
    const effectiveEnd = runEnd || new Date();
    const bedEnd = new Date(runStart.getTime() + startLenMin * 60000);
    const wakeStart = new Date(effectiveEnd.getTime() - wakeLenMin * 60000);
    // If wake starts before bedtime ends, just split evenly
    const sleepStart = bedEnd < wakeStart ? bedEnd : new Date((runStart.getTime() + effectiveEnd.getTime()) / 2);
    const sleepEnd = bedEnd < wakeStart ? wakeStart : sleepStart;

    const result = [];

    // Helper: get the value of a series at a given time (last known value at or before t)
    const valueAt = (series, t) => {
        let val = null;
        for (const pt of series) {
            if (pt.x <= t) val = pt.y;
        }
        return val ?? (series.length > 0 ? series[0].y : null);
    };

    // Helper: add points from a series within [from, to), plus initial value
    const addPhase = (series, from, to) => {
        const startVal = valueAt(series, from);
        if (startVal !== null) result.push({ x: from, y: startVal });
        // Include any mid-phase changes
        for (const pt of series) {
            if (pt.x > from && pt.x < to) {
                result.push({ x: pt.x, y: pt.y });
            }
        }
    };

    addPhase(bedtimeSeries, runStart, sleepStart);
    addPhase(sleepSeries, sleepStart, sleepEnd);
    addPhase(wakeSeries, sleepEnd, effectiveEnd);
    // End point
    const lastVal = result.length > 0 ? result[result.length - 1].y : null;
    if (lastVal !== null) result.push({ x: effectiveEnd, y: lastVal });

    return result;
}

function mergeBodySensors(l, c, r) {
    // Combine all timestamps, average concurrent readings
    const allPoints = new Map();
    for (const series of [l, c, r]) {
        for (const pt of series) {
            if (pt.state === "unknown" || pt.state === "unavailable") continue;
            const t = new Date(pt.last_changed).getTime();
            if (!allPoints.has(t)) allPoints.set(t, []);
            allPoints.get(t).push(parseFloat(pt.state));
        }
    }
    return [...allPoints.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([t, vals]) => ({
            x: new Date(t),
            y: vals.reduce((s, v) => s + v, 0) / vals.length,
        }));
}

function rollingAverage(series, windowMs) {
    if (series.length < 2) return series;
    return series.map((pt, i) => {
        const tMin = pt.x.getTime() - windowMs / 2;
        const tMax = pt.x.getTime() + windowMs / 2;
        let sum = 0, count = 0;
        // scan backward
        for (let j = i; j >= 0 && series[j].x.getTime() >= tMin; j--) {
            sum += series[j].y; count++;
        }
        // scan forward
        for (let j = i + 1; j < series.length && series[j].x.getTime() <= tMax; j++) {
            sum += series[j].y; count++;
        }
        return { x: pt.x, y: sum / count };
    });
}

function buildStageBands(stageHist, start, end) {
    const bands = [];
    for (let i = 0; i < stageHist.length; i++) {
        const s = stageHist[i];
        const from = new Date(s.last_changed);
        const to = i + 1 < stageHist.length ? new Date(stageHist[i + 1].last_changed) : end;
        const color = STAGE_COLORS[s.state] || STAGE_COLORS.unknown;
        bands.push({ xMin: from, xMax: to, color: color + "18", label: s.state });
    }
    return bands;
}

function buildActiveBands(progressHist, start, end) {
    const bands = [];
    for (let i = 0; i < progressHist.length; i++) {
        const p = progressHist[i];
        const active = parseFloat(p.state) > 0;
        if (!active) continue;
        const from = new Date(p.last_changed);
        const to = i + 1 < progressHist.length ? new Date(progressHist[i + 1].last_changed) : end;
        bands.push({ xMin: from, xMax: to });
    }
    return bands;
}

// ─── Live stats update ─────────────────────────────────
async function updateLiveStats() {
    try {
        const [stageState, bodyStates, settState, hrState, hrvState] = await Promise.all([
            haState("input_text.apple_health_sleep_stage"),
            Promise.all(bodyEntities().map((e) => haState(e))),
            haState(settingEntity()),
            haState("input_number.apple_health_hr_avg"),
            haState("input_number.apple_health_hrv"),
        ]);

        const stage = stageState.state || "unknown";
        const stageEl = document.getElementById("currentStage");
        stageEl.innerHTML = `<span class="stage-badge stage-${stage}">${stage.replace("_", " ")}</span>`;

        const bodyTemps = bodyStates
            .map((s) => parseFloat(s.state))
            .filter((v) => !isNaN(v));
        const bodyAvg = bodyTemps.length ? bodyTemps.reduce((a, b) => a + b) / bodyTemps.length : null;
        document.getElementById("currentBodyTemp").textContent = bodyAvg ? `${bodyAvg.toFixed(1)}°F` : "--°F";

        const target = STAGE_TARGETS[stage] || STAGE_TARGETS.unknown;
        document.getElementById("currentTarget").textContent = `${target.toFixed(1)}°F`;

        const setting = parseFloat(settState.state);
        document.getElementById("currentSetting").textContent = isNaN(setting)
            ? "--"
            : `${setting.toFixed(1)}°F`;

        const hr = parseFloat(hrState.state);
        document.getElementById("currentHR").textContent = isNaN(hr) ? "-- bpm" : `${Math.round(hr)} bpm`;

        // Controller & SleepSync status
        const progress = await haState(progressEntity());
        const isRunning = parseFloat(progress.state) > 0;

        const stageAge = stageState.last_changed
            ? Math.round((Date.now() - new Date(stageState.last_changed).getTime()) / 60000)
            : null;
        const hrAge = hrState.last_changed
            ? Math.round((Date.now() - new Date(hrState.last_changed).getTime()) / 60000)
            : null;

        const ctrlEl = document.getElementById("controllerStatus");
        if (isRunning) {
            ctrlEl.innerHTML = `<span style="color:#3fb950">Active</span> · ${stage}`;
        } else {
            ctrlEl.innerHTML = `<span style="color:#484f58">Idle</span>`;
        }

        const syncEl = document.getElementById("sleepSyncStatus");
        if (stageAge !== null && stageAge < 10) {
            syncEl.innerHTML = `<span style="color:#3fb950">Live</span> · ${stageAge}m ago`;
        } else if (hrAge !== null && hrAge < 10) {
            syncEl.innerHTML = `<span style="color:#f0883e">HR only</span> · ${hrAge}m ago`;
        } else {
            syncEl.innerHTML = `<span style="color:#484f58">No data</span>`;
        }

        document.getElementById("statusDot").className = "dot live";
        document.getElementById("statusText").textContent = `Live · ${new Date().toLocaleTimeString()}`;
    } catch (err) {
        document.getElementById("statusDot").className = "dot";
        document.getElementById("statusText").textContent = `Error: ${err.message}`;
    }
}

// ─── Chart rendering ───────────────────────────────────
const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    plugins: {
        legend: { labels: { color: "#8b949e", usePointStyle: true, pointStyle: "line", padding: 16 } },
        tooltip: { mode: "index", intersect: false },
    },
    scales: {
        x: {
            type: "time",
            time: { tooltipFormat: "h:mm a", displayFormats: { minute: "h:mm a", hour: "h a" } },
            grid: { color: "#21262d" },
            ticks: { color: "#484f58", maxRotation: 0 },
        },
    },
};

function makeYAxis(id, label, color, position = "left") {
    return {
        id,
        position,
        title: { display: true, text: label, color },
        grid: { color: position === "left" ? "#21262d" : "transparent" },
        ticks: { color },
    };
}

function stageBandPlugin(bands) {
    return {
        id: "stageBands",
        beforeDraw(chart) {
            const { ctx, chartArea: { top, bottom }, scales: { x } } = chart;
            for (const b of bands) {
                const left = x.getPixelForValue(b.xMin);
                const right = x.getPixelForValue(b.xMax);
                ctx.fillStyle = b.color;
                ctx.fillRect(left, top, right - left, bottom - top);
            }
        },
    };
}

function initCharts() {
    tempChart = new Chart(document.getElementById("tempChart"), {
        type: "line",
        data: { datasets: [] },
        options: {
            ...chartDefaults,
            scales: {
                ...chartDefaults.scales,
                y: makeYAxis("y", "°F", "#e6edf3"),
            },
        },
    });

    settingChart = new Chart(document.getElementById("settingChart"), {
        type: "line",
        data: { datasets: [] },
        options: {
            ...chartDefaults,
            scales: {
                ...chartDefaults.scales,
                y: {
                    ...makeYAxis("y", "Setting", "#3fb950"),
                    min: -10, max: 10,
                },
            },
        },
    });

    hrChart = new Chart(document.getElementById("hrChart"), {
        type: "line",
        data: { datasets: [] },
        options: {
            ...chartDefaults,
            scales: {
                ...chartDefaults.scales,
                y: makeYAxis("y", "HR (bpm)", "#f47067"),
                y2: makeYAxis("y2", "HRV (ms)", "#bc8cff", "right"),
            },
        },
    });
}

function updateCharts(data) {
    const { bodyAvg, targetSeries, setpointSeries, scheduleSeries, hrSeries, hrvSeries, stageBands, start, end } = data;

    // Sync all chart x-axes to the same time range
    const xMin = start;
    const xMax = end;

    // Temp chart — only show Target line when there's meaningful stage data
    const tempDatasets = [
        {
            label: "Body Temp",
            data: bodyAvg,
            borderColor: "#f47067",
            backgroundColor: "#f4706730",
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.3,
            fill: false,
        },
    ];
    if (targetSeries.length >= 5) {
        tempDatasets.push({
            label: "Target",
            data: targetSeries,
            borderColor: "#58a6ff",
            borderWidth: 2,
            borderDash: [6, 3],
            pointRadius: 0,
            tension: 0,
            stepped: "before",
            fill: false,
        });
    }
    tempChart.data.datasets = tempDatasets;
    tempChart.options.scales.x.min = xMin;
    tempChart.options.scales.x.max = xMax;
    tempChart.options.plugins.stageBands = stageBands;
    if (!tempChart.config.plugins?.find((p) => p.id === "stageBands")) {
        tempChart.config.plugins = [stageBandPlugin(stageBands)];
    } else {
        tempChart.config.plugins[0] = stageBandPlugin(stageBands);
    }
    tempChart.update();

    // Setting chart — one continuous schedule line (-10 to +10)
    const settingDatasets = [];
    if (scheduleSeries.length > 0) {
        settingDatasets.push({
            label: "Temperature Setting",
            data: scheduleSeries,
            borderColor: "#3fb950",
            backgroundColor: "#3fb95020",
            borderWidth: 2.5,
            pointRadius: 0,
            stepped: "before",
            fill: true,
            yAxisID: "y",
        });
    }
    settingChart.data.datasets = settingDatasets;
    settingChart.options.scales.x.min = xMin;
    settingChart.options.scales.x.max = xMax;
    if (!settingChart.config.plugins?.find((p) => p.id === "stageBands")) {
        settingChart.config.plugins = [stageBandPlugin(stageBands)];
    } else {
        settingChart.config.plugins[0] = stageBandPlugin(stageBands);
    }
    settingChart.update();

    // HR chart
    hrChart.data.datasets = [
        {
            label: "Heart Rate",
            data: hrSeries,
            borderColor: "#f47067",
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.3,
            yAxisID: "y",
            fill: false,
        },
        {
            label: "HRV",
            data: hrvSeries,
            borderColor: "#bc8cff",
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.3,
            yAxisID: "y2",
            fill: false,
        },
    ];
    hrChart.options.scales.x.min = xMin;
    hrChart.options.scales.x.max = xMax;
    if (!hrChart.config.plugins?.find((p) => p.id === "stageBands")) {
        hrChart.config.plugins = [stageBandPlugin(stageBands)];
    } else {
        hrChart.config.plugins[0] = stageBandPlugin(stageBands);
    }
    hrChart.update();
}

// ─── Model view ────────────────────────────────────────
function setView(v) {
    currentView = v;
    document.getElementById("viewLive").classList.toggle("active", v === "live");
    document.getElementById("viewModel").classList.toggle("active", v === "model");

    // Toggle sections
    const modelEl = document.getElementById("modelView");
    if (v === "model") {
        document.querySelector(".stats-row").style.display = "none";
        document.querySelectorAll("main > .chart-container").forEach(el => el.style.display = "none");
        modelEl.style.display = "block";
        renderModelCharts().catch(err => console.error("Model render error:", err));
    } else {
        document.querySelector(".stats-row").style.display = "";
        document.querySelectorAll("main > .chart-container").forEach(el => el.style.display = "");
        modelEl.style.display = "none";
        if (token) fetchHistory();
    }
}

async function renderModelCharts() {
    // Bar chart: target temp per stage (always shown)
    const stages = ["in_bed", "deep", "core", "rem", "awake"];
    const labels = stages.map(s => s.replace("_", " "));
    const temps = stages.map(s => STAGE_TARGETS[s]);
    const colors = stages.map(s => STAGE_COLORS[s]);

    if (modelTargetChart) modelTargetChart.destroy();
    modelTargetChart = new Chart(document.getElementById("modelTargetChart"), {
        type: "bar",
        data: {
            labels,
            datasets: [{
                label: "Target Body Temp (°F)",
                data: temps,
                backgroundColor: colors.map(c => c + "60"),
                borderColor: colors,
                borderWidth: 2,
                borderRadius: 6,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: "y",
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => `${ctx.parsed.x.toFixed(1)}°F`,
                    },
                },
            },
            scales: {
                x: {
                    min: 72, max: 90,
                    title: { display: true, text: "Body Sensor Target (°F)", color: "#8b949e" },
                    grid: { color: "#21262d" },
                    ticks: { color: "#8b949e" },
                },
                y: {
                    grid: { display: false },
                    ticks: { color: "#e6edf3", font: { size: 14 } },
                },
            },
        },
    });

    // Last Night chart: real data from HA or placeholder
    if (token) {
        await renderLastNightChart();
    } else {
        renderPlaceholderNightChart();
    }
}

function renderPlaceholderNightChart() {
    document.getElementById("lastNightDesc").textContent = "Connect to Home Assistant to see real overnight body temperature data.";
    document.getElementById("lastNightStats").style.display = "none";
    if (modelNightChart) modelNightChart.destroy();
    modelNightChart = new Chart(document.getElementById("modelNightChart"), {
        type: "line",
        data: { datasets: [] },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                title: {
                    display: true,
                    text: "No data — connect to HA to view last night's temperatures",
                    color: "#484f58",
                    font: { size: 16 },
                },
            },
        },
    });
}

async function renderLastNightChart() {
    try {
        // Find the most recent overnight run by looking at run_progress
        // Search last 48 hours for a completed run
        const now = new Date();
        const lookback = new Date(now.getTime() - 48 * 3600e3);
        const progressHist = await haHistory(progressEntity(), lookback, now);

        // Find completed runs (progress goes 0 → increasing → 0)
        const runs = [];
        let runStart = null;
        let prevP = 0;
        for (const r of progressHist) {
            const p = parseFloat(r.state);
            if (isNaN(p)) continue;
            if (p > 0 && prevP === 0) runStart = new Date(r.last_changed);
            if (p === 0 && prevP > 0 && runStart) {
                runs.push({ start: runStart, end: new Date(r.last_changed) });
                runStart = null;
            }
            prevP = p;
        }
        // If currently running, use now as end
        if (runStart) runs.push({ start: runStart, end: now });

        if (runs.length === 0) {
            document.getElementById("lastNightDesc").textContent = "No overnight run found in the last 48 hours.";
            renderPlaceholderNightChart();
            return;
        }

        // Use the longest recent run (likely the real overnight, not a short test)
        const bestRun = runs.reduce((best, r) =>
            (r.end - r.start) > (best.end - best.start) ? r : best
        );
        const padStart = new Date(bestRun.start.getTime() - 15 * 60000);
        const padEnd = new Date(bestRun.end.getTime() + 15 * 60000);

        // Fetch body sensor + setting + presets + schedule lengths + sleep stage data
        const [bodyL, bodyC, bodyR, settHist, bedtimeH, sleepH, wakeH, startLenS, wakeLenS, stageHist] = await Promise.all([
            haHistory(`sensor.smart_topper_${zone}_side_body_sensor_left`, padStart, padEnd),
            haHistory(`sensor.smart_topper_${zone}_side_body_sensor_center`, padStart, padEnd),
            haHistory(`sensor.smart_topper_${zone}_side_body_sensor_right`, padStart, padEnd),
            haHistory(settingEntity(), padStart, padEnd),
            haHistory(presetEntities()[0], padStart, padEnd),
            haHistory(presetEntities()[1], padStart, padEnd),
            haHistory(presetEntities()[2], padStart, padEnd),
            haState(startLengthEntity()),
            haState(wakeLengthEntity()),
            haHistory("input_text.apple_health_sleep_stage", padStart, padEnd),
        ]);

        const bodyRaw = mergeBodySensors(bodyL, bodyC, bodyR);
        const bodyAvg = rollingAverage(bodyRaw, 5 * 60000);
        const parseSeries = (hist) => hist
            .filter(s => s.state !== 'unavailable' && s.state !== 'unknown')
            .map(s => ({ x: new Date(s.last_changed), y: parseFloat(s.state) }))
            .filter(p => !isNaN(p.y));
        const scheduleSeries = stitchSchedule(
            parseSeries(bedtimeH), parseSeries(sleepH), parseSeries(wakeH),
            bestRun.start, bestRun.end,
            parseFloat(startLenS.state) || 60, parseFloat(wakeLenS.state) || 30
        );

        // Build sleep stage bands if available
        const stageBands = buildStageBands(stageHist, padStart, padEnd);

        // Build target series from stage data
        const targetSeries = stageHist
            .filter(s => s.state in STAGE_TARGETS)
            .map(s => ({
                x: new Date(s.last_changed),
                y: STAGE_TARGETS[s.state],
            }));

        // Compute stats
        const bodyVals = bodyAvg.map(p => p.y);
        const duration = (bestRun.end - bestRun.start) / 3600e3;
        const bedtimeStr = bestRun.start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
        const wakeStr = bestRun.end.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
        const dateStr = bestRun.start.toLocaleDateString([], { month: "short", day: "numeric" });

        document.getElementById("lastNightDesc").textContent =
            `${dateStr}: ${bedtimeStr} → ${wakeStr} (${duration.toFixed(1)}h). ` +
            `Body sensor ranged ${Math.min(...bodyVals).toFixed(1)}–${Math.max(...bodyVals).toFixed(1)}°F ` +
            `(avg ${(bodyVals.reduce((a, b) => a + b, 0) / bodyVals.length).toFixed(1)}°F).` +
            (stageHist.length > 2 ? "" : " No sleep stage data yet — first overnight with SleepSync will populate stage bands.");

        // Chart datasets
        const datasets = [
            {
                label: "Body Temp (avg)",
                data: bodyAvg,
                borderColor: "#f47067",
                backgroundColor: "#f4706720",
                borderWidth: 2,
                pointRadius: 0,
                tension: 0.3,
                fill: true,
                yAxisID: "y",
            },
        ];

        if (targetSeries.length > 0) {
            datasets.push({
                label: "Controller Target",
                data: targetSeries,
                borderColor: "#58a6ff",
                borderWidth: 2,
                borderDash: [6, 3],
                pointRadius: 0,
                tension: 0,
                stepped: "before",
                fill: false,
                yAxisID: "y",
            });
        }

        if (scheduleSeries.length > 0) {
            datasets.push({
                label: "Temperature Setting",
                data: scheduleSeries,
                borderColor: "#3fb950",
                backgroundColor: "#3fb95020",
                borderWidth: 2.5,
                pointRadius: 0,
                stepped: "before",
                fill: true,
                yAxisID: "y2",
            });
        }

        // Draw chart
        if (modelNightChart) modelNightChart.destroy();
        modelNightChart = new Chart(document.getElementById("modelNightChart"), {
            type: "line",
            data: { datasets },
            plugins: stageBands.length > 0 ? [stageBandPlugin(stageBands)] : [],
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 300 },
                plugins: {
                    legend: { labels: { color: "#8b949e", usePointStyle: true, pointStyle: "line", padding: 16 } },
                    tooltip: { mode: "index", intersect: false },
                },
                scales: {
                    x: {
                        type: "time",
                        time: { tooltipFormat: "h:mm a", displayFormats: { minute: "h:mm a", hour: "h a" } },
                        grid: { color: "#21262d" },
                        ticks: { color: "#484f58", maxRotation: 0 },
                        title: { display: true, text: "Time", color: "#8b949e" },
                    },
                    y: {
                        position: "left",
                        title: { display: true, text: "Body Temp (°F)", color: "#f47067" },
                        grid: { color: "#21262d" },
                        ticks: { color: "#f47067" },
                    },
                    y2: {
                        position: "right",
                        title: { display: true, text: "Setting (-10 to +10)", color: "#3fb950" },
                        grid: { color: "transparent" },
                        ticks: { color: "#3fb950" },
                        min: -10, max: 10,
                    },
                },
            },
        });

        // Stats grid
        const statsEl = document.getElementById("lastNightStats");
        const gridEl = document.getElementById("nightStatsGrid");
        const schedVals = scheduleSeries.map(p => p.y).filter(v => !isNaN(v));
        const statCards = [
            { label: "Bedtime", value: bedtimeStr, color: "#8b949e" },
            { label: "Wake", value: wakeStr, color: "#8b949e" },
            { label: "Duration", value: `${duration.toFixed(1)}h`, color: "#8b949e" },
            { label: "Avg Body Temp", value: `${(bodyVals.reduce((a, b) => a + b, 0) / bodyVals.length).toFixed(1)}°F`, color: "#f47067" },
            { label: "Min Body Temp", value: `${Math.min(...bodyVals).toFixed(1)}°F`, color: "#58a6ff" },
            { label: "Max Body Temp", value: `${Math.max(...bodyVals).toFixed(1)}°F`, color: "#f0883e" },
        ];
        if (schedVals.length > 0) {
            statCards.push({ label: "Setting Range", value: `${Math.min(...schedVals)} to ${Math.max(...schedVals)}`, color: "#3fb950" });
        }

        gridEl.innerHTML = statCards.map(c =>
            `<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center">
                <div style="font-size:0.8rem;color:#8b949e;margin-bottom:4px">${c.label}</div>
                <div style="font-size:1.5rem;font-weight:bold;color:${c.color}">${c.value}</div>
            </div>`
        ).join("");
        statsEl.style.display = "block";

    } catch (err) {
        console.error("Last night fetch error:", err);
        document.getElementById("lastNightDesc").textContent = `Error loading last night data: ${err.message}`;
        renderPlaceholderNightChart();
    }
}

// ─── Interaction handlers ──────────────────────────────
function setZone(z) {
    zone = z;
    document.querySelectorAll(".zone-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.zone === z);
    });
    fetchHistory();
}



async function fetchHistory() {
    if (!token) return;
    try {
        const data = await fetchAll();
        updateCharts(data);
        await updateLiveStats();
    } catch (err) {
        console.error("Fetch error:", err);
        document.getElementById("statusDot").className = "dot";
        document.getElementById("statusText").textContent = `Error: ${err.message}`;
    }
}

// ─── Token input ───────────────────────────────────────
function showTokenPrompt() {
    document.getElementById("tokenOverlay").style.display = "flex";
    document.getElementById("tokenInput").focus();
}

function submitToken() {
    const t = document.getElementById("tokenInput").value.replace(/\s+/g, "").trim();
    if (!t) return;
    token = t;
    localStorage.setItem("ha_token", token);
    document.getElementById("tokenOverlay").style.display = "none";
    document.getElementById("connectBtn").style.display = "none";
    boot();
}

function boot() {
    initCharts();
    if (token) {
        fetchHistory();
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(() => fetchHistory(), POLL_INTERVAL);
    }
}

// ─── Boot ──────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
    const saved = localStorage.getItem("ha_token");
    // Clear bad tokens (empty or non-JWT from old prompt() bug)
    if (saved && saved.startsWith("eyJ")) {
        token = saved;
    } else {
        localStorage.removeItem("ha_token");
        token = "";
    }
    initCharts();
    if (token) {
        fetchHistory();
        pollTimer = setInterval(() => fetchHistory(), POLL_INTERVAL);
        document.getElementById("connectBtn").style.display = "none";
    } else {
        // No valid token — show Model view so there's something to see
        setView("model");
        document.getElementById("statusText").textContent = "No token — Model view only. Enter token for live data.";
    }
    document.getElementById("tokenInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitToken();
    });
});
