/**
 * Wires the 3D stage (scene3d.js) to the WebSocket feed and its toolbar.
 *
 * Kept apart from scene3d.js so the renderer stays a pure "given a scene and a
 * pose, draw it" module with no knowledge of the protocol or the DOM around it.
 */
import { createStage } from "./scene3d.js?v=20261020-1";

const container = document.getElementById("view3d");
const empty = document.getElementById("stage-empty");
const stage = createStage(container);

const tools = document.getElementById("view-tools");

window.COMP2_BUS.on((msg) => {
  if (msg.type === "scene") {
    stage.setScene(msg.scene);
    const has = Boolean(msg.scene);
    empty.hidden = has;
    tools.hidden = !has;          // no arena, nothing for the camera modes to do
  } else if (msg.type === "pose") {
    stage.setPose(msg);
  } else if (msg.type === "race") {
    stage.setRace(msg);
  } else if (msg.type === "reset") {
    // drop the old flight path rather than leaving the last attempt's line
    // hanging over the new one
    stage.clearTrail();
  }
});

// --- camera-mode toolbar ----------------------------------------------------

const modeButtons = [...document.querySelectorAll("#view-tools [data-mode]")];
for (const btn of modeButtons) {
  btn.onclick = () => {
    stage.setMode(btn.dataset.mode);
    modeButtons.forEach((b) => b.classList.toggle("on", b === btn));
  };
}

const trailBtn = document.getElementById("trail");
trailBtn.onclick = () => {
  const on = !trailBtn.classList.contains("on");
  trailBtn.classList.toggle("on", on);
  stage.setTrailVisible(on);
};
