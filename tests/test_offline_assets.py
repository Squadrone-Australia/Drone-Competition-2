import re
from pathlib import Path

FRONTEND = Path(__file__).parent.parent / "comp2" / "frontend"


def test_no_external_urls_in_frontend():
    pattern = re.compile(rb"https?://(?!localhost)")
    offenders = []
    for f in FRONTEND.rglob("*"):
        if f.suffix in {".html", ".css", ".js"} and "vendor" not in f.parts:
            if pattern.search(f.read_bytes()):
                offenders.append(str(f))
    assert not offenders, f"external URLs found (breaks offline use): {offenders}"


def test_three_js_is_vendored():
    # three.module.min.js imports three.core.min.js relatively; OrbitControls
    # resolves through the import map in index.html. All three must be present or
    # the 3D view silently fails to load with no network to fall back on.
    for name in ("three.module.min.js", "three.core.min.js", "OrbitControls.js"):
        assert (FRONTEND / "vendor" / name).stat().st_size > 10_000, name


def test_the_import_map_points_at_the_vendored_three():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert '"three": "./vendor/three.module.min.js"' in html


def test_frontend_loads_only_the_cockpit_scripts():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'src="app.js?v=' in html
    assert 'src="plan.js?v=' in html
    assert 'src="view3d.js?v=' in html
    assert "blockly" not in html.lower()
    assert not (FRONTEND / "vendor" / "blockly.min.js").exists()


def test_frontend_offers_a_reversible_real_tello_switch():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'id="drone-mode"' in html
    assert 'id="use-tello"' in html
    assert '"Use Simulator"' in app_js
    assert 'useSimulator ? "sim" : "tello"' in app_js
    assert "window.confirm" in app_js


def test_frontend_reports_and_interlocks_the_external_ide_client():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'id="python-client"' in html
    assert 'msg.type === "python_client"' in app_js
    assert "pythonClientConnected" in app_js


def test_frontend_is_a_fixed_cockpit_without_browser_coding():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    for panel in (
        "stage",
        "camera-panel",
        "track-panel",
        "race-panel",
        "console-panel",
    ):
        assert f'id="{panel}"' in html
    for removed in (
        "coding",
        "pane-python",
        "pane-blocks",
        "ed-input",
        "debug-panel",
        "run",
    ):
        assert f'id="{removed}"' not in html
    assert 'id="stop"' in html and 'id="estop"' in html
    assert 'id="visual-deck"' in html and 'id="instrument-deck"' in html
    assert "#visual-deck" in css and "#instrument-deck" in css
    assert '"run_python"' not in app_js and 'type: "run"' not in app_js


def test_drone_camera_is_permanent_and_not_a_floating_window():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    view_js = (FRONTEND / "view3d.js").read_text(encoding="utf-8")
    assert 'id="camera-panel"' in html and 'id="camera-state"' in html
    assert 'id="cam-dock"' not in html
    assert "camera-window" not in css
    assert "pointerdown" not in view_js


def test_legacy_script_output_reaches_the_system_log():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app_js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'id="console-panel"' in html and 'id="console"' in html
    assert 'msg.type === "output"' in app_js
    assert "ERROR  " in app_js


def test_the_stylesheet_keeps_its_colours_in_the_token_block():
    """Surfaces and accents are named once, in :root, and used by name.

    The only raw hex allowed past that point is the syntax-highlighting palette
    and a couple of true blacks — everything a repaint would want to change has
    to be reachable from one block of variables.
    """
    css = (FRONTEND / "style.css").read_text(encoding="utf-8")
    for token in ("--hoop", "--live", "--abort", "--ink", "--dim", "--rule"):
        assert token in css, token
    assert "prefers-reduced-motion" in css
    tokens = css[css.index(":root") : css.index("* { box-sizing")]
    loose = re.findall(r"#[0-9a-fA-F]{3,8}\b", css.replace(tokens, ""))
    assert len(loose) <= 20, (
        f"raw hex leaking past the token block: {sorted(set(loose))}"
    )


def test_packaged_app_carries_the_external_ide_client():
    spec = (FRONTEND.parent.parent / "comp2.spec").read_text(encoding="utf-8")
    assert '("client", "client")' in spec
