"""Which subtasks need something other than text, and who can actually make it.

AETHER's capability dimensions were all text-work — coding, reasoning, review,
testing, architecture, research, terminal. So "generate a 10-second product
video" scored as *coding* and went to whichever model writes the best Python,
which dutifully returned a paragraph describing a video. Plausible, confident,
and not the thing that was asked for. A silent wrong answer is the worst
failure mode this project recognises, so modality is treated as a hard gate
rather than another weight.

Two rules follow from "no fake integrations":

1. **A media subtask may only be routed to a provider that declares the
   modality.** Not "prefers" — may only. Scoring it softly would let a
   text-only model win on capability and hand back prose.
2. **A provider declares a modality only when AETHER can actually invoke it.**
   Gemini can generate video; AETHER cannot ask it to, because there is no
   standard endpoint for that behind our adapter. Declaring `video` on the
   strength of the vendor's abilities rather than ours would be exactly the
   fake integration the project forbids.

Which is why `image` is served today and `video` is not: `/images/generations`
is a widely implemented OpenAI-compatible endpoint, and video generation is
vendor-specific with no common shape. When nothing can serve a modality,
routing fails loudly with a reason naming what is missing.
"""

from __future__ import annotations

import re

# Capabilities that mean "not text". Kept separate from the scoring dimensions
# because they gate rather than rank.
IMAGE = "image"
VIDEO = "video"
AUDIO = "audio"

MEDIA_CAPABILITIES: frozenset[str] = frozenset({IMAGE, VIDEO, AUDIO})

# What AETHER can currently *invoke*, as opposed to what models can do.
# `/images/generations` is standard across OpenAI-compatible servers.
# Video and audio generation have no common endpoint, so a subtask needing them
# fails with ADAPTER_REQUIRED rather than being handed to a text model.
SERVED_MODALITIES: frozenset[str] = frozenset({IMAGE})

# Deterministic detection, for the same reason routing and staffing are: it
# must work when the planner's provider is down, and "why did this need an
# image model?" must have an answer.
#
# Anchored on a produce-verb plus a media noun. Bare nouns match far too much —
# "add a diagram to the README" is a writing task, and "fix the video player"
# is a coding task that merely contains the word video.
_PRODUCE = r"(?:generate|create|make|produce|render|draw|design|synthesize|compose|record)"

_PATTERNS: dict[str, re.Pattern[str]] = {
    IMAGE: re.compile(
        rf"\b{_PRODUCE}\b[^.]{{0,40}}?\b(image|picture|photo|illustration|artwork|"
        rf"logo|icon|thumbnail|poster|banner|render)s?\b",
        re.IGNORECASE,
    ),
    VIDEO: re.compile(
        rf"\b{_PRODUCE}\b[^.]{{0,40}}?\b(video|animation|clip|footage|movie|reel|"
        rf"trailer)s?\b",
        re.IGNORECASE,
    ),
    AUDIO: re.compile(
        rf"\b{_PRODUCE}\b[^.]{{0,40}}?\b(audio|voiceover|voice-over|narration|"
        rf"speech|soundtrack|music|podcast|jingle)s?\b",
        re.IGNORECASE,
    ),
}

# Phrases that name a medium while describing ordinary software work. Without
# these, "fix the video player" and "add image upload" would be routed to a
# media model that cannot write code.
_ABOUT_NOT_OF = re.compile(
    r"\b(player|upload|uploader|pipeline|codec|compression|streaming|thumbnailer|"
    r"gallery|carousel|api|endpoint|component|library|parser|encoder|decoder|"
    r"transcoder|cdn|storage|bucket)\b",
    re.IGNORECASE,
)


def detect_modalities(text: str) -> set[str]:
    """Which non-text outputs this work actually has to produce.

    Conservative on purpose. A false positive routes ordinary coding to an
    image model and fails the subtask; a false negative leaves today's
    behaviour, which is merely no worse than before.
    """
    if not text:
        return set()
    if _ABOUT_NOT_OF.search(text):
        # Building software that handles media is a coding task.
        return set()
    return {name for name, pattern in _PATTERNS.items() if pattern.search(text)}


def unserved(modalities: set[str]) -> set[str]:
    """Modalities nothing in AETHER can currently produce."""
    return {m for m in modalities if m not in SERVED_MODALITIES}


def describe_gap(modalities: set[str]) -> str:
    """Why a media subtask cannot be routed, in words that suggest a fix."""
    missing = sorted(unserved(modalities))
    if not missing:
        return ""
    return (
        f"needs {', '.join(missing)} output, which no adapter can produce yet. "
        f"AETHER can invoke image generation ('/images/generations'); "
        f"{'/'.join(missing)} generation has no standard endpoint behind the "
        f"OpenAI-compatible adapter. Routing this to a text model would return "
        f"a description instead of the artifact."
    )


def tag_modalities(subtasks: list) -> list[str]:
    """Add detected media capabilities to each subtask's requirements.

    Runs after planning so it covers both the LLM planner and the keyword
    fallback — a media subtask must be gated however the plan was produced.
    Returns the modalities found, for the caller to record.
    """
    found: set[str] = set()
    for sub in subtasks:
        needed = detect_modalities(f"{sub.title} {sub.description}")
        if not needed:
            continue
        found |= needed
        for m in sorted(needed):
            if m not in sub.required_capabilities:
                sub.required_capabilities.append(m)
    return sorted(found)
