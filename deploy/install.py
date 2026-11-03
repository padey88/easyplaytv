#!/usr/bin/env python3
"""Install proxy and scheduled catalogue-refresh user services."""
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UNITS = Path.home() / ".config" / "systemd" / "user"


def render(name):
    text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
    return text.replace("__PROJECT_DIR__", str(ROOT)).replace("__PYTHON__", sys.executable)


def main():
    env = ROOT / ".env"
    if not env.exists():
        env.write_text("EASYPLAY_USER=easyplay\nEASYPLAY_PASS=%s\n"
                       % secrets.token_urlsafe(24), encoding="utf-8")
    env.chmod(0o600)
    (ROOT / "public").mkdir(exist_ok=True)
    UNITS.mkdir(parents=True, exist_ok=True)
    for filename in ("easyplaytv.service", "easyplaytv-refresh@.service",
                     "easyplaytv-live.timer", "easyplaytv-vod.timer"):
        (UNITS / filename).write_text(render(filename), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now",
                    "easyplaytv.service", "easyplaytv-live.timer",
                    "easyplaytv-vod.timer"], check=True)
    print("Proxy and automatic live/VOD refresh timers are enabled.")
    print("Check schedules: systemctl --user list-timers 'easyplaytv-*'")


if __name__ == "__main__":
    main()
