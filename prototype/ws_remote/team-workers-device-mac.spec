# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['device.py'],
    pathex=[],
    binaries=[],
    datas=[('../exec_server.py', '.'), ('ui/device.html', 'ui'), ('ui/theme.css', 'ui')],
    hiddenimports=['websockets', 'exec_server', 'docker_policy', 'shlex', 'locale', 'signal', 'subprocess', 'tempfile', 'threading', 'uuid', 'hmac', 'http.server'],
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
    name='team-workers-device-mac',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
