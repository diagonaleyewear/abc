"""Read-only permissions audit; never opens databases, secrets or image contents."""
import json
import os
from pathlib import Path


def audit(root: Path) -> dict:
    result = {"path": str(root), "exists": root.exists()}
    if root.exists():
        info = root.stat()
        result.update(uid=info.st_uid, gid=info.st_gid, mode=oct(info.st_mode & 0o7777),
                      readable=os.access(root, os.R_OK), writable=os.access(root, os.W_OK),
                      traversable=os.access(root, os.X_OK) if root.is_dir() else None)
    return result


if __name__ == "__main__":
    assert os.getuid() == 1037 and os.getgid() == 100
    print(json.dumps({"identity": "1037:100", "groups": os.getgroups(),
                      "mode": "read-only metadata inspection",
                      "paths": [audit(Path(path)) for path in (
                          "/app/data", "/app/data/kering", "/app/data/kering/history.sqlite3",
                          "/app/data/process_activity.sqlite3", "/app/data/order_alerts.sqlite3",
                          "/app/repo/images", "/app/repo/images/.catalog.sqlite3")]}, indent=2))
