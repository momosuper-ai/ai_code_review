import os
import tempfile

os.environ.setdefault("VLLM_API_KEY", "synthetic")
os.environ.setdefault("REPOS_BASE_DIR", tempfile.mkdtemp(prefix="aicr-repos-"))

import pytest

from src.core.agent_tools import AgentToolExecutor


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repo structure for testing."""
    # Create files
    (tmp_path / "main.py").write_text(
        "def hello():\n    print('hello')\n\nclass Foo:\n    pass\n"
    )
    (tmp_path / "utils.py").write_text(
        "import os\n\ndef helper():\n    return 42\n"
    )
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").write_text(
        "from main import hello\n\ndef run():\n    hello()\n"
    )
    (sub / "__init__.py").write_text("")
    # Create a .git dir (empty, just to test exclusion)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    return tmp_path


@pytest.fixture
def executor(temp_repo):
    return AgentToolExecutor(str(temp_repo))
