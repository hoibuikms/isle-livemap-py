# PyInstaller spec: pyinstaller IsleLiveMap.spec  (one-folder, windowed)
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"]
    + collect_submodules("scapy.layers")
    + collect_submodules("scapy.arch")
    + ["scapy.sendrecv", "scapy.config", "scapy.layers.inet"]
    + collect_submodules("isle_livemap")
)

a = Analysis(
    ["run_isle_livemap.py"],
    pathex=["."],
    datas=[("isle_livemap/assets", "isle_livemap/assets")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IsleLiveMap",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="IsleLiveMap", upx=False)
