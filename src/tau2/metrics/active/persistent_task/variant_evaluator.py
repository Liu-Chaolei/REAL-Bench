"""Post-run scoring for authored Gaia2 interaction variants."""

from __future__ import annotations


class InteractionVariantEvaluator:
    """Evaluate variant evidence without driving the scenario."""

    @staticmethod
    def score(controller, scoring=None):
        """Return the existing variant score from finalized controller evidence."""
        return controller._score(scoring)
