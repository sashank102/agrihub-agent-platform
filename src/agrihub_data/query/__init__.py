"""Pure query functions over a species bundle.

Functions take an open :class:`agrihub_data.bundle.Bundle` (or only the
registry) and return typed rows; rows that are facts about a gene expose
``evidence()`` to become :class:`agrihub.state.EvidenceItem` objects.
"""
