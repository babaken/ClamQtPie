<p align="center">
  <img src="clamav_frontend/icons/app.png" width="128" alt="ClamQtPie icon">
</p>

<h1 align="center">ClamQtPie</h1>

<p align="center">
  A Windows / Linux GUI frontend for ClamAV (clamd / freshclam)
</p>

<p align="center">
  <a href="README.md">日本語</a> | English
</p>

---

## Overview

ClamQtPie is a GUI frontend that turns the open-source antivirus engine [ClamAV](https://www.clamav.net/) into a desktop app that lives in the system tray. It is written in Python with PySide6 (Qt) and runs on both Windows and Linux.

The ClamAV engine itself is not bundled. ClamQtPie talks to a separately installed clamd.

> The application UI and the design documents are in Japanese.

## Features

| Feature | Description |
|---|---|
| On-demand scan | Scan selected folders and files, with live progress and the file being scanned |
| Real-time protection | Windows: detects file creation and changes with watchdog, then scans<br>Linux: integrates with ClamAV's official **clamonacc** (on-access scanning) |
| Quarantine | Moves detected files into a quarantine folder; list, restore or permanently delete them |
| Scheduling | Run scans and definition updates on a cron schedule, without running the same job twice at once |
| Definition updates | Manual and automatic updates via freshclam, with update history |
| Notifications | Desktop notifications (Windows: Toast / Linux: notify-send) and e-mail (SMTP). On Windows, clicking a detection notification opens the Quarantine screen |
| Exclusions | Exclude by file, folder, extension or process |
| System tray | State icons (normal / monitoring / detection / error), single-instance lock, start with the OS (applied as soon as you save it in Settings) |
| Logs | Records detections, scans, quarantine actions and updates with a category; filtering and CSV export; jump from an error code (e.g. `E1001`) to the matching log entries |
| Starting clamd | Tries to start clamd as an OS service if it is stopped (the "サービスを開始" button on the "ClamAV エンジン サービス" screen, plus one automatic attempt when a stop is detected; a clamd stopped from that screen is not restarted automatically) |

## Requirements

- **OS**: Windows 10 / 11 (64-bit) or Linux
- **ClamAV**: must be installed separately (see "Required ClamAV components" below)
- **For development**: Python 3.14 or later (also tested on 3.15)

### Required ClamAV components

| Component | Windows | Linux | Purpose |
|---|---|---|---|
| clamd (scan engine) | **Required**. Registered as the OS service `clamd` | **Required**. The `clamav-daemon` service | Every scan (on-demand and real-time) is sent to clamd |
| freshclam (definition updates) | **Required**. The app runs it directly (no update service needed) | **Required**. The `clamav-freshclam` service is also required for automatic and scheduled updates | Updating virus definitions |
| clamonacc | Not used | Only for real-time protection in clamonacc mode | See "Setting up real-time protection on Linux (clamonacc)" below |
| clamscan / clamdscan | Not used | Not used | Only for troubleshooting |

- No scans can run while clamd is stopped (the window, tray, definition updates, quarantine management and logs still work). When the app detects that clamd has stopped, it tries once to start it as an OS service
- The app looks up freshclam and clamonacc on the **PATH**. If they are not on the PATH, set the full path on the Settings screen (freshclam on the "定義更新" tab, clamonacc on the "ClamAV エンジン" tab)
- By default the app connects to clamd over TCP `127.0.0.1:3310` on Windows and over the UNIX socket `/var/run/clamav/clamd.ctl` on Linux. If you change `TCPSocket` / `TCPAddr` / `LocalSocket` in `clamd.conf`, set the same values on the "ClamAV エンジン" tab of the Settings screen (`clamd.tcp_host` / `clamd.tcp_port` / `clamd.unix_socket_path` in `config.json`). "接続テスト" (connection test) lets you try the connection before saving. Connection settings take effect after the app is restarted. The socket location differs between Linux distributions, so check `LocalSocket` in `clamd.conf`
- clamd is expected to run on the same PC

### Preparing Windows

<!-- To verify: whether the ClamAV 1.5.4 installer registers the service and updates PATH, where conf_examples is, and how clamd.exe --install-service behaves -->

1. Install the Windows build from the [official ClamAV site](https://www.clamav.net/downloads)
2. In the install folder (for example `C:\Program Files\ClamAV`), copy `clamd.conf.sample` and `freshclam.conf.sample` from `conf_examples` to the install folder as `clamd.conf` / `freshclam.conf`, and delete the `Example` line in each
3. Make clamd listen on TCP in `clamd.conf`:

   ```
   TCPSocket 3310
   TCPAddr 127.0.0.1
   ```

4. In an administrator command prompt, go to the install folder, download the definitions, then register and start clamd as a service:

   ```bat
   freshclam.exe
   clamd.exe --install-service
   sc start clamd
   ```

5. Add the install folder to the PATH environment variable (or set the freshclam location on the "定義更新" tab of the Settings screen)

### Preparing Linux

Install with `apt install clamav clamav-daemon` (Debian / Ubuntu), `pacman -S clamav` (Arch), etc., and enable the clamd and definition update services (see "Notes for Linux" below).

To run the release build on an X11 desktop, also install `libxcb-cursor0` (`xcb-util-cursor` on Arch), which Qt needs. It is not needed on a Wayland desktop.

### Checking the setup

On the app's "システム診断" (system diagnostics) screen, click "ヘルスチェックを実行" and make sure there are no [NG] or [注意] lines. [注意] lines point out executables that are not on the PATH and differences between the listen address in `clamd.conf` and the app's connection settings.

### Tested environments

| Environment | Details |
|---|---|
| Windows 11 Pro | Python 3.14.8 / 3.15.0, PySide6 6.12.0, ClamAV 1.5.4 |
| Linux (WSL2 Ubuntu 26.04) | Python 3.14, PySide6 6.12.0, ClamAV 1.5.4 (including clamonacc) |

## Usage (release build)

There is no installer; ClamQtPie is distributed as a portable Zip.

1. Extract the release Zip
   - **Windows: extract it to a location that only administrators can write to, such as `C:\Program Files\ClamQtPie\`** (extracting there needs administrator rights). The app runs with administrator rights, so if it sits in a folder that standard users can modify, changes to its files or settings could be abused with administrator rights
   - Places to avoid on Windows: Desktop, Downloads, Documents, USB drives, and folders you created directly under `C:\` (such as `C:\Tools`; by default, standard users can write to them)
   - Linux: extract it to a location only you can write to, such as under your home folder (avoid shared folders that other users can write to)
   - On both systems, **do not put it in a synced folder** such as OneDrive, Dropbox or Google Drive. Quarantined files (`quarantine/`) would spread to the cloud and to your other PCs. Also exclude `quarantine/` from backups
2. Run `ClamQtPie.exe` (Windows) or `ClamQtPie` (Linux)

### Portable layout

- On first launch, `config.json`, `app.db`, `logs/` and `quarantine/` are created next to the executable
- Copy the whole folder to another PC to take your settings with you (the destination must also meet the conditions above)
- **Only the SMTP password has to be entered again on the new machine**, because it is stored in the OS keychain (Windows: Credential Manager, Linux: Secret Service)

### Notes for Windows

- The app **always runs with administrator rights**, so a UAC prompt appears every time it starts. Administrator rights are needed for quarantine (changing file permissions), real-time protection, and the auto-start setting (writing to the registry)
- If you enable "OS起動時自動起動" (start with the OS) in Settings, **the UAC prompt also appears at every OS start**. This is how Windows works. If you choose "No", protection does not run for that session
- Real-time protection scans and quarantines a file **after** it has been saved. It cannot block a file before it is opened
- **Keep Microsoft Defender enabled and use ClamQtPie alongside it.** ClamQtPie is meant as an additional layer. If Defender flags files in the quarantine folder (`quarantine/`), add only the quarantine folder to Defender's exclusions (do not exclude the whole ClamQtPie folder)

### Notes for Linux

- **Run the app as a normal user** (no `sudo`). Tasks that need root (starting clamd, clamonacc, definition updates) show a pkexec authentication prompt when needed
- It is recommended to let the OS services run clamd and the definition updates

  ```sh
  sudo systemctl enable --now clamav-daemon.service clamav-freshclam.service
  ```

  Service names may differ by distribution (Arch uses `clamav-daemon.service` / `clamav-freshclam.service`)
- How definition updates work
  - "Update now" from the Update screen, the Home screen or the tray asks for administrator authentication (pkexec), then runs freshclam
  - Automatic and scheduled updates cannot authenticate unattended, so they do not run freshclam. If the `clamav-freshclam` service is running they are skipped (the service does the update); otherwise they fail with a hint to enable the service

### Upgrading

1. Extract the new version's Zip into a separate folder
2. To keep your settings, copy `config.json` from the old folder into the new one (the SMTP password has to be entered again)
3. Delete the old folder

The same steps apply when upgrading from a version released under the old name (ClamAV Frontend, `ClamAVFrontend.exe`). The first time the new version starts, it carries over the autostart entry, the SMTP password and the setting that opens the Quarantine screen from a notification click, all of which were registered under the old name.

### Uninstalling

1. Right-click the tray icon and choose "終了" (Quit)
2. If you enabled auto-start, turn off "OS起動時自動起動" on the General tab in Settings and save
3. Delete the extracted folder
4. (Windows) Remove the settings used to open the Quarantine screen from a notification click. Run this in PowerShell:

   ```powershell
   Remove-Item -Recurse "HKCU:\Software\Classes\clamqtpie", "$env:LOCALAPPDATA\clamqtpie"
   ```

### Setting up real-time protection on Linux (clamonacc)

On Linux, ClamQtPie uses clamonacc to scan files **before** they are opened.

1. Install clamonacc (part of `clamav-daemon` on Debian / Ubuntu) and pkexec (polkit)
2. Run clamd on a UNIX socket (`LocalSocket`)
3. Add the following line to `clamd.conf` and restart clamd (**required**: clamonacc refuses to start without it):

   ```
   OnAccessExcludeUname clamav
   ```

   To stop infected files from being opened at all, also add `OnAccessPrevention yes`

4. When you start monitoring on the Real-time screen, you will be asked for administrator authentication (clamonacc needs root)

If clamonacc or pkexec is not found, ClamQtPie falls back to watchdog automatically. The method in use is shown as "方式" (method) on the Real-time screen.

Notes:

- Files larger than `OnAccessMaxFileSize` (5 MB by default) are not scanned on access
- clamd's temporary directory (`TemporaryDirectory`, `/tmp` by default) and `/` cannot be watched (a clamonacc limitation; ClamQtPie skips them)
- If `clamd.conf` contains `OnAccessMountPath` or `OnAccessDisableDDD yes`, the watch targets and excluded folders set in ClamQtPie are not used
- With glibc 2.44 or later (e.g. Arch Linux), a clamonacc bug ([ClamAV Issue #1797](https://github.com/Cisco-Talos/clamav/issues/1797)) keeps it from watching subfolders, and monitoring cannot start when there are excluded folders. ClamQtPie shows an error in that case. Set `realtime.linux_backend` to `"watchdog"` in `config.json` to use watchdog instead (files are scanned after they change)

## Development

```bash
cd clamav_frontend
python -m venv .venv
.venv\Scripts\activate            # Linux: . .venv/bin/activate
pip install -r requirements.txt

python app.py                     # run the app
python -m pytest -q               # run tests (GUI tests run headless)
python build_release.py --version 0.9.0   # build with PyInstaller and create a Zip in dist/
```

- Tests live under `tests/` and use a fake clamd (`FakeClamdServer`). If a real clamd is available, the EICAR integration tests run too
- To run the EICAR tests with Windows Defender enabled, add `clamav_frontend\.pytest_eicar` to Defender's exclusions and run `python -m pytest --basetemp=.pytest_eicar`
- Integration tests against a real clamonacc on Linux: `sudo QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/integration/test_real_clamonacc.py`

### Directory layout

```
ClamQtPie/
├── clamav_frontend/      the application
│   ├── app.py            entry point (builds the services and wires them to the screens)
│   ├── core/             service layer (clamd protocol, scanning, real-time protection, quarantine, scheduler, notifications, ...)
│   ├── db/               SQLAlchemy models and DB service (Alembic migrations run automatically at startup)
│   ├── gui/              main window, 8 screens, system tray
│   ├── alembic/          DB migrations
│   ├── icons/            icons (generated by scripts/generate_icons.py)
│   ├── scripts/          icon generation and performance measurement scripts
│   ├── tests/            unit / integration tests
│   └── README_DEV.md     per-phase implementation notes, design decisions and bugs found (Japanese)
└── docs/                 requirements, design documents and test specification (Japanese)
```

## Documentation

The design documents are in `docs/` (Japanese). Main ones:

- [Requirements](docs/ClamAV_Frontend_Requirements.md)
- [Linux real-time protection design (clamonacc)](docs/Linuxリアルタイム監視詳細設計書.md)
- [Basic design](docs/基本設計書_ClamAVフロントエンド.md) / [Detailed design](docs/詳細設計書_ClamAVフロントエンド.md) / [Screen design](docs/画面設計書_ClamAVフロントエンド.md)
- [Error codes and exceptions](docs/エラーコード一覧_例外設計書.md)
- [config.json schema](docs/config_jsonスキーマ定義書.md)
- [Test specification](docs/テスト仕様書.md)
- [Development notes (README_DEV.md)](clamav_frontend/README_DEV.md)

## Reporting issues

Please open an [issue](https://github.com/babaken/ClamQtPie/issues) and include the error code shown in the error dialog (e.g. `E1001`) and a CSV exported from the Log screen.

Please do not report security problems (vulnerabilities) in public issues. Follow [SECURITY.md](SECURITY.md) and use GitHub's private vulnerability reporting instead.

## License

ClamQtPie is released under the [MIT License](LICENSE). You may use, modify, redistribute and sell it freely, as long as you keep the copyright notice.

The release Zip includes the following libraries, each under its own license:

| Library | License |
|---|---|
| PySide6 / Qt (including shiboken6) | LGPL-3.0 (shipped as separate DLLs in the release build, so they can be replaced) |
| SQLAlchemy, Alembic, APScheduler, keyring | MIT |
| watchdog | Apache-2.0 |
| pywin32 (Windows only) | PSF |
| Python itself | PSF |

ClamAV itself (GPL-2.0) is not bundled. ClamQtPie only talks to a separately installed clamd over a socket.

## Trademarks

ClamAV is a registered trademark of Cisco Systems, Inc. This project is not an official ClamAV project and is not affiliated with Cisco Systems, Inc. The app icon is an original design and does not use the official ClamAV logo.
