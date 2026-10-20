/**
 * Third-person 3D view of the simulated arena.
 *
 * The camera panel shows what the drone sees; this shows *where the drone is*,
 * which is the half a student cannot reconstruct from a first-person feed. It is
 * driven entirely by `{"type":"scene"}` (once) and `{"type":"pose"}` (~30 Hz)
 * messages — it never touches the team's program or the detector, and it is
 * strictly a display: the arena coordinates it draws with are not reachable from
 * the contestant API.
 *
 * Adapters with no arena-absolute pose (mock, real Tello) send `scene: null`,
 * and the stage shows a placeholder instead.
 *
 * Coordinates. The simulator uses the published competition frame: x along
 * the cage's 6 m length, y across its 4 m width, z up, heading clockwise
 * from +y. Three.js is y-up, so:  three.x = x,  three.y = z,  three.z = -y,
 * and a heading of h degrees is a rotation of -h about Y.
 */
import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";

// Fallbacks for a scene payload that does not carry the gate's own numbers.
const GATE_DIAMETER_M = 0.70;  // outer edge of the Squadrone fabric ring
const GATE_OPENING_M = 0.50;   // its clear opening, what is scored
const GATE_ORANGE_HALF_ANGLE_DEG = 40; // each orange segment, either side of vertical
const GATE_CHECKER_ROWS = 4;   // checker squares across the band
const GATE_TUBE_M = 0.02;      // the stand's PVC tube
const GATE_ELBOW_M = 0.045;    // how far each white fitting runs along the tube
const GATE_MAST_M = 0.20;      // ring's lowest point down to the stand's crossbar
const GATE_STRING_M = 0.004;   // a hanging gate's cord (fallback; the scene sends it)
const GATE_STRING_ANGLE_DEG = 30; // where the cords meet the band, from 12 o'clock
const GATE_FOOT_M = 0.37;      // crossbar, and each foot across the gate plane
const GATE_CLAMP_M = 0.05;     // white fitting between the ring and the mast
const DRONE_SPAN_M = 0.22;     // deliberately enlarged, but still fits the 0.50 m opening
const DRONE_LANDED_Y_M = 0.06; // pose z=0 is the floor, not the airframe centre
const TRAIL_MAX = 400;
const PROP_SPIN = 25;        // rad/s while flying
const COL = {
  bg: 0x0b1220, floor: 0x27364f, grid: 0x3b4e78, gridMajor: 0x5b74ac,
  wall: 0x7f9bd4, body: 0xe2e8f0, arm: 0xf7941d, prop: 0x93c5fd,
  post: 0x475569, trail: 0x38bdf8, fov: 0x38bdf8,
  gate: 0xf7941d, gateNext: 0x22c55e, gatePassed: 0x64748b, elbow: 0xeef0f2,
  ringOrange: "#f54b1e", checkDark: "#16161e", checkLight: "#e2e6ec", print: "#fafafa",
  stand: 0xc6c6c4,
  takeoff: 0x38bdf8, landing: 0xf7941d,
};

const deg = (d) => (d * Math.PI) / 180;
const toThree = (x, y, z) => new THREE.Vector3(x, z, -y);

/** Move `from` a fraction `k` towards `to` the short way round a 360 deg circle. */
function chaseAngle(from, to, k) {
  const delta = ((to - from + 540) % 360) - 180;
  return (from + delta * k + 360) % 360;
}

export function createStage(container, { hfovDeg = 70, aspect = 4 / 3 } = {}) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(COL.bg);
  scene.fog = new THREE.Fog(COL.bg, 10, 34);

  const camera = new THREE.PerspectiveCamera(52, 1, 0.05, 200);
  camera.position.set(3, 4, 6);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI / 2 - 0.02;
  controls.enabled = false;

  scene.add(new THREE.HemisphereLight(0x9fc0ff, 0x121a2c, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.5);
  sun.position.set(4, 9, 5);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -8, right: 8, top: 8, bottom: -8, far: 30 });
  sun.shadow.camera.updateProjectionMatrix();
  scene.add(sun);
  scene.add(sun.target);

  const arena = new THREE.Group();
  scene.add(arena);
  const drone = buildDrone(hfovDeg, aspect);
  drone.group.visible = false;
  scene.add(drone.group);
  // the blob shadow lives in world space, not on the drone, so it stays flat on
  // the floor while the airframe banks
  drone.shadow.visible = false;
  scene.add(drone.shadow);

  const trail = buildTrail();
  scene.add(trail.line);

  // Pose arrives in discrete network updates; the renderer runs at display rate
  // and chases them, which is what turns 30 Hz of samples into smooth flight.
  const target = { x: 0, y: 0, z: 0, heading: 0, roll: 0, pitch: 0, flying: false };
  const shown = { ...target };
  let started = false;
  let mode = "follow";
  let showTrail = true;
  let arenaW = 3, arenaD = 6;
  let gateMats = [];               // one halo material per gate, tinted by race state
  const clock = new THREE.Clock();

  function setScene(desc) {
    disposeChildren(arena);          // a reconnect rebuilds the arena from scratch
    trail.reset();
    gateMats = [];
    if (!desc) { drone.group.visible = drone.shadow.visible = false; return; }
    arenaW = desc.width_m ?? desc.size_m;
    arenaD = desc.depth_m ?? desc.size_m;
    gateMats = buildArena(arena, desc);
    // fog only for the void beyond the arena — pulled in any closer it greys out
    // the room itself, worst of all from the top-down camera
    const span = Math.max(arenaW, arenaD);
    scene.fog.near = span * 2.5;
    scene.fog.far = span * 9;
    // The sun's shadow frustum is a fixed box, so it has to be resized to the
    // room — left at the square arena's size, a 10 m corridor loses every
    // shadow past the halfway point.
    sun.position.set(arenaW / 2 + span * 0.35, span * 1.4, -arenaD / 2 + span * 0.45);
    sun.target.position.set(arenaW / 2, 0, -arenaD / 2);
    sun.target.updateMatrixWorld();
    Object.assign(sun.shadow.camera,
                  { left: -span, right: span, top: span, bottom: -span, far: span * 5 });
    sun.shadow.camera.updateProjectionMatrix();
    drone.group.visible = drone.shadow.visible = true;
    const [sx, sy] = desc.start ?? [arenaW / 2, arenaD / 2];
    Object.assign(target, { x: sx, y: sy, z: 0 });
    Object.assign(shown, target);
    started = false;
  }

  /** Tint the gates from a `{"type":"race"}` message: passed grey, next green. */
  function setRace(race) {
    (race?.gates || []).forEach((g, i) => {
      const mat = gateMats[i];
      if (!mat) return;
      const colour = g.passed ? COL.gatePassed
        : (race.next_gate === i ? COL.gateNext : COL.gate);
      mat.color.setHex(colour);
      mat.emissive.setHex(colour);
    });
  }

  function setPose(p) {
    Object.assign(target, p);
    if (!started) { Object.assign(shown, p); started = true; }
  }

  function setMode(next) {
    mode = next;
    controls.enabled = next === "orbit";
    if (next === "orbit") {
      controls.target.copy(toThree(shown.x, shown.y, shown.z));
      controls.update();
    }
  }

  function setTrailVisible(on) {
    showTrail = on;
    trail.line.visible = on;
    if (!on) trail.reset();
  }

  function clearTrail() {
    trail.reset();
  }

  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  function frame() {
    const dt = Math.min(clock.getDelta(), 0.1);
    // exponential chase, framerate-independent
    const k = 1 - Math.exp(-12 * dt);
    shown.x += (target.x - shown.x) * k;
    shown.y += (target.y - shown.y) * k;
    shown.z += (target.z - shown.z) * k;
    shown.pitch += (target.pitch - shown.pitch) * k;
    // Heading and roll wrap at 360, so they chase the short way round. Without
    // this a 350->10 yaw spins almost all the way back the other way, and a
    // back-flip (roll 0 -> 359 -> 0) tumbles forwards instead of backwards.
    shown.heading = chaseAngle(shown.heading, target.heading, k);
    shown.roll = chaseAngle(shown.roll, target.roll, k);

    const pos = toThree(shown.x, shown.y, shown.z);
    // A landed pose reports z=0. Keep the model above the floor instead of
    // burying half of it in the floor mesh, while leaving its trail and camera
    // target on the simulator's geometrically honest pose.
    const modelPos = pos.clone();
    modelPos.y = Math.max(modelPos.y, DRONE_LANDED_Y_M);
    drone.group.position.copy(modelPos);
    drone.group.rotation.set(deg(shown.pitch), -deg(shown.heading), deg(shown.roll), "YXZ");
    drone.setFlying(target.flying, dt);
    drone.shadow.position.set(pos.x, 0.012, pos.z);
    drone.shadow.material.opacity = Math.max(0.03, 0.34 - shown.z * 0.09);

    if (showTrail && target.flying) trail.push(pos);

    updateCamera(camera, controls, mode, pos, arenaW, arenaD, dt);
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  return { setScene, setPose, setRace, setMode, setTrailVisible, clearTrail,
           getMode: () => mode };
}

// --- camera rigs ------------------------------------------------------------

function updateCamera(camera, controls, mode, pos, w, d, dt) {
  const k = 1 - Math.exp(-6 * dt);
  if (mode === "follow") {
    camera.up.set(0, 1, 0);
    // Preserve arena context without shrinking the drone and gates to a few
    // pixels on a large stage.
    const back = Math.min(Math.max(Math.max(w, d) * 0.62, 3.0), 4.2);
    camera.position.lerp(new THREE.Vector3(pos.x, pos.y + back * 0.45, pos.z + back), k);
    camera.lookAt(pos);
  } else if (mode === "topdown") {
    // Frames the whole arena, not the drone: this is the view a student reads as
    // a map, and one that slides around under the drone is useless for that.
    // Straight down makes the default up vector (0,1,0) parallel to the view, so
    // the roll is undefined and the arena spins during the transition — point up
    // at arena north instead, which is also the orientation of the minimap.
    camera.up.set(0, 0, -1);
    const centre = new THREE.Vector3(w / 2, 0, -d / 2);
    camera.position.lerp(new THREE.Vector3(centre.x, fitHeight(camera, w, d), centre.z), k);
    camera.lookAt(centre);
  } else {
    camera.up.set(0, 1, 0);
    controls.target.lerp(pos, k);
    controls.update();
  }
}

/**
 * Height at which a `w` x `d` arena fits both FOVs.
 *
 * The camera's up vector points at arena north, so the room's depth is framed by
 * the *vertical* FOV and its width by the horizontal one — take whichever needs
 * more height, or a corridor gets cropped at both ends.
 */
function fitHeight(camera, w, d) {
  const halfV = deg(camera.fov) / 2;
  const halfH = Math.atan(Math.tan(halfV) * camera.aspect);
  return Math.max((d / 2) / Math.tan(halfV), (w / 2) / Math.tan(halfH)) * 1.15;
}

// --- arena ------------------------------------------------------------------

function disposeChildren(group) {
  group.traverse((o) => {
    o.geometry?.dispose();
    if (Array.isArray(o.material)) o.material.forEach((m) => m.dispose());
    else o.material?.dispose();
  });
  group.clear();
}

/** Builds the cage and returns one halo material per gate, for race tinting. */
/**
 * Build the room the camera is flying through.
 *
 * This used to be a stylised blue box with a palette of its own, which meant the
 * operator's 3D view and the drone's camera disagreed about what the arena
 * looked like: the camera flew through black netting, a hall full of furniture
 * and a dark varnished floor, while this drew an empty glass room. Both now read
 * the same `appearance` block from the scene message.
 *
 * What is deliberately *not* copied is the photographic side of it. The netting
 * stays see-through, the survey grid and the gate numbers stay on, and the drone
 * and its trail are still drawn as legible symbols: the job here is to show
 * where the aircraft is inside a faithful room, not to be a second camera feed.
 */
function buildArena(group, desc) {
  const w = desc.width_m ?? desc.size_m;
  const d = desc.depth_m ?? desc.size_m;
  const top = desc.height_m ?? desc.wall_height_m ?? 3.0;
  const look = desc.appearance || null;

  const gloss = look ? (look.floor.gloss ?? 0) : 0;
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(w, d),
    new THREE.MeshStandardMaterial({
      color: look ? look.floor.colour : COL.floor,
      roughness: look ? 1 - 0.7 * gloss : 0.95,
      metalness: 0.35 * gloss,
    }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(w / 2, 0, -d / 2);
  floor.receiveShadow = true;
  group.add(floor);

  // The survey grid is the simulator's own aid, not a marking on any real
  // floor, so it follows whichever floor is actually being drawn.
  if (!look || look.floor.grid) {
    const grid = buildGrid(w, d, 0.5);
    grid.position.y = 0.004;
    group.add(grid);
  } else if (look.floor.plank_m > 0) {
    const boards = buildBoards(w, d, look.floor.plank_m);
    boards.position.y = 0.004;
    group.add(boards);
  }

  if (look && look.hall) {
    buildHall(group, w, d, top, look.hall, look.crowd, look.light);
  }
  if (look && look.net) {
    buildNetting(group, w, d, top, look.net);
  } else {
    // No netting: keep the glass box, so a bare arena still reads as a room.
    const walls = new THREE.Mesh(
      new THREE.BoxGeometry(w, top, d),
      new THREE.MeshStandardMaterial({
        color: COL.wall, transparent: true, opacity: 0.07,
        side: THREE.BackSide, roughness: 1,
      }),
    );
    walls.position.set(w / 2, top / 2, -d / 2);
    group.add(walls);
  }

  // The cage's own frame, which the camera sees as dark uprights at the corners.
  const netted = Boolean(look && look.net);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.BoxGeometry(w, top, d)),
    new THREE.LineBasicMaterial({
      color: netted ? look.net.colour : COL.wall,
      transparent: true,
      opacity: netted ? 0.9 : 0.45,
    }),
  );
  edges.position.set(w / 2, top / 2, -d / 2);
  group.add(edges);

  if (desc.start) group.add(buildPad(desc.start, COL.takeoff));
  if (desc.finish) group.add(buildPad(desc.finish, COL.landing));

  const mats = [];
  (desc.gates || []).forEach((g, i) => {
    const gate = buildGate(g, i, desc.tube_m, look && look.gate, desc.ring, {
      ceilingM: top,
      stringM: desc.string_m,
      stringAngleDeg: desc.string_angle_deg,
    });
    group.add(gate.group);
    mats.push(gate.mat);
  });
  return mats;
}

/** Board seams on a plank floor, at the pitch the camera sees. */
function buildBoards(w, d, pitch) {
  const pts = [];
  for (let x = pitch; x < w; x += pitch) pts.push(x, 0, 0, x, 0, -d);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  return new THREE.LineSegments(
    geo,
    new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.25 }),
  );
}

/** GridHelper is square-only, so a rectangular room draws its own lines. */
function buildGrid(w, d, step) {
  const pts = [];
  const nx = Math.max(1, Math.round(w / step));
  const nz = Math.max(1, Math.round(d / step));
  for (let i = 0; i <= nx; i++) {
    const x = (i * w) / nx;
    pts.push(x, 0, 0, x, 0, -d);
  }
  for (let i = 0; i <= nz; i++) {
    const z = -(i * d) / nz;
    pts.push(0, 0, z, w, 0, z);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: COL.grid }));
}

/** Takeoff / landing pad on the floor. */
function buildPad([x, y], color) {
  const pad = new THREE.Mesh(
    new THREE.RingGeometry(0.18, 0.30, 24),
    new THREE.MeshBasicMaterial({
      color, transparent: true, opacity: 0.55, side: THREE.DoubleSide,
    }),
  );
  pad.rotation.x = -Math.PI / 2;
  pad.position.copy(toThree(x, y, 0.006));
  return pad;
}

/** A gate on its floor stand, numbered for pass order.
 *
 * The ring is the real Squadrone gate: a fabric band, orange top and bottom
 * with white print, black-and-white checkered either side. `diameter_m` is its
 * outer edge; the drone flies through `opening_diameter_m`, which is what is
 * scored. The band's layout arrives with the scene (`desc.ring`), and its face
 * is painted the way the simulator's camera texture is, so the two views agree.
 * Under it, grey PVC tube: a mast down to a crossbar, a leg from each end of
 * that to the floor, and a foot across the gate plane under each leg — or, when
 * the arena's mount is "hanging", two white cords up to the ceiling. The race
 * tint rides on a thin halo round the ring, leaving the face its true colours. */
function buildGate(g, index, tubeM, look, ring, hang = {}) {
  const group = new THREE.Group();
  group.position.copy(toThree(g.x, g.y, g.z));
  const yaw = deg(g.yaw_deg ?? 0);
  const rOut = (g.diameter_m ?? GATE_DIAMETER_M) / 2;
  const rIn = (g.opening_diameter_m ?? GATE_OPENING_M) / 2;
  // The tube diameter arrives with the scene, so this cannot drift out of step
  // with the rig the camera is looking at the way the old constant did.
  const t = tubeM ?? GATE_TUBE_M, e = GATE_ELBOW_M;

  const colours = {
    orange: look ? look.orange : COL.ringOrange,
    dark: look ? look.dark : COL.checkDark,
    light: look ? look.light : COL.checkLight,
    print: look ? look.print : COL.print,
  };
  const mat = new THREE.MeshStandardMaterial({
    color: COL.gate, emissive: COL.gate, emissiveIntensity: 0.75, roughness: 0.45,
  });
  const standColour = look ? look.stand : COL.stand;
  const standMat = new THREE.MeshStandardMaterial({
    color: standColour, emissive: standColour, emissiveIntensity: 0.15, roughness: 0.5,
  });
  const elbowColour = look ? look.elbow : COL.elbow;
  const elbowMat = new THREE.MeshStandardMaterial({
    color: elbowColour,
    emissive: elbowColour,
    emissiveIntensity: look ? 0.15 : 0.45,
    roughness: 0.6,
  });
  // The ring lies in the XY plane (normal +Z three). Rotate about Y so the
  // plane normal points along the gate's world yaw: (sin yaw, cos yaw) world is
  // (sin yaw, 0, -cos yaw) in three space, reached by rotation.y = PI - yaw.
  const frame = new THREE.Group();
  frame.rotation.y = Math.PI - yaw;

  // The face: painted as seen from the approach side, which looks along +Z at
  // the ring's back, so the mesh is turned to face it.
  const paint = ringTexture(colours, rIn / rOut, ring);
  const face = new THREE.Mesh(
    new THREE.RingGeometry(rIn, rOut, 96, 1),
    new THREE.MeshStandardMaterial({
      map: paint,
      emissive: 0xffffff,
      emissiveMap: paint,
      emissiveIntensity: 0.35,
      roughness: 0.8,
      side: THREE.DoubleSide,
      transparent: true,
    }),
  );
  face.rotation.y = Math.PI;
  face.castShadow = true;
  frame.add(face);
  const halo = new THREE.Mesh(
    new THREE.RingGeometry(rOut + 0.01, rOut + 0.03, 96, 1),
    mat,
  );
  frame.add(halo);

  // A cylinder along the frame's own axes: "x" lateral, "y" up, "z" the gate
  // normal (the stand's feet, which brace the rig fore and aft).
  const tube = (len, x, y, axis, radius, material, z = 0) => {
    const m = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, len, 12), material);
    if (axis === "x") m.rotation.z = Math.PI / 2;
    if (axis === "z") m.rotation.x = Math.PI / 2;
    m.position.set(x, y, z);
    m.castShadow = true;
    frame.add(m);
  };

  if (look && look.mount === "hanging") {
    // Two white cords from the band's outer edge straight up to the ceiling,
    // where the camera's renderer draws them.
    const stringColour = look.string ?? "#f8f8f8";
    const stringMat = new THREE.MeshStandardMaterial({
      color: stringColour, emissive: stringColour, emissiveIntensity: 0.3, roughness: 0.9,
    });
    const angle = deg(hang.stringAngleDeg ?? GATE_STRING_ANGLE_DEG);
    const u = rOut * Math.sin(angle), v0 = rOut * Math.cos(angle);
    const v1 = (hang.ceilingM ?? g.z + rOut + 0.5) - g.z;
    const radius = (hang.stringM ?? GATE_STRING_M) / 2;
    for (const s of [-1, 1]) tube(v1 - v0, s * u, (v0 + v1) / 2, "y", radius, stringMat);
  } else {
    // the stand, in the ring's own axes: the floor is g.z below the centre
    const cx = GATE_FOOT_M / 2 - t / 2;             // leg centrelines
    const footY = -g.z + t / 2;                     // the feet lie on the floor
    const barY = -rOut - GATE_MAST_M + t / 2;       // crossbar centreline
    tube(GATE_MAST_M, 0, -rOut - GATE_MAST_M / 2 + t / 2, "y", t / 2, standMat);
    tube(2 * cx, 0, barY, "x", t / 2, standMat);                            // crossbar
    for (const s of [-1, 1]) {
      tube(barY - footY, s * cx, (barY + footY) / 2, "y", t / 2, standMat); // leg
      tube(GATE_FOOT_M, s * cx, footY, "z", t / 2, standMat);               // foot
      tube(e, s * cx, barY, "y", t * 0.62, elbowMat);      // elbow onto the crossbar
      tube(e, s * cx, footY, "y", t * 0.62, elbowMat);     // tee onto the foot
    }
    tube(e, 0, barY, "x", t * 0.62, elbowMat);             // tee under the mast
    // the clamp gripping the bottom of the ring
    tube(GATE_CLAMP_M, 0, -rOut - GATE_CLAMP_M / 2, "y", t * 0.62, elbowMat);
  }
  group.add(frame);

  group.add(buildGateNumber(index + 1, rOut));
  return { group, mat };
}

/** The ring's face, painted on a canvas: the same layout the simulator's
 * camera texture uses (angles clockwise from 12 o'clock as seen from the
 * approach side), so what the 3D view shows is what the video shows. */
function ringTexture(colours, innerRatio, ring) {
  const half = (ring && ring.orange_half_angle_deg) ?? GATE_ORANGE_HALF_ANGLE_DEG;
  const rows = (ring && ring.checker_rows) ?? GATE_CHECKER_ROWS;
  const size = 512;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const c = size / 2, rOut = size / 2, rIn = rOut * innerRatio;
  const band = rOut - rIn;
  // canvas angles start at 3 o'clock and run clockwise on screen
  const at = (theta) => deg(theta - 90);
  const sector = (a0, a1, ra, rb, colour) => {
    ctx.beginPath();
    ctx.arc(c, c, ra, at(a0), at(a1));
    ctx.arc(c, c, rb, at(a1), at(a0), true);
    ctx.closePath();
    ctx.fillStyle = colour;
    ctx.fill();
  };
  for (const centre of [0, 180]) sector(centre - half, centre + half, rOut, rIn, colours.orange);
  const mid = (rOut + rIn) / 2;
  const n = Math.max(1, Math.round((mid * deg(180 - 2 * half)) / (band / rows)));
  for (const side of [1, -1]) {
    for (let i = 0; i < n; i++) {
      const a0 = half + ((180 - 2 * half) * i) / n;
      const a1 = half + ((180 - 2 * half) * (i + 1)) / n;
      for (let k = 0; k < rows; k++) {
        const ra = rOut - (band * k) / rows, rb = rOut - (band * (k + 1)) / rows;
        const colour = (i + k) % 2 ? colours.light : colours.dark;
        if (side > 0) sector(a0, a1, ra, rb, colour);
        else sector(-a1, -a0, ra, rb, colour);
      }
    }
  }
  // the print: the web address over the top, the name under the bottom, each
  // reading left to right with its letters under as seen from the approach
  ctx.fillStyle = colours.print;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  // `under`: along the bottom, where left to right runs anticlockwise
  const arcText = (text, centre, radius, px, under) => {
    ctx.font = `bold ${px}px system-ui, sans-serif`;
    const step = (ch) => ctx.measureText(ch).width / radius;   // radians per glyph
    const total = [...text].reduce((s, ch) => s + step(ch), 0);
    let a = deg(centre) - (under ? -1 : 1) * total / 2;
    for (const ch of text) {
      const w = step(ch);
      a += (under ? -1 : 1) * w / 2;
      ctx.save();
      ctx.translate(c + radius * Math.sin(a), c - radius * Math.cos(a));
      ctx.rotate(under ? a - Math.PI : a);
      ctx.fillText(ch, 0, 0);
      ctx.restore();
      a += (under ? -1 : 1) * w / 2;
    }
  };
  arcText("SQUADRONE.COM.AU", 0, mid, Math.round(band * 0.5), false);
  arcText("Squadrone", 180, mid + band * 0.1, Math.round(band * 0.45), true);
  const tex = new THREE.CanvasTexture(canvas);
  tex.anisotropy = 4;
  return tex;
}

/** The gate's order number as a sprite, floating above the ring. */
function buildGateNumber(n, halfHeight) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.font = "bold 44px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#e2e8f0";
  ctx.strokeStyle = "#0b1220";
  ctx.lineWidth = 6;
  ctx.strokeText(String(n), 32, 34);
  ctx.fillText(String(n), 32, 34);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: new THREE.CanvasTexture(canvas), depthTest: false, transparent: true,
  }));
  sprite.scale.set(0.36, 0.36, 1);
  sprite.position.y = halfHeight + 0.18;
  return sprite;
}

// --- drone ------------------------------------------------------------------

function buildDrone(hfovDeg, aspect) {
  const group = new THREE.Group();
  // Motors sit at +/-0.42u and each prop reaches another 0.26u, so the complete
  // rotor-to-rotor span is 1.36u. Scale from that outer span; treating `u` as
  // the span made the model wider than the gate it was supposed to fit through.
  const u = DRONE_SPAN_M / 1.36;

  const body = new THREE.Mesh(
    new THREE.BoxGeometry(u * 0.55, u * 0.22, u * 0.75),
    new THREE.MeshStandardMaterial({
      color: COL.body, emissive: 0x334155, emissiveIntensity: 0.35,
      roughness: 0.45, metalness: 0.15,
    }),
  );
  body.castShadow = true;
  group.add(body);

  const props = [];
  const armMat = new THREE.MeshStandardMaterial({ color: COL.arm, roughness: 0.45 });
  const propMat = new THREE.MeshStandardMaterial({
    color: COL.prop, transparent: true, opacity: 0.75, roughness: 0.3,
  });
  for (const [sx, sz] of [[1, 1], [-1, 1], [1, -1], [-1, -1]]) {
    const ax = sx * u * 0.42, az = sz * u * 0.42;
    const arm = new THREE.Mesh(new THREE.BoxGeometry(u * 0.09, u * 0.09, u * 0.62), armMat);
    arm.position.set(ax / 2, 0, az / 2);
    arm.rotation.y = Math.atan2(ax, az);
    arm.castShadow = true;
    group.add(arm);

    const hub = new THREE.Mesh(
      new THREE.CylinderGeometry(u * 0.07, u * 0.07, u * 0.12, 10), armMat);
    hub.position.set(ax, u * 0.08, az);
    group.add(hub);

    const prop = new THREE.Mesh(
      new THREE.CylinderGeometry(u * 0.26, u * 0.26, u * 0.02, 18), propMat);
    prop.position.set(ax, u * 0.15, az);
    prop.userData.dir = sx * sz;       // counter-rotating pairs, like the real thing
    group.add(prop);
    props.push(prop);
  }

  // nose light: the only way to read heading at a glance in top-down view
  const nose = new THREE.Mesh(
    new THREE.SphereGeometry(u * 0.07, 12, 12),
    new THREE.MeshBasicMaterial({ color: 0x4ade80 }),
  );
  nose.position.set(0, 0, -u * 0.42);
  group.add(nose);

  group.add(buildFovCone(hfovDeg, aspect));

  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(u * 0.6, 24),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.3 }),
  );
  shadow.rotation.x = -Math.PI / 2;

  let spin = 0;
  function setFlying(flying, dt) {
    spin += (flying ? PROP_SPIN : 0) * dt;
    for (const p of props) p.rotation.y = spin * p.userData.dir;
  }
  return { group, shadow, setFlying };
}

/** Wireframe of what the onboard camera can see — the link between the two views. */
function buildFovCone(hfovDeg, aspect) {
  const len = 1.2;
  const halfW = len * Math.tan(deg(hfovDeg) / 2);
  const halfH = halfW / aspect;
  const tip = new THREE.Vector3(0, 0, 0);
  const far = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(
    ([sx, sy]) => new THREE.Vector3(sx * halfW, sy * halfH, -len));
  const pts = [];
  for (const c of far) pts.push(tip.clone(), c.clone());
  for (let i = 0; i < 4; i++) pts.push(far[i].clone(), far[(i + 1) % 4].clone());
  const lines = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: COL.fov, transparent: true, opacity: 0.32 }),
  );
  lines.position.z = -DRONE_SPAN_M * 0.38;
  return lines;
}

// --- flight trail -----------------------------------------------------------

function buildTrail() {
  const positions = new Float32Array(TRAIL_MAX * 3);
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geom.setDrawRange(0, 0);
  const line = new THREE.Line(geom, new THREE.LineBasicMaterial({
    color: COL.trail, transparent: true, opacity: 0.7,
  }));
  line.frustumCulled = false;
  let count = 0;
  const last = new THREE.Vector3(Infinity, Infinity, Infinity);

  return {
    line,
    push(p) {
      if (p.distanceTo(last) < 0.02) return;
      last.copy(p);
      if (count === TRAIL_MAX) {           // drop the oldest half, keep it cheap
        positions.copyWithin(0, TRAIL_MAX * 3 / 2);
        count = TRAIL_MAX / 2;
      }
      positions.set([p.x, p.y, p.z], count * 3);
      count += 1;
      geom.setDrawRange(0, count);
      geom.attributes.position.needsUpdate = true;
      geom.computeBoundingSphere();
    },
    reset() {
      count = 0;
      last.set(Infinity, Infinity, Infinity);
      geom.setDrawRange(0, 0);
    },
  };
}
// The wall maths lives in arena_layout.js so a node test can check it against
// the simulator's own placement; see the note there about why it is a plain
// script rather than a module.
const LAYOUT = (typeof window !== "undefined" && window.COMP2_ARENA_LAYOUT) || null;
const cageWalls = (w, d) => LAYOUT.cageWalls(w, d);
const wallFacing = (wall) => LAYOUT.wallFacing(wall);
/** World point on a wall, already converted into three.js space. */
const onWall = (wall, u, v, outward = 0) =>
  toThree(...LAYOUT.wallPoint(wall, u, v, outward));

/**
 * The netting, as a repeating canvas texture rather than thousands of lines.
 *
 * The camera sees a mesh whose cells are near the sensor's resolution limit;
 * drawing that as geometry here would cost more than the rest of the view put
 * together and read no better. A texture gives the same impression, stays
 * legible from the orbit camera, and — crucially — keeps the cage see-through,
 * which is the whole point of a third-person view.
 */
function netTexture(colour, opacity) {
  const cell = 16;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = cell * 8;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = colour;
  ctx.globalAlpha = Math.min(1, opacity * 2.2);
  ctx.lineWidth = 1.5;
  for (let i = 0; i <= 8; i++) {
    const p = i * cell + 0.5;
    ctx.beginPath();
    ctx.moveTo(p, 0); ctx.lineTo(p, canvas.height);
    ctx.moveTo(0, p); ctx.lineTo(canvas.width, p);
    ctx.stroke();
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  return tex;
}

/** Black mesh over each cage side, at the real cell pitch. */
function buildNetting(group, w, d, top, net) {
  const walls = cageWalls(w, d);
  for (const wall of Object.values(walls)) {
    const tex = netTexture(net.colour, net.opacity);
    // One texture tile per eight real cells, so the mesh is the right density
    // at any cage size instead of being stretched to fit.
    tex.repeat.set(wall.span / (net.pitch_m * 8), top / (net.pitch_m * 8));
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(wall.span, top),
      new THREE.MeshBasicMaterial({
        map: tex, transparent: true, side: THREE.DoubleSide, depthWrite: false,
        opacity: 0.9,
      }),
    );
    const centre = onWall(wall, wall.span / 2, top / 2);
    mesh.position.copy(centre);
    mesh.rotation.y = wallFacing(wall);
    group.add(mesh);
  }
}

/** The hall the cage stands in: its far walls, its floor, and its furniture. */
function buildHall(group, w, d, top, hall, crowd, light) {
  const walls = cageWalls(w, d);
  const out = hall.depth_m;
  const wallMat = new THREE.MeshStandardMaterial({
    color: hall.wall_colour, roughness: 0.95, side: THREE.DoubleSide,
  });
  const floorMat = new THREE.MeshStandardMaterial({
    color: hall.floor_colour, roughness: 0.9, side: THREE.DoubleSide,
  });
  for (const [name, wall] of Object.entries(walls)) {
    const pad = out + 1.0;
    const far = new THREE.Mesh(
      new THREE.PlaneGeometry(wall.span + pad * 2, top + 1.5), wallMat,
    );
    far.position.copy(onWall(wall, wall.span / 2, (top + 1.5) / 2, out));
    far.rotation.y = wallFacing(wall);
    group.add(far);

    // The hall's own floor, from the cage wall out to its wall. Laid flat by
    // the X rotation, so unlike the upright planes it cannot also be turned
    // about Y — a north/south wall runs along world x, an east/west one along
    // world y, and the two need their dimensions the other way round.
    const alongX = wall.u[0] !== 0;
    const strip = new THREE.Mesh(
      alongX
        ? new THREE.PlaneGeometry(wall.span + pad * 2, out)
        : new THREE.PlaneGeometry(out, wall.span + pad * 2),
      floorMat,
    );
    strip.position.copy(onWall(wall, wall.span / 2, 0, out / 2));
    strip.rotation.x = -Math.PI / 2;
    group.add(strip);

    for (const prop of hall.props || []) buildProp(group, wall, prop, out);
    if (light && light.wall === name) buildLight(group, wall, light, out);
  }
  for (const person of crowd || []) {
    const wall = walls[person.wall];
    if (wall) buildPerson(group, wall, person, out);
  }
}

/** One object against the hall wall — a doorway, a board, a table, a chair. */
function buildProp(group, wall, prop, out) {
  const depth = out - 0.04;
  const mat = new THREE.MeshStandardMaterial({
    color: prop.colour, roughness: 0.85, side: THREE.DoubleSide,
  });
  const slab = (u0, u1, v0, v1, material = mat) => {
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(Math.abs(u1 - u0), Math.abs(v1 - v0)), material,
    );
    mesh.position.copy(onWall(wall, (u0 + u1) / 2, (v0 + v1) / 2, depth));
    mesh.rotation.y = wallFacing(wall);
    group.add(mesh);
  };
  const { u, v, w: pw, h: ph, kind } = prop;
  if (kind === "doorway" || kind === "board" || kind === "monitor") {
    slab(u, u + pw, v, v + ph);
  } else if (kind === "table" || kind === "bench") {
    slab(u, u + pw, v + ph - 0.05, v + ph);
    const legs = new THREE.MeshStandardMaterial({ color: "#5a5c60", roughness: 0.9 });
    slab(u + 0.05, u + 0.11, v, v + ph - 0.05, legs);
    slab(u + pw - 0.12, u + pw - 0.06, v, v + ph - 0.05, legs);
  } else if (kind === "chair") {
    slab(u, u + pw, v + 0.42, v + ph);
  } else {
    slab(u, u + pw, v, v + ph);
  }
}

/** A spectator outside the net: legs, torso and head, as the camera draws them. */
function buildPerson(group, wall, person, out) {
  const depth = out - person.standoff;
  const parts = LAYOUT.personParts(person.stature);
  const part = ([halfWidth, v0, v1], colour) => {
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(halfWidth * 2, v1 - v0),
      new THREE.MeshStandardMaterial({
        color: colour, roughness: 0.9, side: THREE.DoubleSide,
      }),
    );
    mesh.position.copy(onWall(wall, person.u, (v0 + v1) / 2, depth));
    mesh.rotation.y = wallFacing(wall);
    group.add(mesh);
  };
  part(parts.legs, person.trousers);
  part(parts.torso, person.shirt);
  part(parts.head, person.skin);
}

/**
 * The bright opening outside the net.
 *
 * In the footage this is the brightest thing in shot and what the camera's
 * exposure chases, so it is drawn as an actual light as well as a glowing
 * panel — the gates nearest it pick up the same bias the camera sees.
 */
function buildLight(group, wall, light, out) {
  const panel = new THREE.Mesh(
    new THREE.PlaneGeometry(light.w, light.h),
    new THREE.MeshBasicMaterial({ color: light.colour, side: THREE.DoubleSide }),
  );
  panel.position.copy(onWall(wall, light.u + light.w / 2, light.v + light.h / 2, out - 0.05));
  panel.rotation.y = wallFacing(wall);
  group.add(panel);

  const lamp = new THREE.PointLight(light.colour, 2.2 * (light.intensity ?? 1), 14, 2);
  lamp.position.copy(
    onWall(wall, light.u + light.w / 2, light.v + light.h / 2, out - 0.6),
  );
  group.add(lamp);
}
