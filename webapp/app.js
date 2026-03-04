// Perfectly Snug Web Controller — Frontend JS

let currentZone = "left";
let refreshInterval = null;

// ─── API Calls ─────────────────────────────────────────
async function api(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Request failed");
    }
    return res.json();
}

async function getStatus(zone) {
    return api("GET", `/api/zone/${zone}/status`);
}

async function setSetting(zone, name, value) {
    return api("PUT", `/api/zone/${zone}/setting/${name}`, { value });
}

// ─── UI Updates ────────────────────────────────────────
function tempDisplay(val) {
    const display = val - 10;
    return display > 0 ? `+${display}` : `${display}`;
}

function tempClass(val) {
    const d = val - 10;
    if (d < 0) return "cool";
    if (d > 0) return "warm";
    return "neutral";
}

function updateUI(data) {
    const s = data.settings;

    // Temperature sliders
    ["l1", "l2", "l3"].forEach(key => {
        if (s[key]) {
            const slider = document.getElementById(`slider-${key}`);
            const display = document.getElementById(`display-${key}`);
            slider.value = s[key].raw;
            display.textContent = tempDisplay(s[key].raw);
            display.className = `slider-value ${tempClass(s[key].raw)}`;
        }
    });

    // Foot warmer
    if (s.footWarmer) {
        document.querySelectorAll("#foot-warmer-buttons .option-btn").forEach(btn => {
            btn.classList.toggle("active", parseInt(btn.dataset.value) === s.footWarmer.raw);
        });
    }

    // Responsive cooling
    if (s.coolingMode) {
        document.getElementById("cooling-toggle").checked = s.coolingMode.responsive;
        document.getElementById("cooling-label").textContent = s.coolingMode.responsive ? "On" : "Off";
    }

    // Schedule
    if (s.scheduleEnable) {
        const enabled = s.scheduleEnable.enabled;
        document.getElementById("schedule-toggle").checked = enabled;
        document.getElementById("schedule-label").textContent = enabled ? "On" : "Off";
        document.getElementById("schedule-details").classList.toggle("hidden", !enabled);
    }

    // Profile mode (1 vs 3 levels)
    if (s.profileEnable) {
        const three = s.profileEnable.threeLevels;
        document.getElementById("btn-1level").classList.toggle("active", !three);
        document.getElementById("btn-3level").classList.toggle("active", three);
        document.getElementById("duration-section").style.display = three ? "" : "none";
    }

    if (s.sched1Start) document.getElementById("sched1-start").value = s.sched1Start.display;
    if (s.sched1Stop) document.getElementById("sched1-stop").value = s.sched1Stop.display;
    if (s.sched1Days) renderDays("sched1-days", s.sched1Days.raw, "sched1Days");

    if (s.sched2Start) document.getElementById("sched2-start").value = s.sched2Start.display;
    if (s.sched2Stop) document.getElementById("sched2-stop").value = s.sched2Stop.display;
    if (s.sched2Days) renderDays("sched2-days", s.sched2Days.raw, "sched2Days");

    // Start/Wake length
    if (s.t1) document.getElementById("start-length").value = s.t1.minutes;
    if (s.t3) document.getElementById("wake-length").value = s.t3.minutes;

    // Quiet mode & volume
    if (s.quietEnable) {
        document.getElementById("quiet-toggle").checked = s.quietEnable.raw === 1;
    }
    if (s.volume) {
        document.getElementById("slider-volume").value = s.volume.raw;
        document.getElementById("display-volume").textContent = s.volume.raw;
    }

    // Status
    if (s.running) {
        const el = document.getElementById("stat-running");
        el.textContent = s.running.running ? "Yes" : "No";
        el.style.color = s.running.running ? "var(--success)" : "var(--text-dim)";
    }
    if (s.side) document.getElementById("stat-side").textContent = s.side.display || s.side.raw;
    if (s.burstMode) document.getElementById("stat-burst").textContent = s.burstMode.mode;
    if (s.runProgress) document.getElementById("stat-progress").textContent = s.runProgress.raw;

    // Temperatures
    if (s.tempAmbient) document.getElementById("stat-temp-ambient").textContent = s.tempAmbient.tempF + "°F";
    if (s.tempSetpoint) document.getElementById("stat-temp-setpoint").textContent = s.tempSetpoint.tempF + "°F";
    if (s.tempSensorRight) document.getElementById("stat-temp-tsr").textContent = s.tempSensorRight.tempF + "°F";
    if (s.tempSensorCenter) document.getElementById("stat-temp-tsc").textContent = s.tempSensorCenter.tempF + "°F";
    if (s.tempSensorLeft) document.getElementById("stat-temp-tsl").textContent = s.tempSensorLeft.tempF + "°F";
    if (s.tempHeaterHead) document.getElementById("stat-temp-hh").textContent = s.tempHeaterHead.tempF + "°F";
    if (s.tempHeaterFoot) document.getElementById("stat-temp-hf").textContent = s.tempHeaterFoot.tempF + "°F";

    // Connection status
    document.getElementById("connection-status").className = "status-dot connected";
    document.getElementById("last-update").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

// ─── Schedule Days ─────────────────────────────────────
function renderDays(containerId, bitmask, settingName) {
    const container = document.getElementById(containerId);
    const dayNames = ["S", "M", "T", "W", "T", "F", "S"];
    container.innerHTML = "";
    dayNames.forEach((name, i) => {
        const btn = document.createElement("button");
        btn.className = `day-btn ${bitmask & (1 << i) ? "active" : ""}`;
        btn.textContent = name;
        btn.onclick = () => toggleDay(settingName, bitmask, i);
        container.appendChild(btn);
    });
}

async function toggleDay(settingName, currentBitmask, dayIndex) {
    const newBitmask = currentBitmask ^ (1 << dayIndex);
    await sendSetting(settingName, newBitmask);
}

// ─── Event Handlers ────────────────────────────────────
function onSliderInput(key, value) {
    const display = document.getElementById(`display-${key}`);
    display.textContent = tempDisplay(parseInt(value));
    display.className = `slider-value ${tempClass(parseInt(value))}`;
}

async function onSliderChange(key, value) {
    await sendSetting(key, parseInt(value));
}

async function setFootWarmer(level) {
    if (level === 0) {
        // Turn off: set heater limit to 0, then level to 0
        await sendSetting("heaterLimit", 0);
        await sendSetting("footWarmer", 0);
    } else {
        // Turn on: set level, then enable heater limit to 100
        await sendSetting("footWarmer", level);
        await sendSetting("heaterLimit", 100);
    }
}

async function toggleCooling(checked) {
    await sendSetting("coolingMode", checked ? 1 : 0);
    document.getElementById("cooling-label").textContent = checked ? "On" : "Off";
}

async function toggleSchedule(checked) {
    await sendSetting("scheduleEnable", checked ? 1 : 0);
    document.getElementById("schedule-label").textContent = checked ? "On" : "Off";
    document.getElementById("schedule-details").classList.toggle("hidden", !checked);
}

async function toggleQuiet(checked) {
    await sendSetting("quietEnable", checked ? 1 : 0);
}

function onVolumeInput(value) {
    document.getElementById("display-volume").textContent = value;
}

async function onVolumeChange(value) {
    await sendSetting("volume", parseInt(value));
}

async function setScheduleTime(settingName, timeStr) {
    const [h, m] = timeStr.split(":").map(Number);
    const encoded = (h << 8) | m;
    await sendSetting(settingName, encoded);
}

async function setProfileMode(value) {
    await sendSetting("profileEnable", value);
}

async function setDuration(settingName, value) {
    await sendSetting(settingName, parseInt(value));
}

// ─── Sending Wrapper ───────────────────────────────────
async function sendSetting(name, value) {
    const content = document.getElementById("zone-content");
    content.classList.add("sending");
    try {
        await setSetting(currentZone, name, value);
    } catch (e) {
        console.error("Failed to set:", name, value, e);
        alert(`Failed to update ${name}: ${e.message}`);
    } finally {
        content.classList.remove("sending");
        // Refresh after a short delay
        setTimeout(() => refreshStatus(), 500);
    }
}

// ─── Zone Switching ────────────────────────────────────
function switchZone(zone) {
    currentZone = zone;
    document.querySelectorAll(".zone-tab").forEach(tab => {
        tab.classList.toggle("active", tab.dataset.zone === zone);
    });
    refreshStatus();
}

// ─── Refresh Loop ──────────────────────────────────────
async function refreshStatus() {
    const statusEl = document.getElementById("connection-status");
    statusEl.className = "status-dot loading";
    try {
        const data = await getStatus(currentZone);
        updateUI(data);
    } catch (e) {
        console.error("Refresh failed:", e);
        statusEl.className = "status-dot disconnected";
        document.getElementById("last-update").textContent = "Connection lost";
    }
}

// ─── Init ──────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    refreshStatus();
    refreshInterval = setInterval(refreshStatus, 15000);
});
