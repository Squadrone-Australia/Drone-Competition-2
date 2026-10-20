// The browser's 3D view places the hall, its furniture and the spectators
// against the cage walls. The simulator's renderer places the very same things
// against its own copy of that wall table, and the two must agree: if they
// drift, the operator's 3D view shows a different room from the one the camera
// is flying through, which is exactly the bug this whole module exists to stop.
//
// tests/fixtures/wall_points.json is generated from comp2/sim/render.py, and
// tests/test_arena_view.py re-derives it, so neither side can move alone.
//
// Run with: node --test tests/js/*.test.js
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const LAYOUT = require("../../comp2/frontend/arena_layout.js");
const fixture = JSON.parse(
  fs.readFileSync(path.join(__dirname, "../fixtures/wall_points.json"), "utf8"),
);

test("wall points match the simulator's own placement", () => {
  const { width_m, depth_m } = fixture.arena;
  const walls = LAYOUT.cageWalls(width_m, depth_m);
  for (const c of fixture.cases) {
    const wall = walls[c.wall];
    assert.ok(wall, `no such wall: ${c.wall}`);
    assert.strictEqual(wall.span, c.span, `${c.wall} span`);
    const got = LAYOUT.wallPoint(wall, c.u, c.v, c.out);
    for (let i = 0; i < 3; i++) {
      assert.ok(
        Math.abs(got[i] - c.point[i]) < 1e-9,
        `${c.wall} u=${c.u} v=${c.v} out=${c.out}: axis ${i} ` +
          `expected ${c.point[i]}, got ${got[i]}`,
      );
    }
  }
});

test("every wall's outward normal points out of the cage", () => {
  const w = 6, d = 4;
  const walls = LAYOUT.cageWalls(w, d);
  const centre = [w / 2, d / 2];
  for (const [name, wall] of Object.entries(walls)) {
    // A point pushed outward must end up further from the cage centre than the
    // wall itself. A sign slip here once put the whole hall inside the cage.
    const on = LAYOUT.wallPoint(wall, wall.span / 2, 1);
    const out = LAYOUT.wallPoint(wall, wall.span / 2, 1, 2);
    const near = Math.hypot(on[0] - centre[0], on[1] - centre[1]);
    const far = Math.hypot(out[0] - centre[0], out[1] - centre[1]);
    assert.ok(far > near, `${name}: outward offset moved inwards`);
  }
});

test("wall facing lays a plane flat against each side", () => {
  const walls = LAYOUT.cageWalls(6, 4);
  // A plane rotated by wallFacing has normal (sin f, 0, cos f) in three space,
  // which is (sin f, -cos f) in world xy. That must be the inward normal, -out.
  for (const [name, wall] of Object.entries(walls)) {
    const f = LAYOUT.wallFacing(wall);
    const nx = Math.sin(f);
    const ny = -Math.cos(f);
    assert.ok(
      Math.abs(nx - -wall.out[0]) < 1e-9 && Math.abs(ny - -wall.out[1]) < 1e-9,
      `${name}: facing ${f} does not point into the cage`,
    );
  }
});

test("a spectator's parts stack up without gaps", () => {
  const parts = LAYOUT.personParts(1.75);
  assert.strictEqual(parts.legs[1], 0);
  assert.strictEqual(parts.legs[2], parts.torso[1], "torso must sit on the legs");
  assert.strictEqual(parts.torso[2], parts.head[1], "head must sit on the torso");
  assert.ok(parts.head[2] > 1.6 && parts.head[2] < 1.9, "roughly person-sized");
  // Taller people are taller all the way up, not just at the head.
  const tall = LAYOUT.personParts(1.95);
  assert.ok(tall.torso[1] > parts.torso[1] && tall.head[2] > parts.head[2]);
});
