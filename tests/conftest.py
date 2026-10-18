from collections.abc import Iterator

import pytest

from tests.helpers import OPEN


@pytest.fixture(autouse=True)
def _close_connections() -> Iterator[None]:
    yield
    while OPEN:
        OPEN.pop().close()
