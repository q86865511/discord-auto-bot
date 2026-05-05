# PyInstaller spec for DiscordBot.exe — single-file Windows executable.
#
# Strategy: do NOT bundle Chromium (would push exe to 400+ MB and slow
# cold-start). Instead, the boot path detects missing Chromium on first
# run and triggers `playwright install chromium`, downloading ~300MB to
# the user's local Playwright cache.
#
# Run: build.bat (which calls `pyinstaller build.spec`)

# noinspection PyUnresolvedReferences
block_cipher = None


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        # Static assets (none right now; placeholder for future templates etc.)
    ],
    hiddenimports=[
        # Playwright async API uses dynamic imports that PyInstaller misses
        "playwright._impl._driver",
        "playwright._impl._api_types",
        "playwright._impl._connection",
        # SMTP / TLS may pull these implicitly
        "email.mime.text",
        "smtplib",
        # Logging rotation file handler
        "logging.handlers",
        # bot/ package — referenced via dynamic imports in main.py
        "bot",
        "bot.slot",
        "bot.slot.parsers",
        "bot.slot.analysis",
        "bot.web",
        "bot.web.dashboard",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't ship the test stack
        "tkinter",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="DiscordBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX trips Defender false-positives more often
    upx_exclude=[],
    runtime_tmpdir=None,      # extract to OS temp on each run
    console=True,             # we need the Rich UI; keep the cmd window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="icon.ico",        # add an .ico if you want a custom icon
)
