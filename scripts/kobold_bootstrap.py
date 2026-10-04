"""Create a model-free KoboldCpp startup profile; native .kcpps files stay intact."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Keep server identity and access controls. Model/runtime settings are applied by
# KoboldCpp when the original profile is selected inside the proxy's GPU lease.
SERVER_FIELDS = frozenset({
    "host", "port", "port_param", "password", "adminpassword", "ssl", "nocertify",
    "reqtimeout", "quiet", "debugmode", "remotetunnel", "adminunloadtimeout",
    "downloaddir", "baseconfig",
})


def write_bootstrap(source: Path, destination: Path) -> None:
    profile = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError("KoboldCpp profile must be a JSON object")
    bootstrap = {key: value for key, value in profile.items() if key in SERVER_FIELDS}
    bootstrap.update(nomodel=True, usecpu=True, admin=True, routermode=True)
    destination.write_text(json.dumps(bootstrap) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_bootstrap(Path(sys.argv[1]), Path(sys.argv[2]))
