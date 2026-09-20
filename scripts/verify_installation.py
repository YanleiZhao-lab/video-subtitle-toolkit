import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from dependency_manager import DependencyManager  # noqa: E402
from toolkit_core import ToolPaths  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        os.environ["VIDEO_SUBTITLE_TOOLKIT_HOME"] = directory
        paths = ToolPaths.discover(ROOT / "app")
        Path(paths.work).mkdir(parents=True, exist_ok=True)
        manager = DependencyManager(ROOT / "app" / "dependencies.json", paths.work)
        assert len(manager.states()) >= 4
        probe = paths.work / ".write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    print("Base runtime verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
