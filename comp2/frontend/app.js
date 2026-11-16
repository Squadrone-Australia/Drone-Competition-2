const statusEl = document.getElementById("status");
const pythonClientEl = document.getElementById("python-client");
const consoleEl = document.getElementById("console");
const videoEl = document.getElementById("video");
const cameraStateEl = document.getElementById("camera-state");
const raceSummaryEl = document.getElementById("race-summary");
const raceClockEl = document.getElementById("race-clock");
const raceStateEl = document.getElementById("race-state");
const raceGatesEl = document.getElementById("race-gates");
const raceScoreEl = document.getElementById("race-score");
const droneModeEl = document.getElementById("drone-mode");
const useTelloEl = document.getElementById("use-tello");
const reconnectEl = document.getElementById("reconnect-drone");

let ws;
let lastFrameUrl;
let raceRunning = false;
let droneMode = null;
let droneSwitching = false;
let pythonClientConnected = false;
let lastRaceState = null;
let droneLinkOk = true;
let videoBlocked = false;
let linkMessage = null;

/**
 * Shared message bus for the independent arena and track renderers.
 * Scene state can arrive before those scripts subscribe, so state is replayed.
 */
const bus = window.COMP2_BUS = {
  handlers: [],
  sticky: {},
  on(fn) {
    this.handlers.push(fn);
    Object.values(this.sticky).forEach(fn);
  },
  emit(msg) {
    // The module-based 3D view can finish loading after the WebSocket's initial
    // state burst. Pose must be replayed too or a stationary drone remains at
    // the scene default until its next movement.
    if (msg.type === "scene" || msg.type === "tracks" || msg.type === "race"
        || msg.type === "pose" || msg.type === "arena") {
      this.sticky[msg.type] = msg;
    }
    this.handlers.forEach((fn) => fn(msg));
  },
};

function log(message) {
  consoleEl.textContent += `${message}\n`;
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

function send(message) {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(message));
  } else {
    log("Cannot send command: simulator connection is unavailable.");
  }
}

window.COMP2_SEND = send;

function updateTargetInterlock() {
  useTelloEl.disabled = raceRunning
    || pythonClientConnected
    || droneSwitching
    || !droneMode;
  updateReconnect();
}

// Only the real aircraft can lose its link, so the button only exists there.
// It stays available while the link looks fine: a rebooted Tello answers
// nothing but reports nothing either, and this is the cure.
function updateReconnect() {
  reconnectEl.hidden = droneMode !== "tello";
  reconnectEl.disabled = raceRunning || pythonClientConnected || droneSwitching;
  reconnectEl.classList.toggle("needs-attention", droneMode === "tello" && !droneLinkOk);
}

// A camera the laptop's firewall is blocking is not a lost drone: it still
// answers and still flies, so it gets its own status rather than "offline".
function showDroneLink(msg) {
  const wasOk = droneLinkOk;
  const wasBlocked = videoBlocked;
  droneLinkOk = msg.ok !== false;
  videoBlocked = msg.video_blocked === true;
  updateReconnect();
  if (msg.message && (droneLinkOk !== wasOk || videoBlocked !== wasBlocked
                      || msg.message !== linkMessage)) {
    log((droneLinkOk && !videoBlocked ? "" : "Warning: ") + msg.message);
  }
  linkMessage = msg.message;
  if (!droneLinkOk) {
    statusEl.textContent = "drone offline, reconnecting";
    statusEl.className = "status bad";
  } else if (videoBlocked) {
    statusEl.textContent = "connected, no video";
    statusEl.className = "status bad";
    cameraStateEl.classList.remove("live");
    cameraStateEl.lastElementChild.textContent = "no video from drone";
  } else if (wasOk === false || wasBlocked) {
    statusEl.textContent = "connected";
    statusEl.className = "status ok";
  }
}

function setRunning(running) {
  raceRunning = running;
  updateTargetInterlock();
  document.body.classList.toggle("running", running);
  bus.emit({ type: "running", running });
}

function showDroneMode(msg) {
  const previousMode = droneMode;
  droneMode = msg.mode;
  droneSwitching = Boolean(msg.switching);
  const names = { sim: "simulator", tello: "real Tello", mock: "mock drone" };
  droneModeEl.textContent = `Drone: ${names[droneMode] || droneMode}`;
  useTelloEl.textContent = droneSwitching
    ? "Switching..."
    : (droneMode === "tello" ? "Use Simulator" : "Use real Tello");
  useTelloEl.title = droneMode === "tello"
    ? "Switch back to the simulator"
    : "Connect to a real DJI Tello";
  updateTargetInterlock();
  if (previousMode && previousMode !== droneMode) {
    lastRaceState = null;
    log(`Switched to ${names[droneMode] || droneMode}.`);
  }
}

function showPythonClient(msg) {
  const wasConnected = pythonClientConnected;
  pythonClientConnected = Boolean(msg.connected);
  pythonClientEl.textContent = pythonClientConnected ? "IDE connected" : "IDE disconnected";
  pythonClientEl.classList.toggle("ok", pythonClientConnected);
  pythonClientEl.classList.toggle("bad", !pythonClientConnected);
  updateTargetInterlock();
  if (pythonClientConnected !== wasConnected) {
    log(pythonClientConnected
      ? `External Python connected to ${msg.drone || droneMode}.`
      : `External Python disconnected${msg.reason ? `: ${msg.reason}` : "."}`);
  }
}

function showRace(race) {
  raceSummaryEl.textContent = race.total
    ? `${Math.min(race.passed + 1, race.total)} / ${race.total}`
    : "- / -";
  raceClockEl.textContent = `${(race.elapsed_s || 0).toFixed(2)} s`;

  const stateNames = {
    idle: "ready: take off to start the clock",
    running: "racing",
    finished: "finished",
    dnf: "landed early: did not finish",
  };
  raceStateEl.textContent = stateNames[race.state] || race.state;
  raceStateEl.className = race.state;

  raceGatesEl.replaceChildren(...(race.gates || []).map((gateData) => {
    const gate = document.createElement("span");
    gate.className = "gate"
      + (gateData.passed ? " passed" : (gateData.index === race.next_gate ? " next" : ""));

    const ring = document.createElement("span");
    ring.className = "ring";
    ring.textContent = gateData.index + 1;

    const offset = document.createElement("span");
    offset.className = "offset";
    offset.textContent = gateData.passed ? String(Math.round(gateData.offset_m * 100)) : "·";
    gate.append(ring, offset);
    gate.title = gateData.passed
      ? `gate ${gateData.index + 1}: passed at ${gateData.time_s.toFixed(2)} s, `
        + `${Math.round(gateData.offset_m * 100)} cm from the centre`
      : (gateData.index === race.next_gate
        ? `gate ${gateData.index + 1}: next`
        : `gate ${gateData.index + 1}`);
    return gate;
  }));

  const score = race.score;
  raceScoreEl.hidden = !(score && (race.state === "finished" || race.state === "dnf"));
  if (!raceScoreEl.hidden) {
    document.getElementById("s-smooth").textContent = score.smoothness.toFixed(2);
    document.getElementById("s-acc").textContent = score.accuracy.toFixed(2);
    document.getElementById("s-time").textContent = score.time.toFixed(2);
    document.getElementById("s-total").textContent = score.total.toFixed(1);
  }

  if (race.state === "finished" && lastRaceState !== "finished") {
    log(`Finished: ${race.passed}/${race.total} gates in ${race.elapsed_s.toFixed(2)} s, `
      + `score ${score ? score.total.toFixed(1) : "-"}.`);
  } else if (race.state === "dnf" && lastRaceState !== "dnf") {
    log(`DNF: landed with ${race.passed}/${race.total} gates passed.`);
  }
  lastRaceState = race.state;
}

function showFrame(blob) {
  const url = URL.createObjectURL(blob);
  videoEl.src = url;
  if (lastFrameUrl) URL.revokeObjectURL(lastFrameUrl);
  lastFrameUrl = url;
  cameraStateEl.classList.add("live");
  cameraStateEl.lastElementChild.textContent = "live";
}

// --- version notices ------------------------------------------------------
// The server sends "update_available" when GitHub's main has commits this copy
// lacks, and "updated" (once per update) with what the last update changed.
// Both are re-sent on every reconnect, so a closed one is remembered by the
// newest commit it listed: the same news stays closed, newer news shows again.

const updateNoticesEl = document.getElementById("update-notices");
const DISMISSED_KEY = "comp2.dismissedUpdates";
const shownUpdates = new Map();

function dismissedUpdates() {
  try {
    return new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function rememberDismissed(key) {
  const keys = dismissedUpdates();
  keys.add(key);
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify([...keys].slice(-20)));
  } catch {
    // Private window or blocked storage: it stays closed until a reload.
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function showUpdateNotice(msg) {
  const commits = Array.isArray(msg.commits) ? msg.commits : [];
  if (!commits.length) return;
  const key = `${msg.type}:${commits[0].sha}`;
  if (shownUpdates.get(msg.type)?.key === key) return;  // a reconnect re-sent it
  shownUpdates.get(msg.type)?.node?.remove();

  const available = msg.type === "update_available";
  const total = Math.max(msg.total || 0, commits.length);
  const title = available
    ? `A newer COMP2 is available (${plural(total, "change")})`
    : `COMP2 was updated (${plural(total, "change")})`;
  const hint = available
    ? "Stop COMP2 (Ctrl+C in its window), then start it again with Start COMP2 to update."
    : `Now on version ${msg.to}, from ${msg.from}. Here is what changed.`;
  log(available ? `${title}. ${hint}` : `${title}: now ${msg.to}, was ${msg.from}.`);
  if (dismissedUpdates().has(key)) {
    shownUpdates.set(msg.type, { key, node: null });
    return;
  }

  const node = el("section", `update-notice ${available ? "available" : "updated"}`);
  node.setAttribute("aria-label", title);
  const head = el("div", "update-head");
  const text = el("div", "update-text");
  text.append(el("div", "update-title", title), el("div", "update-hint", hint));
  const list = el("ol", "update-list");
  for (const commit of commits) {
    const item = el("li");
    const details = el("details");
    const summary = el("summary", commit.body ? "" : "plain", commit.subject);
    summary.append(el("span", "update-sha", commit.sha));
    details.append(summary);
    if (commit.body) details.append(el("div", "update-body", commit.body));
    item.append(details);
    list.append(item);
  }
  if (total > commits.length) {
    list.append(el("li", "update-more", `and ${plural(total - commits.length, "earlier change")}`));
  }
  // The changelog after an update is the point, so it opens; news of an update
  // not yet installed stays a single line until asked for.
  list.hidden = available;
  const toggle = el("button", "", available ? "What's new" : "Hide list");
  toggle.type = "button";
  toggle.onclick = () => {
    list.hidden = !list.hidden;
    toggle.textContent = list.hidden ? "What's new" : "Hide list";
  };
  const close = el("button", "", "Close");
  close.type = "button";
  close.title = "Hide this until there is newer news";
  close.onclick = () => {
    rememberDismissed(key);
    node.remove();
    shownUpdates.set(msg.type, { key, node: null });
  };
  head.append(text, toggle, close);
  node.append(head, list);
  // Newest news first: the changelog of the running version sits under news
  // of the next one.
  if (available) updateNoticesEl.prepend(node);
  else updateNoticesEl.append(node);
  shownUpdates.set(msg.type, { key, node });
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}/ws`);
  ws.binaryType = "blob";
  ws.onopen = () => {
    statusEl.textContent = "connected";
    statusEl.className = "status ok";
  };
  ws.onclose = () => {
    statusEl.textContent = "disconnected: retrying";
    statusEl.className = "status bad";
    cameraStateEl.classList.remove("live");
    cameraStateEl.lastElementChild.textContent = "waiting for frames";
    setRunning(false);
    setTimeout(connect, 1000);
  };
  ws.onmessage = (event) => {
    if (event.data instanceof Blob) {
      showFrame(event.data);
      return;
    }

    const msg = JSON.parse(event.data);
    bus.emit(msg);
    if (msg.type === "finished") {
      log(`Run ${msg.reason}${msg.detail ? `: ${msg.detail}` : "."}`);
      setRunning(false);
    } else if (msg.type === "output") {
      const prefix = msg.stream === "stderr" ? "ERROR  " : "";
      log(prefix + msg.text.trimEnd());
    } else if (msg.type === "script") {
      setRunning(msg.state === "started");
    } else if (msg.type === "python_client") {
      showPythonClient(msg);
    } else if (msg.type === "race") {
      showRace(msg);
    } else if (msg.type === "drone_mode") {
      showDroneMode(msg);
    } else if (msg.type === "drone_link") {
      showDroneLink(msg);
    } else if (msg.type === "error") {
      log(`Warning: ${msg.message}`);
    } else if (msg.type === "warning") {
      // Runtime warnings from the drone API: a clamped distance or speed. They
      // matter because the program otherwise ran without doing what was meant.
      log(`Warning: ${msg.message}`);
    } else if (msg.type === "estopped") {
      // `ok: false` means the cut never reached the drone; say so rather than
      // the reassuring line. An older server omits `ok` and reads as success.
      log(msg.ok === false
        ? `WARNING: ${msg.message || "EMERGENCY STOP did not reach the drone"}`
        : "EMERGENCY STOP");
      setRunning(false);
    } else if (msg.type === "update_available" || msg.type === "updated") {
      showUpdateNotice(msg);
    } else if (msg.type === "reset") {
      lastRaceState = null;
      log(msg.repositioned
        ? "Returned to the simulator takeoff pad."
        : "Race cleared; the physical drone has not moved.");
    }
  };
}

connect();

document.getElementById("stop").onclick = () => send({ type: "stop" });
document.getElementById("reset").onclick = () => send({ type: "reset" });
useTelloEl.onclick = () => {
  const useSimulator = droneMode === "tello";
  const confirmed = window.confirm(useSimulator
    ? "Switch to the simulator? Make sure the real Tello is safely landed first. Programs will stop controlling the physical drone."
    : "Switch to the real Tello? Join the TELLO Wi-Fi first. Programs will control the physical drone.");
  if (confirmed) send({ type: "switch_drone", mode: useSimulator ? "sim" : "tello" });
};
reconnectEl.onclick = () => {
  log("Reconnecting to the Tello...");
  send({ type: "reconnect_drone" });
};
document.getElementById("estop").onclick = () => send({ type: "estop" });
