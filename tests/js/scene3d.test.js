// Smoke-runs scene3d.js's arena builder against a real scene payload.
//
// There is no browser in the test environment and the repo takes no npm runtime
// dependency, so this loads the module's source with three.js and the DOM
// replaced by the smallest stubs that let the code actually execute. That is
// worth doing: the 3D view is otherwise only checked by looking at it, and the
// failure mode being guarded here — a payload field the builder reads under a
// different name, so the arena silently falls back to the old blue room — does
// not throw anywhere a human would notice.
//
// Run with: node --test tests/js/*.test.js
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.join(__dirname, "../../comp2/frontend");

// --- the smallest three.js that scene3d.js can run against -----------------

const made = [];

class Obj3D {
  constructor(kind, geometry, material) {
    this.kind = kind;
    this.geometry = geometry;
    this.material = material;
    this.children = [];
    this.position = {
      x: 0, y: 0, z: 0,
      set(x, y, z) { this.x = x; this.y = y; this.z = z; },
      copy(v) { this.x = v.x; this.y = v.y; this.z = v.z; return this; },
    };
    this.rotation = { x: 0, y: 0, z: 0, set(x, y, z) { Object.assign(this, { x, y, z }); } };
    this.scale = { set() {} };
    made.push(this);
  }
  add(...kids) { this.children.push(...kids); }
  copy(v) { return Object.assign(this, v); }
}

const geom = (kind) => class { constructor(...args) { this.kind = kind; this.args = args; } };
const mat = (kind) => class { constructor(opts = {}) { Object.assign(this, opts); this.kind = kind; } };

const THREE = {
  Group: class extends Obj3D { constructor() { super("Group"); } },
  Mesh: class extends Obj3D { constructor(g, m) { super("Mesh", g, m); } },
  LineSegments: class extends Obj3D { constructor(g, m) { super("LineSegments", g, m); } },
  Sprite: class extends Obj3D { constructor(m) { super("Sprite", null, m); } },
  PointLight: class extends Obj3D {
    constructor(colour, intensity) { super("PointLight"); this.color = colour; this.intensity = intensity; }
  },
  Vector3: class { constructor(x = 0, y = 0, z = 0) { Object.assign(this, { x, y, z }); } },
  PlaneGeometry: geom("Plane"), BoxGeometry: geom("Box"),
  CylinderGeometry: geom("Cylinder"), RingGeometry: geom("Ring"),
  EdgesGeometry: geom("Edges"),
  BufferGeometry: class { setAttribute() {} },
  Float32BufferAttribute: class { constructor(a) { this.array = a; } },
  MeshStandardMaterial: mat("standard"), MeshBasicMaterial: mat("basic"),
  LineBasicMaterial: mat("line"), SpriteMaterial: mat("sprite"),
  CanvasTexture: class { constructor() { this.repeat = { set() {} }; } },
  Texture: class { constructor() { this.repeat = { set() {} }; } },
  RepeatWrapping: 1000, DoubleSide: 2, BackSide: 1, FrontSide: 0,
};

const canvasStub = () => ({
  width: 0, height: 0,
  getContext: () => new Proxy({}, {
    get: (_t, prop) =>
      prop === "measureText" ? () => ({ width: 10 }) : () => undefined,
  }),
});

/** Load scene3d.js with its imports and globals replaced by the stubs above. */
function loadScene3d() {
  let src = fs.readFileSync(path.join(FRONTEND, "scene3d.js"), "utf8");
  src = src
    .replace(/^import [\s\S]*?;$/gm, "")
    .replace(/^export /gm, "");   // the module keywords are meaningless in a vm
  const layout = require(path.join(FRONTEND, "arena_layout.js"));
  const sandbox = {
    THREE,
    OrbitControls: class { constructor() { this.target = { set() {} }; } },
    window: { COMP2_ARENA_LAYOUT: layout },
    document: { createElement: canvasStub },
  };
  const vm = require("node:vm");
  const context = vm.createContext(sandbox);
  vm.runInContext(`${src}\n; this.buildArena = buildArena;`, context);
  return sandbox.buildArena;
}

/** Netting is a textured plane mesh; gate-number sprites carry a map too. */
const isNetting = (o) =>
  o.kind === "Mesh" &&
  o.geometry && o.geometry.kind === "Plane" &&
  o.material && o.material.map;

const cagePayload = JSON.parse(
  fs.readFileSync(path.join(__dirname, "../fixtures/scene_cage.json"), "utf8"),
);

// --- the tests -------------------------------------------------------------

test("the cage arena builds without throwing", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  const group = new THREE.Group();
  const mats = buildArena(group, cagePayload);
  assert.strictEqual(mats.length, cagePayload.gates.length, "one material per gate");
  assert.ok(made.length > 50, `only ${made.length} objects built`);
});

test("the floor is the colour the camera sees, not the old blue", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  buildArena(new THREE.Group(), cagePayload);
  const wanted = cagePayload.appearance.floor.colour;
  const floors = made.filter((o) => o.material && o.material.color === wanted);
  assert.ok(floors.length > 0, `no surface used the sent floor colour ${wanted}`);
});

test("netting and the hall beyond it are actually built", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  buildArena(new THREE.Group(), cagePayload);
  const netColour = cagePayload.appearance.net.colour;
  const nets = made.filter(isNetting);
  assert.strictEqual(nets.length, 4, "netting must cover all four cage sides");
  const hallColour = cagePayload.appearance.hall.wall_colour;
  assert.ok(
    made.some((o) => o.material && o.material.color === hallColour),
    "the hall wall was not drawn",
  );
  assert.ok(
    made.some((o) => o.kind === "PointLight"),
    "the bright doorway did not become a light",
  );
  assert.ok(netColour, "fixture has no net colour");
});

test("a clean arena keeps the glass room instead of a hall", () => {
  const buildArena = loadScene3d();
  const clean = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../fixtures/scene_clean.json"), "utf8"),
  );
  made.length = 0;
  buildArena(new THREE.Group(), clean);
  assert.ok(!made.some(isNetting), "a clean arena must not be netted");
  assert.ok(
    made.some((o) => o.material && o.material.side === THREE.BackSide),
    "the glass box is missing",
  );
  assert.ok(!made.some((o) => o.kind === "PointLight"), "no hall, no doorway light");
});

test("the gate is built from the sent tube diameter", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  buildArena(new THREE.Group(), cagePayload);
  const radii = made
    .filter((o) => o.geometry && o.geometry.kind === "Cylinder")
    .map((o) => o.geometry.args[0]);
  // The stand's tube is drawn at half the sent diameter; the white fittings
  // are a little fatter, so that value must be present exactly.
  assert.ok(
    radii.some((r) => Math.abs(r - cagePayload.tube_m / 2) < 1e-12),
    `no tube drawn at the sent radius ${cagePayload.tube_m / 2}`,
  );
});

test("each gate is a ring of the sent diameters, painted on a canvas", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  buildArena(new THREE.Group(), cagePayload);
  const g = cagePayload.gates[0];
  const faces = made.filter(
    (o) => o.geometry && o.geometry.kind === "Ring" && o.material && o.material.map,
  );
  assert.strictEqual(faces.length, cagePayload.gates.length, "one face per gate");
  const [inner, outer] = faces[0].geometry.args;
  assert.ok(Math.abs(inner - g.opening_diameter_m / 2) < 1e-12, `inner radius ${inner}`);
  assert.ok(Math.abs(outer - g.diameter_m / 2) < 1e-12, `outer radius ${outer}`);
});

test("payloads from an older server still build", () => {
  // scene messages predating the appearance block must not break the view.
  const buildArena = loadScene3d();
  const legacy = { ...cagePayload };
  delete legacy.appearance;
  delete legacy.tube_m;
  delete legacy.ring;
  made.length = 0;
  assert.doesNotThrow(() => buildArena(new THREE.Group(), legacy));
  assert.ok(made.length > 20);
});

test("a hanging arena draws two cords per gate and no stand", () => {
  const buildArena = loadScene3d();
  const hung = JSON.parse(JSON.stringify(cagePayload));
  hung.appearance.gate.mount = "hanging";
  made.length = 0;
  buildArena(new THREE.Group(), hung);
  const cylinders = made.filter((o) => o.geometry && o.geometry.kind === "Cylinder");
  const cords = cylinders.filter(
    (o) => Math.abs(o.geometry.args[0] - hung.string_m / 2) < 1e-12,
  );
  assert.strictEqual(cords.length, 2 * hung.gates.length, "two cords per gate");
  // the stand's tubes (and fittings) are all fatter than a cord
  assert.ok(
    cylinders.every((o) => o.geometry.args[0] <= hung.string_m / 2 + 1e-12),
    "a stand was built for a hanging gate",
  );
  // a cord reaches the ceiling: its top is the arena height above the floor
  const g = hung.gates[0];
  const cord = cords[0];
  const length = cord.geometry.args[2];
  const r = g.diameter_m / 2;
  const v0 = r * Math.cos((hung.string_angle_deg * Math.PI) / 180);
  assert.ok(Math.abs(length - (hung.height_m - g.z - v0)) < 1e-9, `cord length ${length}`);
});

test("a standing arena builds no cords", () => {
  const buildArena = loadScene3d();
  made.length = 0;
  buildArena(new THREE.Group(), cagePayload);
  const cords = made.filter(
    (o) =>
      o.geometry && o.geometry.kind === "Cylinder" &&
      Math.abs(o.geometry.args[0] - cagePayload.string_m / 2) < 1e-12,
  );
  assert.strictEqual(cords.length, 0);
});
