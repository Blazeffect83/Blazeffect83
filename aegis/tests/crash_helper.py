"""Child process for the abrupt-termination test: starts an objective whose
step runs a long sandboxed process, so the parent can SIGKILL us mid-action."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.objectives import ObjectiveManager  # noqa: E402
from app.models.mock_provider import MockProvider  # noqa: E402
from app.services import build_services  # noqa: E402
from tests.conftest import make_settings  # noqa: E402

tmp = Path(sys.argv[1])
tool = sys.argv[2]
settings = make_settings(tmp)
step = ({"title": "long sandboxed job", "tool": "python_run", "args": {"code": "import time\ntime.sleep(60)",
                                                                          "timeout_seconds": 120}}
        if tool == "python_run" else
        {"title": "long tests", "tool": "run_tests", "args": {"path": "test_slow.py", "timeout_seconds": 120}})
mock = MockProvider([{"steps": [step]}])
s = build_services(settings, providers={"mock": mock})
oid = ObjectiveManager(s).create("Run a long job that will be interrupted")
if tool == "run_tests":
    ws = settings.workspace / oid
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n")
print(oid, flush=True)
s.controller.run(oid)
