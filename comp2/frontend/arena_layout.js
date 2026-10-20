/**
 * Where the cage's sides are, and how things hang off them.
 *
 * Pure geometry in the published competition frame — no three.js, no DOM — so
 * the 3D view and a node test can both use it. That matters because this is the
 * half that is easy to get wrong and impossible to eyeball: the simulator's
 * renderer places the hall, its furniture and the spectators against the same
 * four walls, and if these two disagree the operator's 3D view quietly shows a
 * different room from the one the camera is flying through.
 *
 * It mirrors `_walls` and `_at` in comp2/sim/render.py. `u` runs along a wall,
 * `v` is up it, and `out` points out of the cage, all in metres.
 *
 * Loaded as a classic browser script and as a CommonJS module from the tests.
 */

/** The four sides of a width x depth cage, looking from inside. */
function cageWalls(width, depth) {
  return {
    north: { origin: [0, depth, 0], u: [1, 0, 0], out: [0, 1, 0], span: width },
    south: { origin: [width, 0, 0], u: [-1, 0, 0], out: [0, -1, 0], span: width },
    east: { origin: [width, depth, 0], u: [0, -1, 0], out: [1, 0, 0], span: depth },
    west: { origin: [0, 0, 0], u: [0, 1, 0], out: [-1, 0, 0], span: depth },
  };
}

/** World point `u` along a wall, `v` up it, `outward` metres outside the cage. */
function wallPoint(wall, u, v, outward = 0) {
  return [
    wall.origin[0] + wall.u[0] * u + wall.out[0] * outward,
    wall.origin[1] + wall.u[1] * u + wall.out[1] * outward,
    wall.origin[2] + v,
  ];
}

/**
 * Rotation about three.js Y that lays a plane flat against a wall, facing in.
 *
 * A plane starts in the XY plane facing +Z three, which is -y world. The cage's
 * inward normal is -out, so this is the angle from -y world to that.
 */
function wallFacing(wall) {
  return Math.atan2(-wall.out[0], wall.out[1]);
}

/**
 * A spectator's three slabs, as [halfWidth, v0, v1] in metres.
 *
 * Same proportions the renderer draws, so a person is the same size and shape
 * in both views.
 */
function personParts(stature) {
  const shoulder = (0.24 * stature) / 1.75;
  const hip = 0.44 * stature;
  const neck = 0.84 * stature;
  return {
    legs: [shoulder * 0.8, 0, hip],
    torso: [shoulder, hip, neck],
    head: [0.09, neck, neck + 0.22],
  };
}

// A plain script, not an ES module, so the node tests can require() it.
// scene3d.js is a module and picks it up from the global, which is set before any module runs because classic scripts are
// not deferred.
const ARENA_LAYOUT = { cageWalls, wallPoint, wallFacing, personParts };

if (typeof window !== "undefined") window.COMP2_ARENA_LAYOUT = ARENA_LAYOUT;
if (typeof module !== "undefined") module.exports = ARENA_LAYOUT;
