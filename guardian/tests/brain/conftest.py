import pytest


@pytest.fixture
def bstack():
    from mocks.brain_local_stack import BrainLocalStack
    s = BrainLocalStack()
    s.send(*s.twin.meta())
    yield s
    s.close()


@pytest.fixture
def make_bstack():
    made = []

    def _make(meta=True, **kw):
        from mocks.brain_local_stack import BrainLocalStack
        s = BrainLocalStack(**kw)
        if meta:
            s.send(*s.twin.meta())
        made.append(s)
        return s
    yield _make
    for s in made:
        s.close()
