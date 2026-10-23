"""Shared closed vocabulary for manual and extracted teaching-style feedback."""
from typing import Literal

StyleAspect = Literal["rhythm", "wording", "interaction", "examples", "humor",
    "warmth", "persona", "structure", "detail", "terminology", "notation",
    "transitions", "recap", "questioning"]
StylePreference = Literal["more", "less", "keep", "clearer", "slower", "faster",
    "simpler", "deeper", "warmer", "more_direct"]
