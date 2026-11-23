"""Media is the single writer; control binds each lease to one healthy revision."""

from types import SimpleNamespace


class CaptureHealth:
    def __init__(self, context=None):
        self._state = context.Value("Q", 1) if context else SimpleNamespace(value=1)

    def update(self, healthy):
        revision = self._state.value
        if (revision % 2 == 0) != healthy:
            self._state.value = revision + 1

    def permit(self):
        revision = self._state.value
        return revision if revision % 2 == 0 else None
