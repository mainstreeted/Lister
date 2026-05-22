from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from ..config import Criteria
from ..models import Job


class Connector(ABC):
    """One per platform. Yields normalized Job objects."""

    platform_name: str

    @abstractmethod
    def discover(self, criteria: Criteria) -> Iterable[Job]:
        """Yield every job currently visible on this platform that's worth considering.

        Connectors do minimal filtering (e.g. location/title keyword match) — the
        real ranking happens later. The goal here is to cast a wide-but-relevant net.
        """
        ...
