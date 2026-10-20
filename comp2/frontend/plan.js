/**
 * Track panel: the published circuit, as a flat floor plan.
 *
 * A read-only view of the same cage the 3D stage draws, fed by the same
 * `{"type":"scene"}` and `{"type":"pose"}` messages, plus `{"type":"race"}` so
 * the gates colour up as they are passed. There is no editing here: tracks are
 * published layouts that must match the physical cage, so organisers edit the
 * JSON files in comp2/tracks instead.
 */
(() => {
  const panel = document.getElementById("track-panel");
  const canvas = document.getElementById("track-plan");
  const picker = document.getElementById("track");
  const difficultyEl = document.getElementById("track-difficulty");
  const noteEl = document.getElementById("plan-note");
  const ctx = canvas.getContext("2d");

  const GATE_DIAMETER_M = 0.70; // outer edge of the fabric ring
  const GATE_OPENING_M = 0.50;  // its clear opening

  const COL = {
    floor: "#16233c", grid: "#24354f", wall: "#7f9bd4",
    gate: "#f7941d", gateNext: "#22c55e", gatePassed: "#64748b",
    takeoff: "#38bdf8", landing: "#f7941d", drone: "#f7941d", label: "#e2e8f0",
  };

  let scene = null;
  let pose = null;
  let race = null;
  let running = false;
  let view = null;             // {x0, y0, s} — plan pixels per metre

  // --- geometry ------------------------------------------------------------

  /** Fit a `w` x `d` cage into the canvas, preserving its aspect. */
  function fit(w, d, cw, ch, pad) {
    const s = Math.min((cw - 2 * pad) / w, (ch - 2 * pad) / d);
    return { s, x0: (cw - w * s) / 2, y0: (ch - d * s) / 2 };
  }

  // y grows north but canvas rows grow down, so the plan is flipped vertically —
  // same convention as the camera minimap, which appears side by side with it.
  const toPx = (x, y) => [view.x0 + x * view.s, view.y0 + (scene.depth_m - y) * view.s];

  // --- drawing -------------------------------------------------------------

  function draw() {
    const cw = canvas.clientWidth, ch = canvas.clientHeight;
    if (!cw || !ch) return;
    const dpr = Math.min(devicePixelRatio, 2);
    if (canvas.width !== cw * dpr || canvas.height !== ch * dpr) {
      canvas.width = cw * dpr;
      canvas.height = ch * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    if (!scene) return;

    const w = scene.width_m, d = scene.depth_m;
    view = fit(w, d, cw, ch, 12);
    const [rx, ry] = toPx(0, d);
    ctx.fillStyle = COL.floor;
    ctx.fillRect(rx, ry, w * view.s, d * view.s);

    ctx.strokeStyle = COL.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = 0; x <= w + 1e-6; x += 0.5) {
      const [px] = toPx(x, 0);
      ctx.moveTo(px, ry); ctx.lineTo(px, ry + d * view.s);
    }
    for (let y = 0; y <= d + 1e-6; y += 0.5) {
      const [, py] = toPx(0, y);
      ctx.moveTo(rx, py); ctx.lineTo(rx + w * view.s, py);
    }
    ctx.stroke();

    ctx.strokeStyle = COL.wall;
    ctx.lineWidth = 2;
    ctx.strokeRect(rx, ry, w * view.s, d * view.s);

    drawPad(scene.start, COL.takeoff, "S");
    drawPad(scene.finish, COL.landing, "F");

    // the racing line between gates, drawn under them
    if (scene.gates?.length && scene.start) {
      ctx.strokeStyle = "rgba(148, 163, 184, 0.35)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(...toPx(scene.start[0], scene.start[1]));
      for (const g of scene.gates) ctx.lineTo(...toPx(g.x, g.y));
      if (scene.finish) ctx.lineTo(...toPx(scene.finish[0], scene.finish[1]));
      ctx.stroke();
      ctx.setLineDash([]);
    }

    (scene.gates || []).forEach((g, i) => drawGate(g, i));
    if (pose) drawDrone();
  }

  function gateColour(i) {
    if (!race) return COL.gate;
    const g = (race.gates || [])[i];
    if (g?.passed) return COL.gatePassed;
    if (race.next_gate === i) return COL.gateNext;
    return COL.gate;
  }

  /** A gate from above: the ring's full width, with the clear opening drawn
   * heavier than the fabric band either side of it, plus its number. */
  function drawGate(g, i) {
    const yaw = (g.yaw_deg * Math.PI) / 180;
    const lx = Math.cos(yaw), ly = -Math.sin(yaw);      // in-plane horizontal
    const rOut = (g.diameter_m ?? GATE_DIAMETER_M) / 2;
    const rIn = (g.opening_diameter_m ?? GATE_OPENING_M) / 2;
    ctx.strokeStyle = gateColour(i);
    ctx.lineCap = "round";
    const span = (r, width) => {
      const [ax, ay] = toPx(g.x - r * lx, g.y - r * ly);
      const [bx, by] = toPx(g.x + r * lx, g.y + r * ly);
      ctx.lineWidth = width;
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    };
    span(rOut, 2);                                     // ring, outer edge to outer edge
    span(rIn, 5);                                      // the clear opening
    // a tick along the pass direction, so the required side is readable
    const [cx, cy] = toPx(g.x, g.y);
    const nx = Math.sin(yaw), ny = Math.cos(yaw);
    const [tx, ty] = toPx(g.x + 0.28 * nx, g.y + 0.28 * ny);
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(tx, ty); ctx.stroke();
    label(String(i + 1), cx + 10, cy - 6, COL.label);
  }

  function drawPad(p, color, text) {
    if (!p) return;
    const [px, py] = toPx(p[0], p[1]);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(px, py, 9, 0, 2 * Math.PI); ctx.stroke();
    label(text, px, py + 20, color);
  }

  function drawDrone() {
    const [px, py] = toPx(pose.x, pose.y);
    const h = (pose.heading * Math.PI) / 180;
    ctx.strokeStyle = ctx.fillStyle = COL.drone;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(px, py, 4, 0, 2 * Math.PI); ctx.fill();
    ctx.beginPath();
    ctx.moveTo(px, py);
    ctx.lineTo(px + 13 * Math.sin(h), py - 13 * Math.cos(h));
    ctx.stroke();
  }

  function label(text, px, py, color) {
    ctx.fillStyle = color;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(text, px, py);
  }

  // --- messages ------------------------------------------------------------

  function send(msg) {
    if (window.COMP2_SEND) window.COMP2_SEND(msg);
  }

  function setScene(desc) {
    scene = desc;
    race = null;
    if (scene) {
      difficultyEl.textContent = scene.difficulty ? `×${scene.difficulty}` : "";
      noteEl.textContent = scene.gates
        ? `${scene.gates.length} gates · take off at S, land at F · tracks are `
          + "published layouts — edit comp2/tracks/*.json to change them"
        : "";
    }
    draw();
  }

  function setTracks(msg) {
    const list = msg.tracks;
    panel.hidden = !list;
    if (!list) return;
    picker.replaceChildren();
    for (const t of list) {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `${t.name} (×${t.difficulty})`;
      opt.title = t.description || "";
      picker.appendChild(opt);
    }
    if (msg.current) picker.value = msg.current;
  }

  window.COMP2_BUS.on((msg) => {
    if (msg.type === "scene") setScene(msg.scene);
    else if (msg.type === "tracks") setTracks(msg);
    else if (msg.type === "pose") { pose = msg; draw(); }
    else if (msg.type === "race") { race = msg; draw(); }
    else if (msg.type === "reset") { pose = null; race = null; draw(); }
    else if (msg.type === "running") { running = msg.running; picker.disabled = running; }
  });

  picker.onchange = () => send({ type: "track", name: picker.value });
  new ResizeObserver(draw).observe(canvas);
})();
