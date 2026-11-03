#!/usr/bin/env python3
"""Install the EasyPlayTV user service for the current checkout."""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
ENV = ROOT / ".env"


def main():
    if not ENV.exists():
        password = secrets.token_urlsafe(24)
        ENV.write_text("EASYPLAY_USER=easyplay\nEASYPLAY_PASS=%s\n" % password,
                       encoding="utf-8")
        ENV.chmod(0o600)
        print("Created %s with a generated password." % ENV)
    ENV.chmod(0o600)
    (ROOT / "public").mkdir(exist_ok=True)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "deploy" / "easyplaytv.service").read_text(encoding="utf-8")
    content = template.replace("__PROJECT_DIR__", str(ROOT)).replace("__PYTHON__", sys.executable)
    (UNIT_DIR / "easyplaytv.service").write_text(content, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "easyplaytv.service"], check=True)
    print("Installed the user service. Start it with: systemctl --user start easyplaytv")
    print("For LAN access set EASYPLAY_HOST=0.0.0.0 in .env and restart the service.")


if __name__ == "__main__":
    main()
