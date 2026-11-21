# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct, VSVersionInfo,
)
from pathlib import Path
from runpy import run_path

metadata = run_path(str(Path(SPECPATH) / 'rpytranslator' / '__init__.py'))
release = metadata['__version__']
version_numbers = tuple(map(int, release.split('.'))) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_numbers, prodvers=version_numbers),
    kids=[StringFileInfo([StringTable('080404b0', [
        StringStruct('ProductName', metadata['APP_NAME']),
        StringStruct('FileDescription', metadata['APP_NAME']),
        StringStruct('FileVersion', release),
        StringStruct('ProductVersion', release),
    ])]), VarFileInfo([VarStruct('Translation', [0x0804, 1200])])],
)

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('win32more')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# unrpyc imports this standard-library module dynamically from its bundled
# source tree; include it explicitly for one-file builds.
hiddenimports += ['pickletools']

# unrpyc is loaded dynamically from a filesystem path at runtime, so PyInstaller
# cannot discover it as a normal import. Include its source tree explicitly;
# datas entries use the (source, destination-directory) format expected by
# Analysis. rpyc_loader resolves it below ``sys._MEIPASS/unrpyc-master``.
_unrpyc_root = Path('unrpyc-master')
_unrpyc_skip_dirs = {'.github', '__pycache__', 'testcases'}
for _path in _unrpyc_root.rglob('*'):
    if not _path.is_file() or any(_part in _unrpyc_skip_dirs for _part in _path.parts):
        continue
    if _path.suffix.lower() in {'.pyc', '.rpyc'}:
        continue
    _dest = Path('unrpyc-master') / _path.relative_to(_unrpyc_root).parent
    datas.append((str(_path), str(_dest)))


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RenPyTranslator',
    version=version_info,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
