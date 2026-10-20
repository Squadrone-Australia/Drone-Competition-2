/**
 * The arena panel: what the room around the gates looks like.
 *
 * Unlike the track picker, this one writes. A track has to match the physical
 * cage, so organisers edit those files; but the venue's floor, netting, lighting
 * and the people standing outside it are exactly what a pilot should be able to
 * vary, to find out whether a detector works or has merely been fitted to one
 * picture of one room. Randomise draws one entry from every menu at once.
 *
 * The server owns the menus: this renders whatever `arena` message it is sent
 * and never hardcodes a list of its own.
 */
(function () {
  const panel = document.getElementById("arena-panel");
  const controls = document.getElementById("arena-controls");
  const intensity = document.getElementById("arena-light-intensity");
  const intensityValue = document.getElementById("arena-light-value");
  const randomButton = document.getElementById("arena-random");
  const preset = document.getElementById("arena-preset");
  const note = document.getElementById("arena-note");
  const send = window.COMP2_SEND;

  // Friendlier than the wire names, which are chosen for the Python side.
  const LABELS = {
    floor: "Floor",
    net: "Netting",
    hall: "Beyond the net",
    crowd: "Spectators",
    light: "Light source",
    imaging: "Camera",
    gate: "Gate finish",
    mount: "Gate mounting",
  };
  const ORDER = ["floor", "net", "hall", "crowd", "light", "imaging", "gate", "mount"];

  const pickers = {};
  let running = false;
  let built = false;

  function humanise(name) {
    return name.replace(/_/g, " ");
  }

  function build(catalog) {
    if (built) return;
    built = true;
    for (const key of ORDER) {
      const options = catalog.libraries[key];
      if (!options) continue;
      const row = document.createElement("label");
      row.className = "arena-row";
      row.textContent = LABELS[key] || key;
      const select = document.createElement("select");
      select.id = `arena-${key}`;
      for (const name of options) {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = humanise(name);
        select.appendChild(opt);
      }
      select.onchange = apply;
      row.appendChild(select);
      controls.appendChild(row);
      pickers[key] = select;
    }
    for (const name of catalog.presets || []) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = humanise(name);
      preset.appendChild(opt);
    }
    panel.hidden = false;
  }

  function apply() {
    const parts = {};
    for (const [key, select] of Object.entries(pickers)) parts[key] = select.value;
    parts.light_intensity = Number(intensity.value);
    send({ type: "arena", parts });
  }

  function setDisabled(state) {
    // Swapping the scenery mid-flight would move the world under a running
    // race, exactly as a track change would, so the server refuses it and the
    // controls say so rather than letting the click fail.
    for (const select of Object.values(pickers)) select.disabled = state;
    preset.disabled = state;
    intensity.disabled = state;
    randomButton.disabled = state;
    note.textContent = state
      ? "Stop the run to change the arena."
      : "Mix and match, or randomise, to see what a detector really survives.";
  }

  window.COMP2_BUS.on((msg) => {
    if (msg.type === "arena") {
      build(msg.catalog);
      if (msg.current) {
        preset.value = msg.current.name || "";
        // The menus are sorted, so "hanging" would otherwise sit selected over
        // a stand-mounted arena.
        if (msg.current.mount && pickers.mount) pickers.mount.value = msg.current.mount;
        if (msg.current.light_intensity != null) {
          intensity.value = msg.current.light_intensity;
          intensityValue.textContent = Number(msg.current.light_intensity).toFixed(2);
        }
      }
    } else if (msg.type === "running") {
      running = msg.running;
      setDisabled(running);
    }
  });

  intensity.oninput = () => {
    intensityValue.textContent = Number(intensity.value).toFixed(2);
  };
  intensity.onchange = apply;
  preset.onchange = () => {
    if (preset.value) send({ type: "arena", name: preset.value });
  };
  randomButton.onclick = () => send({ type: "arena", randomize: true });
  setDisabled(running);
})();
