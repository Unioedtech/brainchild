"""Per-OS service registration. One entry point, dispatches by platform."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from brainchild.config import PATHS

LABEL = "sh.brainchild.daemon"


def register() -> None:
    if sys.platform == "darwin":
        _register_macos()
    elif sys.platform == "linux":
        _register_linux()
    elif sys.platform == "win32":
        _register_windows()
    else:
        raise NotImplementedError(f"unsupported platform: {sys.platform}")


def unregister() -> None:
    if sys.platform == "darwin":
        _unregister_macos()
    elif sys.platform == "linux":
        _unregister_linux()
    elif sys.platform == "win32":
        _unregister_windows()


# ---- macOS ------------------------------------------------------------------

def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _register_macos() -> None:
    python = sys.executable
    plist = _macos_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>brainchild</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>WorkingDirectory</key><string>{PATHS.install_dir}</string>
  <key>StandardOutPath</key><string>{PATHS.logs_dir / 'launchd.out'}</string>
  <key>StandardErrorPath</key><string>{PATHS.logs_dir / 'launchd.err'}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
"""
    plist.write_text(content, encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=True)


def _unregister_macos() -> None:
    plist = _macos_plist_path()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if plist.exists():
        plist.unlink()


# ---- Linux ------------------------------------------------------------------

def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "brainchild.service"


def _register_linux() -> None:
    python = sys.executable
    unit = _systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    content = f"""[Unit]
Description=Brainchild personal-agent daemon
After=network-online.target

[Service]
Type=simple
ExecStart={python} -m brainchild
Restart=always
RestartSec=10
WorkingDirectory={PATHS.install_dir}
StandardOutput=append:{PATHS.logs_dir / 'systemd.log'}
StandardError=append:{PATHS.logs_dir / 'systemd.log'}

[Install]
WantedBy=default.target
"""
    unit.write_text(content, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "brainchild"], check=True)


def _unregister_linux() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", "brainchild"], capture_output=True)
    unit = _systemd_unit_path()
    if unit.exists():
        unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


# ---- Windows ----------------------------------------------------------------

def _register_windows() -> None:
    # Prefer pythonw.exe (no console) but fall back to python.exe.
    # Microsoft Store Python and some venv setups don't ship pythonw.
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not Path(pythonw).exists():
        pythonw = sys.executable
    user = os.environ.get("USERNAME", "")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>{user}</Author></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <RestartOnFailure><Interval>PT1M</Interval><Count>99</Count></RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw}</Command>
      <Arguments>-m brainchild</Arguments>
      <WorkingDirectory>{PATHS.install_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    xml_path = PATHS.install_dir / "brainchild-task.xml"
    xml_path.write_text(xml, encoding="utf-16")
    subprocess.run(["schtasks", "/Delete", "/TN", "Brainchild", "/F"], capture_output=True)
    subprocess.run(
        ["schtasks", "/Create", "/TN", "Brainchild", "/XML", str(xml_path)],
        check=True,
    )


def _unregister_windows() -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", "Brainchild", "/F"], capture_output=True)
