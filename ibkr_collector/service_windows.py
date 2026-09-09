"""Current-user Windows Task Scheduler install/start/stop/status/uninstall."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from ibkr_collector import TASK_NAME
from ibkr_collector.config import default_data_dir, load_config, repo_root, write_example_config
from ibkr_collector.logging_setup import setup_logging
from ibkr_collector.secrets_win import read_ingest_token, write_ingest_token

SCHTASKS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")


def _venv_pythonw() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "FMP_SCREENER" / "ibkr-collector" / "venv" / "Scripts" / "pythonw.exe"


def _task_xml(pythonw: Path, repo: Path, user: str) -> str:
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>FMP_SCREENER read-only IBKR/TWS market-data collector (no orders)</Description>
    <Author>{user}</Author>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw}</Command>
      <Arguments>-m ibkr_collector run</Arguments>
      <WorkingDirectory>{repo}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""".format(user=escape(user), pythonw=escape(str(pythonw)), repo=escape(str(repo)))


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([SCHTASKS, *args], capture_output=True, text=True, check=False)


def _ensure_token() -> str | None:
    existing = read_ingest_token()
    if existing:
        return existing
    sys.stderr.write(
        "No ingest token in Windows Credential Manager. Copy the server token into a local "
        "0600 file (not via chat) and run: python -m ibkr_collector provision-token --from-file PATH\n"
    )
    return None


def provision_token(from_file: str) -> int:
    path = Path(from_file)
    if not path.is_file():
        sys.stderr.write("token file not found\n")
        return 3
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(ch.isspace() for ch in token):
        sys.stderr.write("token file is empty or contains whitespace\n")
        return 3
    write_ingest_token(token)
    sys.stdout.write("Ingest token stored in Windows Credential Manager. Delete the source file.\n")
    return 0


def install() -> int:
    if os.name != "nt":
        sys.stderr.write("Windows Task Scheduler install is only supported on Windows.\n")
        return 3
    cfg = load_config()
    write_example_config(cfg.config_path)
    setup_logging(cfg.log_dir)
    pythonw = _venv_pythonw()
    if not pythonw.is_file():
        sys.stderr.write("Collector venv pythonw missing at {0}\n".format(pythonw))
        return 3
    if not _ensure_token():
        return 3
    user = getpass.getuser()
    xml_path = default_data_dir() / "task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(_task_xml(pythonw, repo_root(), user), encoding="utf-16")
    created = _run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    if created.returncode != 0:
        sys.stderr.write(created.stdout + created.stderr)
        return created.returncode
    sys.stdout.write("Installed task {0} (logon trigger, current user, no wake).\n".format(TASK_NAME))
    return 0


def start() -> int:
    if os.name != "nt":
        return 3
    existing = _run_schtasks(["/Query", "/TN", TASK_NAME])
    if existing.returncode != 0:
        code = install()
        if code != 0:
            return code
    result = _run_schtasks(["/Run", "/TN", TASK_NAME])
    sys.stdout.write(result.stdout or "started\n")
    return result.returncode


def stop() -> int:
    if os.name != "nt":
        return 3
    result = _run_schtasks(["/End", "/TN", TASK_NAME])
    sys.stdout.write(result.stdout or "stop requested\n")
    return 0 if result.returncode in {0, 1} else result.returncode


def status() -> int:
    cfg = load_config()
    lock_held = cfg.lock_path.exists()
    sys.stdout.write("config: {0}\n".format(cfg.config_path))
    sys.stdout.write("lock: {0} exists={1}\n".format(cfg.lock_path, lock_held))
    sys.stdout.write("queue: {0} exists={1}\n".format(cfg.queue_path, cfg.queue_path.exists()))
    sys.stdout.write("ingest_url: {0}\n".format(cfg.ingest_url))
    sys.stdout.write("token_present: {0}\n".format(bool(read_ingest_token())))
    if os.name == "nt":
        result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
        sys.stdout.write(result.stdout or result.stderr or "task not installed\n")
        return 0 if result.returncode == 0 else 2
    sys.stdout.write("not Windows; task scheduler N/A\n")
    return 0


def uninstall() -> int:
    if os.name != "nt":
        return 3
    stop()
    result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    sys.stdout.write(
        "Task removed (if it existed). Canonical PostgreSQL market-data rows are not deleted.\n"
    )
    return 0 if result.returncode in {0, 1} else result.returncode


def dispatch(command: str, *, from_file: str | None = None) -> int:
    if command == "install":
        return install()
    if command == "start":
        return start()
    if command == "stop":
        return stop()
    if command == "status":
        return status()
    if command == "uninstall":
        return uninstall()
    if command == "provision-token":
        if not from_file:
            sys.stderr.write("provision-token requires --from-file PATH\n")
            return 3
        return provision_token(from_file)
    raise SystemExit("unknown command")
