class HouseHunterError(RuntimeError):
    """Expected, user-actionable HouseHunter failure."""


class SourceContractError(HouseHunterError):
    """A remote or cached source does not match the pinned contract."""


class BuildNotFoundError(HouseHunterError):
    """No usable published build exists."""


class CensusNoMatchError(HouseHunterError):
    """Census returned a valid empty address match list."""


class AmbiguousPlaceError(HouseHunterError):
    def __init__(self, query: str, candidates: list[dict[str, str]]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(f"Ambiguous place name: {query}")
