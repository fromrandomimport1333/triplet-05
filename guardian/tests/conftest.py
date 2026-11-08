import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture
def stack():
    from mocks.local_stack import LocalStack
    s = LocalStack()
    yield s
    s.close()


def warm(stack, device="wl-1", n=12):
    for _ in range(n):
        stack.twin.normal(device)
