# PyInstaller spec — build with: pyinstaller comp2.spec --noconfirm
a = Analysis(
    ["comp2/launcher.py"],
    datas=[("comp2/frontend", "comp2/frontend"),
           ("comp2/tracks", "comp2/tracks"),
           # installable into a contestant's own IDE interpreter
           ("client", "client"),
           # team starter programs, run from the contestant's IDE
           ("examples", "examples")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto",
                   "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
                   "uvicorn.lifespan.on"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, name="comp2", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="comp2")
