import json
import os
from pathlib import Path


Path("workspace-result.txt").write_text("workspace is writable\n")
print(
    json.dumps(
        {
            "cwd": os.getcwd(),
            "home": os.environ.get("HOME"),
            "workspace_write": Path("workspace-result.txt").read_text().strip(),
        },
        sort_keys=True,
    )
)
