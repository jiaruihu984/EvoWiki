# EvoWiki desktop tray

`EvoWiki.exe` is a small, unsigned Windows Forms application built from
`EvoWikiDesktop.cs`. Keep the executable in the repository root and open it
directly. It needs Windows .NET Framework 4.5 or newer, already present on the
supported Windows 11 platform. Users do not need a Python installation to run
the tray; the ordinary EvoWiki launcher still requires the README prerequisites.

The application creates a notification-area icon and invokes the existing
`uv run --isolated --no-project --with python-dotenv==1.2.4 python
scripts/evowiki_run.py` commands. It contains no model calls, downloader,
PowerShell loader, dynamic compilation or separate service manager. The normal
launcher downloads product dependencies as documented. Starting the desktop
entry starts the local product; learning still requires its normal setup and
budget confirmation.

The tray supports start, stop, restart, opening the owned vault, status and
logs. **Stop service and exit** closes the tray only after normal `down --db`
succeeds. Logs stay in `.evowiki-run/desktop.log`. Language follows
`--language zh|en`, then `EVOWIKI_CLI_LANGUAGE`, then the system locale.

The approved mark has an orange running variant and a grey stopped variant.
Both contain 16/24/32/48/64/128/256 px frames. The tray checks this instance's
local health endpoint every five seconds, so a service stopped outside the
menu also changes the icon. Startup remains grey until the service responds.
Vector sources are in `icons/`; rebuild them with the existing `deep` extra:
`uv run --extra deep python scripts/build_desktop_icons.py`.

Developers can build without installing an SDK:

```powershell
uv run --isolated --no-project python scripts/build_desktop.py
```

The build script invokes the Windows .NET Framework x64 `csc.exe` with explicit
source, references and compiler flags. It only compiles; it does not run the
executable, start services, change antivirus settings or require administrator
rights. `build-manifest.json` records the source, compiler and executable SHA-256
hashes and complete flags. The legacy compiler does not offer deterministic
builds, so these hashes identify a particular build rather than promise identical
binary bytes on rebuilding. No signing or antivirus certification is claimed.
