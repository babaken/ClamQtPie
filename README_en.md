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

> [!IMPORTANT]
> **The way you install ClamQtPie has changed in v0.10.0.** The portable build (unzip and run as administrator) is gone. On Windows you install it with an installer, and on Linux with `install.sh`. Protection now runs as an OS service, and the tray app runs with normal user rights. If you use v0.9.x, follow [Migrating from the portable build (v0.9.x)](#migrating-from-the-portable-build-v09x) to move your settings, quarantined files and other data.

## Overview

ClamQtPie is a GUI frontend that lets you use the open-source antivirus engine [ClamAV](https://www.clamav.net/) from a desktop app in the system tray or from a web browser. It is written in Python with PySide6 (Qt) and runs on both Windows and Linux.

The ClamAV engine itself is not bundled. ClamQtPie talks to a separately installed clamd.

> The application UI and the design documents are in Japanese. This README gives the Japanese labels with an English explanation.

## Features

| Feature | Description |
|---|---|
| On-demand scan | Scan selected folders and files, with live progress and the file being scanned |
| Real-time protection | Windows: detects file creation and changes with watchdog, then scans<br>Linux: integrates with ClamAV's official **clamonacc** (on-access scanning) |
| Quarantine | Moves detected files into a quarantine folder; list, restore or permanently delete them |
| Scheduling | Run scans and definition updates on a cron schedule, without running the same job twice at once. Can wake a sleeping PC to run a job |
| Definition updates | Manual and automatic updates via freshclam, with update history. On Linux you can also start and stop the OS definition update service (clamav-freshclam) from the screen |
| Notifications | Desktop notifications (Windows: Toast / Linux: notify-send) and e-mail (SMTP). On Windows, clicking a detection notification opens the Quarantine screen. Notifications raised by the service while the tray app was not running are shown later |
| Exclusions | Exclude by file, folder, extension or process |
| System tray | State icons (normal / monitoring / detection / error), single-instance lock, start at sign-in |
| WebUI | Use ClamQtPie from a browser (off by default; login, two-step verification, turns itself off at a set time). Supports HTTPS (ClamQtPie's local CA) and, as an experimental feature, connections from other devices on the LAN |
| Logs | Records detections, scans, quarantine actions and updates with a category; filtering and CSV export; jump from an error code (e.g. `E1001`) to the matching log entries |
| Starting clamd | Tries to start clamd as an OS service if it is stopped (the "サービスを開始" button on the "ClamAV エンジン サービス" screen, plus one automatic attempt when a stop is detected; a clamd stopped from that screen is not restarted automatically) |
| Admin command | `clamqtpie-ctl`: service status, settings (including protected ones), quarantine, which users may use the tray app, migration from the old build, WebUI settings |

## How it is built

ClamQtPie has four parts.

| Part | Description |
|---|---|
| Service | Does all the protection work (scans, real-time protection, quarantine, schedules, definition updates, notifications). On Windows it is the service `ClamQtPie` (LocalSystem); on Linux it is the systemd unit `clamqtpie.service` (root). It starts when the PC starts and runs even when nobody is signed in |
| Tray GUI | The window and tray icon (`ClamQtPie.exe` / `clamqtpie`). It runs with normal user rights and asks the service to do things. Closing the tray GUI does not stop protection |
| WebUI | Pages you open in a browser, served by the service (off by default) |
| clamqtpie-ctl | The admin command. Run it as administrator (Windows) or root (Linux) |

Only users in the ClamQtPie Users group (Windows) or the `clamqtpie` group (Linux) can use the tray GUI. The user who installs ClamQtPie is added automatically.

Where settings and data live:

| | Windows | Linux |
|---|---|---|
| Settings | `%ProgramData%\ClamQtPie\config.json` | `/etc/clamqtpie/config.json` |
| Database, quarantined files and so on | `%ProgramData%\ClamQtPie\` | `/var/lib/clamqtpie/` |
| Logs | `%ProgramData%\ClamQtPie\logs\` | `/var/log/clamqtpie/` |
| Program | `%ProgramFiles%\ClamQtPie\` | `/opt/clamqtpie/` |

## Requirements

- **OS**: Windows 10 / 11 (64-bit), or Linux with systemd
- **ClamAV**: must be installed separately (see "Required ClamAV components" below)
- **For development**: Python 3.14 or later (also tested on 3.15)

### Required ClamAV components

| Component | Windows | Linux | Purpose |
|---|---|---|---|
| clamd (scan engine) | **Required**. Registered as the OS service `clamd` | **Required**. The `clamav-daemon` service | Every scan (on-demand and real-time) is sent to clamd |
| freshclam (definition updates) | **Required**. The ClamQtPie service runs it directly | **Required**. The ClamQtPie service runs it directly. You may use the OS `clamav-freshclam` service instead (see "How definition updates work" below) | Updating virus definitions |
| clamonacc | Not used | Only for real-time protection in clamonacc mode | See "Setting up real-time protection on Linux (clamonacc)" below |
| clamscan / clamdscan | Not used | Not used | Only for troubleshooting |

- No scans can run while clamd is stopped (the window, tray, definition updates, quarantine management and logs still work). When ClamQtPie detects that clamd has stopped, it tries once to start it as an OS service
- ClamQtPie looks up freshclam and clamonacc on the **PATH** (the system-wide PATH that the service sees). If they are not on the PATH, set them with `clamqtpie-ctl config set update.freshclam_path <full path>` and `clamqtpie-ctl config set realtime.clamonacc_path <full path>` (the Settings screen of the tray GUI only shows these items; it cannot change them)
- By default ClamQtPie connects to clamd over TCP `127.0.0.1:3310` on Windows and over the UNIX socket `/var/run/clamav/clamd.ctl` on Linux. If you change `TCPSocket` / `TCPAddr` / `LocalSocket` in `clamd.conf`, set the same values in ClamQtPie (`clamd.tcp_host` / `clamd.tcp_port` / `clamd.unix_socket_path`) with `clamqtpie-ctl config set <key> <value> --restart`. The socket location differs between Linux distributions, so check `LocalSocket` in `clamd.conf`
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

5. Add the install folder to the system PATH environment variable (or, after installing ClamQtPie, set the location with `clamqtpie-ctl config set update.freshclam_path "C:\Program Files\ClamAV\freshclam.exe" --restart`)

### Preparing Linux

Install ClamAV with `apt install clamav clamav-daemon` (Debian / Ubuntu), `pacman -S clamav` (Arch) or similar, and enable the clamd service:

```sh
sudo systemctl enable --now clamav-daemon.service
```

Service names can differ between distributions. To use the tray GUI on an X11 desktop, also install `libxcb-cursor0` (`xcb-util-cursor` on Arch), which Qt needs. It is not needed on Wayland desktops. Admin operations are authenticated with polkit (pkexec).

### Checking the setup

After installing, open the "システム診断" (system diagnostics) screen of the tray GUI and press "ヘルスチェックを実行" (run health check). Make sure there are no [NG] or [注意] (caution) entries. [注意] reports executables that are not on the PATH and mismatches between what `clamd.conf` listens on and where ClamQtPie connects.

### Tested environments

| Environment | Details |
|---|---|
| Windows 11 Pro | ClamAV 1.5.4 |
| Linux (EndeavourOS, KDE Plasma 6 on Wayland, systemd) | ClamAV 1.5.4 (including clamonacc) |
| Linux (WSL2 Ubuntu 26.04) | Automated tests only |

## Installing

### Windows

1. Download `ClamQtPie-<version>-setup.exe` from the release and check that its SHA-256 matches the value in the release (in PowerShell: `Get-FileHash .\ClamQtPie-<version>-setup.exe`)
2. Run the installer (it needs administrator rights). You can choose two tasks:
   - Start the tray GUI when you sign in
   - Add the admin command (clamqtpie-ctl) to the PATH
3. If the last page suggests a restart, restart the PC, or sign out and sign in again. The installer has just added you to the ClamQtPie Users group, and the tray GUI cannot connect to the service until you sign in again (until then the installer does not start the tray GUI). This does not happen on updates

The service starts automatically when the PC starts. You can also start the tray GUI from "ClamQtPie" in the Start menu.

### Linux

1. Download `ClamQtPie-<version>-linux64.tar.gz` from the release, check its SHA-256, and extract it:

   ```sh
   sha256sum -c ClamQtPie-<version>-linux64.tar.gz.sha256
   tar -xzf ClamQtPie-<version>-linux64.tar.gz
   ```

2. Run `install.sh` from the extracted folder with `sudo`, as the user who will use the tray GUI:

   ```sh
   sudo sh <extracted folder>/install.sh
   ```

   It installs to `/opt/clamqtpie`, registers and starts the service, adds the user who ran `sudo` to the `clamqtpie` group, and puts `clamqtpie-ctl` in `/usr/local/bin`
3. Sign out and sign in again so the group takes effect (if it still does not work, restart the PC)
4. Start the tray GUI from "ClamQtPie" in the application menu

### Letting other users use the tray GUI

```sh
sudo clamqtpie-ctl users add <user name>      # Linux
clamqtpie-ctl users add <user name>           # Windows (administrator command prompt / PowerShell)
```

The user can use it after signing in again. `users list` lists the users and `users remove` removes one.

## Migrating from the portable build (v0.9.x)

You can move the settings, quarantined files, schedules, exclusions, watched folders and logs of v0.9.x (the portable build) to the service. **Migrate on the same day you install, so that the dates of the old logs are kept.**

1. Quit the old ClamQtPie (right-click the tray icon and choose "終了" (quit))
2. Install the new version (see "Installing" above)
3. Move the old data
   - **Windows**: if the installer finds the old build (a `ClamQtPie.exe` outside the install folder that is registered to start at sign-in), it asks after installing whether to move its data. Choose "はい" (yes). If it was not found, or to do it later, run this in an administrator command prompt:

     ```bat
     clamqtpie-ctl migrate --from "<old folder>" --restart
     ```

   - **Linux**: run:

     ```sh
     sudo clamqtpie-ctl migrate --from <old folder> --restart
     ```

   It lists what it will move (exclusions, watched folders, schedules, quarantined files) and asks you to confirm. It does not migrate if the destination already has data or if the old build is running
4. Once you have checked that everything works, delete the old folder (after migrating, a `MIGRATED.txt` is placed in it)

Notes:

- Only the settings that the tray GUI and WebUI can change are moved. Protected settings (where clamd listens, where freshclam and clamonacc are, and so on) are not moved; if the old values differ from the defaults, they are listed at the end. Set them with `clamqtpie-ctl config set` if you need them
- The SMTP password is moved automatically on Windows. On Linux you are asked to enter it (leave it empty to skip it)
- When you restore a quarantined file that came from the old build, its owner becomes the administrator or root (the original owner was not recorded). Fix the owner and permissions after restoring if needed
- When the new tray GUI starts, it replaces the old build's start-at-sign-in entry with the new tray GUI

## Using ClamQtPie

### Tray GUI

Right-click the tray icon for the menu, or double-click it for the main window. The screens of the main window (ホーム / home, スキャン / scan, リアルタイム保護 / real-time protection, 隔離 / quarantine, スケジュール / schedules, セキュリティ インテリジェンス / definition updates, 保護の履歴 / logs, 設定 / settings, システム診断 / diagnostics, ClamAV エンジン サービス / clamd service) work as in v0.9.x.

### Admin operations

Operations that change protection (starting and stopping real-time protection; changing watched folders, exclusions, schedules or settings; restoring or deleting quarantined files; starting and stopping clamd, and so on) need an administrator's confirmation first.

- **Windows**: after the UAC prompt, ClamQtPie's confirmation window lists what will change. Press "実行" (run) to go ahead
- **Linux**: after the polkit prompt (administrator password), ClamQtPie's confirmation window appears

On the real-time protection and schedule screens, changes are kept on the screen and saved together with one confirmation when you press "変更を保存" (save changes). If you leave the screen before saving, ClamQtPie asks whether to save.

Scanning and quarantining inside your own folder (home folder), updating definitions, and marking notifications as read need no confirmation.

### Protected settings

Where clamd listens, service names, and where freshclam and clamonacc are cannot be changed from the tray GUI or the WebUI, so that they cannot be used to take over the service (the Settings screen only shows them; the tooltip of each field shows the command). Change them with the admin command:

```sh
clamqtpie-ctl config show
clamqtpie-ctl config set <key> <value> --restart     # --restart restarts the service to apply it
```

On Windows, run it in an administrator command prompt or PowerShell; on Linux, run it with `sudo`.

### How definition updates work

- "今すぐ更新" (update now; on the definition update screen, home screen or tray), automatic updates and scheduled updates all run freshclam from the ClamQtPie service
- **On Linux, while the OS definition update service (clamav-freshclam) is running**, ClamQtPie does not run its own automatic or scheduled updates, so the definitions are not updated twice; it leaves updating to the OS service (it logs this only when skipping starts). The "OS の定義更新サービス (clamav-freshclam)" section of the definition update screen shows its state and which one is updating the definitions
  - "■ 停止" (stop) stops it now and keeps it from starting when the PC starts (`systemctl disable --now`). From then on, ClamQtPie updates automatically
  - "▶ 開始" (start) starts it now and makes it start when the PC starts (`systemctl enable --now`)
  - Both are admin operations. The section is not shown where clamav-freshclam does not exist (such as Windows)

## WebUI

You can do almost everything the tray GUI does from a browser. It is off by default, and the service only listens while it is on.

### Getting started

1. In the tray menu, choose "WebUI" → "ログイン設定..." (login settings) and set a user name and password (8 characters or more). ClamQtPie can also generate one (20 characters, shown only once)
2. Choose "WebUI" → "ON にする" (turn on) and pick how long (1 hour, 8 hours, 24 hours, a custom time, or until you turn it off). It turns itself off at that time
3. Choose "WebUI" → "ブラウザで開く" (open in browser) and log in

You can also use the admin command (`clamqtpie-ctl web status`, `web enable --for 8h`, `web enable --until 23:00`, `web disable`, `web set-login` and so on; see `clamqtpie-ctl web --help`).

- You are logged out after 30 minutes without activity
- After 5 failed logins in a row from the same address, logins from it are refused for 5 minutes
- Admin operations ask you to enter the password again in a confirmation dialog (not again for the next 5 minutes)
- Notifications (unread count, list, mark as read) are also available in the WebUI

### Two-step verification

ClamQtPie can also ask for the 6-digit code of an authenticator app (any app that supports TOTP, such as Google Authenticator or Microsoft Authenticator) when you log in.

1. In the tray menu, choose "WebUI" → "二段階認証の設定..." (two-step verification setup) (or run `clamqtpie-ctl web totp setup`)
2. Scan the QR code with the authenticator app and enter the code it shows. The app registers it under the name `ClamQtPie:<this PC's name>`
3. **Recovery codes** are shown once. If you lose your phone, you can use them one at a time instead of a code. Keep them somewhere safe

- When you use a recovery code, the number left is shown. When few are left, set up two-step verification again (this shows new recovery codes). It cannot be set up again from the WebUI
- If the password is right but the code is wrong 10 times, the password may have leaked, so ClamQtPie stops logins with the password and code and notifies you (logins with a recovery code still work). Change the password, or use "WebUI" → "ログインの停止を解く" (lift the login stop) in the tray menu (`clamqtpie-ctl web unlock`), to allow logins again
- To turn it off, choose "二段階認証の設定..." → "解除する" (turn off) (`clamqtpie-ctl web totp reset`)

### HTTPS (ClamQtPie's local CA)

If you only use the WebUI from a browser on this PC, HTTP is fine (browsers treat connections to `127.0.0.1` as secure). HTTPS is required to use it from other devices on the LAN.

```sh
clamqtpie-ctl web tls on        # turn on HTTPS (also shows the URL after the switch)
clamqtpie-ctl web tls status    # show the SHA-256 fingerprint and expiry of the CA certificate
```

ClamQtPie creates a certificate authority for this PC only (a local CA) and a server certificate issued by it. Register the CA certificate on each device that uses a browser (including this PC) to open the WebUI without warnings.

There are two ways to get the CA certificate.

- **A (recommended): export it on the PC and hand over the file.** On the PC that runs ClamQtPie, run `clamqtpie-ctl web tls export-ca clamqtpie-ca.crt` (as administrator on Windows, with `sudo` on Linux) and copy the file to the device, for example with a USB stick. It is written straight from the service's data, so you do not need to compare fingerprints when you register it on this PC. For another device, note the SHA-256 fingerprint it prints, so that you can check that the file was not replaced on the way by comparing it with the certificate details on the device after registering (if the device can show them)
- **B: download it in the device's browser.** Open the WebUI in the device's browser, go past the warning to the login page, and save it with "CA 証明書をダウンロード" (download the CA certificate). Check that the SHA-256 fingerprint on the login page matches `clamqtpie-ctl web tls status` on the PC before registering it. **Going past the warning leaves a certificate exception on that device, and the page may keep showing as "not secure" even after you register the CA** (Firefox for Android has no screen to remove the exception). After registering, run `clamqtpie-ctl web tls renew` on the PC (this only recreates the server certificate; you do not need to register the CA again) and reopen the page

The server certificate is renewed automatically. The CA certificate (valid for 2 years) is not. When it is about to expire, or when the LAN subnet has changed, `web tls status` tells you; recreate it with `clamqtpie-ctl web tls reset-ca`, remove the old CA from each device, and register the new one.

#### Registering on Windows

1. Double-click the CA certificate (.crt) and press "証明書のインストール..." (Install Certificate...)
2. Choose "現在のユーザー" (Current User) as the store location
3. **Choose "証明書をすべて次のストアに配置する" (Place all certificates in the following store), press "参照..." (Browse...) and choose "信頼されたルート証明機関" (Trusted Root Certification Authorities).** If you choose "証明書の種類に基づいて、自動的に証明書ストアを選択する" (Automatically select the certificate store), it goes into "中間証明機関" (Intermediate Certification Authorities) and browsers will not trust it
4. Press "はい" (Yes) on the confirmation

Firefox-based browsers (Firefox, Floorp and others) register it in their own settings (Settings → Privacy & Security → View Certificates... → Authorities → Import... → "Trust this CA to identify websites"). You can check the SHA-256 fingerprint of the imported certificate with "View" on the same screen. **If the same CA is in Windows' "Intermediate Certification Authorities", the warning stays even when the browser trusts it** (`SEC_ERROR_UNKNOWN_ISSUER`). If ClamQtPie's CA is under "Intermediate Certification Authorities" → "Certificates" in `certmgr.msc`, delete it.

#### Registering on Android

Item names differ a little between devices and Android versions. iOS has not been tested.

1. Save the CA certificate file on the device using A or B above
2. Go to Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate, and choose the saved file (searching Settings for "CA certificate" also finds it)
3. To check or remove it: Encryption & credentials → Trusted credentials → User

**Firefox for Android** does not use CAs registered in Android by default. In Firefox, open Settings → About Firefox and tap the Firefox logo 5 times; in the "Secret Settings" that appear, turn on "Use third party CA certificates" and reopen Firefox.

### Connections from the LAN (experimental)

> [!WARNING]
> Using the WebUI from other devices on the LAN (such as phones) is **experimental**. It is disabled by default and not shown in the tray menu. Please report problems in [Issues](https://github.com/babaken/ClamQtPie/issues) (security problems via [SECURITY.md](SECURITY.md)). Use it only on trusted networks such as your home network.

To allow it, HTTPS must be on, two-step verification must be set up, and the password must be **12 characters or more** (`web set-login` itself accepts 8 characters; the length of a password set with a development build before v0.10.0 is unknown, so set it again).

```sh
clamqtpie-ctl web lan on      # allow (use sudo on Linux)
clamqtpie-ctl web status      # show the LAN URLs and any notes
clamqtpie-ctl web lan off     # stop allowing
```

- Once allowed, while the WebUI is on, ClamQtPie accepts connections from directly connected LANs (subnets of private IPv4 addresses; VPNs and virtual networks are excluded). Turning the WebUI off keeps the permission, and the next time it is turned on it listens on the LAN too. The tray menu and the top of the WebUI show that LAN connections are possible
- While it is allowed, a login lasts 15 minutes. After 30 failed logins from the LAN within an hour, logins from the LAN are stopped for 15 minutes
- Turning off HTTPS, turning off two-step verification, or setting a password shorter than 12 characters turns the permission off again
- **Windows**: ClamQtPie creates the inbound firewall rule `ClamQtPie WebUI` (private networks and the local subnet only) and removes it when you stop allowing. It cannot connect while the LAN network is "Public" (`web status` tells you). Switch the network to "Private" in Windows Settings only if you trust it
- **Linux**: ClamQtPie does not change the firewall. If ufw or firewalld is on, `web status` shows how to open the port
- **Android**: recent Android versions require the browser to have the "Nearby devices" (local network) permission (without it, Chrome fails with `ERR_TOO_MANY_RETRIES`). `<PC name>.local` usually cannot be resolved, so open the address URL shown by `web status`

## Notes for Windows

- The tray GUI runs with normal rights (no UAC every time it starts, unlike v0.9.x). UAC appears only for admin operations
- Real-time protection scans and quarantines a file **after** it has been saved. It cannot block a file before it is opened
- **Keep Microsoft Defender running alongside.** ClamQtPie is meant as a supplement. If Defender detects files in the quarantine folder (`%ProgramData%\ClamQtPie\quarantine`), add only the quarantine folder to Defender's exclusions (do not exclude all of ClamQtPie's folders)

## Notes for Linux

- Start the tray GUI as a normal user (no `sudo` needed)
- The service runs as root, so running clamonacc and freshclam does not need authentication each time

### Setting up real-time protection on Linux (clamonacc)

On Linux, ClamQtPie uses clamonacc to scan files **before** they are opened.

1. Install clamonacc (on Debian / Ubuntu it is part of `clamav-daemon`)
2. Run clamd with a UNIX socket (`LocalSocket`)
3. Add this line to `clamd.conf` and restart clamd (**required**; without it clamonacc does not start):

   ```
   OnAccessExcludeUname clamav
   ```

   To block access to infected files, also add `OnAccessPrevention yes`

4. Add watched folders on the real-time protection screen of the tray GUI and start monitoring

If clamonacc is not found, ClamQtPie switches to watchdog mode automatically. The mode in use is shown as "方式" (mode) on the real-time protection screen.

Notes:

- Files larger than `OnAccessMaxFileSize` (5 MB by default) are not scanned on access
- clamd's temporary folder (`TemporaryDirectory`, `/tmp` by default) and `/` cannot be watched (a clamonacc limitation; if you add them, they are left out)
- If `clamd.conf` has `OnAccessMountPath` or `OnAccessDisableDDD yes`, the watched folders and excluded folders set in ClamQtPie are not used
- On systems with glibc 2.44 or later (such as Arch Linux), a clamonacc bug ([ClamAV Issue #1797](https://github.com/Cisco-Talos/clamav/issues/1797)) prevents watching subfolders, and monitoring cannot start while there are excluded folders. An error is shown in that case. Switch to watchdog mode (scans after a file changes) with `sudo clamqtpie-ctl config set realtime.linux_backend watchdog --restart`

## Updating and uninstalling

### Updating

Run the new installer (Windows) or `install.sh` (Linux) the same way as when installing. Settings and data are kept. The service is stopped before the files are replaced and started again afterwards.

### Uninstalling

- **Windows**: uninstall "ClamQtPie" from Settings → Apps → Installed apps. You are asked whether to delete the settings, data and logs too (including quarantined files; they are kept by default)
- **Linux**: run `sudo sh /opt/clamqtpie/uninstall.sh`. You are asked whether to delete the data (with `--purge`, it deletes the data and the group without asking)

## Troubleshooting

- **The tray GUI says "ClamQtPie サービスに接続できません (E8105)" (cannot connect to the ClamQtPie service)**: the service is stopped or has not started yet. Check it with `clamqtpie-ctl status` (in an administrator command prompt on Windows, `sudo clamqtpie-ctl status` on Linux). The logs are in `%ProgramData%\ClamQtPie\logs\` (Windows) and in `/var/log/clamqtpie/` and `journalctl -u clamqtpie` (Linux)
- **On Linux, the service has hit the start limit**: if the service fails 3 times in a day, systemd stops it (and refuses manual starts too). `sudo clamqtpie-ctl status` tells you this. Find and fix the cause in the logs, then start it with `sudo systemctl reset-failed clamqtpie && sudo systemctl start clamqtpie`
- **The tray GUI says "この PC の ClamQtPie を使う権限がありません (E8110)" (no permission to use ClamQtPie on this PC)**: you are not in the group that may use the tray GUI, or you have not signed in again since being added. Follow the guidance in the tray GUI: add yourself with `clamqtpie-ctl users add <user name>`, or sign out and sign in again (restart the PC if that does not help)
- **The WebUI says "サービスにつながりません" (cannot reach the service)**: the WebUI may have been turned off, or the service may have stopped. If you have just recreated the HTTPS CA, reload the page
- **The WebUI says "ログインの失敗が続いたため、しばらく時間をおいてください" (too many failed logins, wait a while)**: logins are locked after repeated failures (unlocked after 5 minutes). If they were stopped because of repeated wrong codes, waiting does not help; see "Two-step verification" above

## Development

```bash
cd clamav_frontend
python -m venv .venv
.venv\Scripts\activate            # Linux: . .venv/bin/activate
pip install -r requirements.txt

python service_main.py --console --data-dir <test folder>   # run the service in the foreground (as administrator / root)
python tray_main.py               # tray GUI (start the service first)
python -m pytest -q               # tests (GUI tests run headless)
python build_release.py --version 0.10.0   # Windows: installer, Linux: tar.gz, written to dist/
```

- Building the Windows installer needs [Inno Setup 6](https://jrsoftware.org/isinfo.php)
- `scripts/install_service.*` at the repository root registers a service that runs the development source (always remove it with `scripts/uninstall_service.*` when you are done). `scripts/run_ClamQtPie.*` starts the tray GUI from the development environment
- Tests live under `tests/`. clamd is replaced by a fake server (`FakeClamdServer`). If a real clamd is available, integration tests with EICAR also run
- To run the EICAR tests with Windows Defender enabled, add `clamav_frontend\.pytest_eicar` to Defender's exclusions and run `python -m pytest --basetemp=.pytest_eicar`
- Integration tests with a real clamonacc on Linux: `sudo QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/integration/test_real_clamonacc.py`

### Directory layout

```
ClamQtPie/
├── clamav_frontend/      The application
│   ├── service_main.py   Service entry point
│   ├── tray_main.py      Tray GUI entry point
│   ├── elevate_main.py   Elevation helper (confirmation window for admin operations)
│   ├── clamqtpie_ctl.py  Admin command
│   ├── api/              Web API served by the service (used by the tray GUI, WebUI and ctl)
│   ├── api_client/       API client for the tray GUI and the elevation helper
│   ├── core/             Services (clamd protocol, scanning, real-time protection, quarantine, scheduler, notifications, ...)
│   ├── db/               SQLAlchemy models and the DB service (Alembic migrations run automatically at startup)
│   ├── gui/              Tray GUI main window, 10 screens, system tray
│   ├── web/              WebUI (HTML, JavaScript, CSS)
│   ├── packaging/        Windows installer (Inno Setup), Linux install.sh and systemd unit
│   ├── alembic/          DB migrations
│   ├── icons/            Icons (generated by scripts/generate_icons.py)
│   ├── scripts/          Icon generation and performance measurement scripts
│   ├── tests/            unit / integration tests
│   └── README_DEV.md     Per-stage implementation notes, design decisions and bugs found (Japanese)
├── scripts/              Scripts to run the app and register the service from the development environment
└── docs/                 Requirements, design specs and test specs (Japanese)
```

## Documentation

The design documents are in `docs/` (Japanese). They are being revised for the v0.10.0 service architecture, starting with the [installer and packaging design](docs/インストーラ_パッケージング設計書.md) and the [error code list](docs/エラーコード一覧_例外設計書.md). The other documents still describe parts as in v0.9.x (a single app); the stage 12 sections of the [development notes (README_DEV.md)](clamav_frontend/README_DEV.md) are the current description.

- [Requirements](docs/ClamAV_Frontend_Requirements.md)
- [Linux real-time protection design (clamonacc)](docs/Linuxリアルタイム監視詳細設計書.md)
- [Basic design](docs/基本設計書_ClamAVフロントエンド.md) / [Detailed design](docs/詳細設計書_ClamAVフロントエンド.md) / [Screen design](docs/画面設計書_ClamAVフロントエンド.md)
- [Error codes and exceptions](docs/エラーコード一覧_例外設計書.md)
- [config.json schema](docs/config_jsonスキーマ定義書.md)
- [Test specification](docs/テスト仕様書.md)
- [Development notes (README_DEV.md)](clamav_frontend/README_DEV.md)

## Reporting bugs

Please report bugs in [Issues](https://github.com/babaken/ClamQtPie/issues) with the error code shown in the error dialog (e.g. `E1001`), a CSV exported from the log screen, and the diagnostics report ("レポートをコピー（パス伏せ字）" in the tray GUI, "伏せ字のレポートをコピー" in the WebUI; both mask your paths and names).

Do not report security problems (vulnerabilities) in public issues. Use the procedure in [SECURITY.md](SECURITY.md) (GitHub's private reporting).

## License

ClamQtPie is released under the [MIT License](LICENSE). You may use, modify, redistribute and use it commercially as long as you keep the copyright notice.

The release packages (installer and tar.gz) include the following libraries, each under its own license.

| Library | License |
|---|---|
| PySide6 / Qt (including shiboken6) | LGPL-3.0 (bundled as DLLs / shared libraries in the release packages, so they can be replaced) |
| SQLAlchemy, Alembic, APScheduler, keyring, FastAPI, Pydantic | MIT |
| Uvicorn, Starlette, segno, psutil | BSD-3-Clause |
| watchdog | Apache-2.0 |
| cryptography | Apache-2.0 or BSD-3-Clause |
| pywin32 (Windows only) | PSF |
| Python | PSF |

The ClamAV engine itself (GPL-2.0) is not bundled. ClamQtPie only talks to a separately installed clamd over a socket.

## Trademarks

ClamAV is a registered trademark of Cisco Systems, Inc. This project is not an official ClamAV project and is not affiliated with Cisco Systems, Inc. The application icon is an original design and does not use the official ClamAV logo.
