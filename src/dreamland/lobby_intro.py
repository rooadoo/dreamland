"""A bounded, synchronous connection introduction before lobby input begins."""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass

from dreamland.lobby_input import LobbyClosed, LobbyInput
from dreamland.lobby_intro_art import INTRO_ART, INTRO_ART_HEIGHT, INTRO_ART_WIDTH
from dreamland.lobby_presentation import CLEAR, LobbyPresentation, terminal_capabilities

LOGGER = logging.getLogger("dreamland.lifecycle")
INTRO_DURATION = 2.0
INTRO_FRAME_COUNT = 20
INTRO_MAX_BYTES = 16 * 1024
INTRO_MIN_COLUMNS = INTRO_ART_WIDTH + 2
INTRO_MIN_ROWS = INTRO_ART_HEIGHT + 2
INTRO_FRAMES = tuple(
    tuple(
        line[
            : ((frame + 1) * INTRO_ART_WIDTH + INTRO_FRAME_COUNT - 1)
            // INTRO_FRAME_COUNT
        ]
        for line in INTRO_ART
    )
    for frame in range(INTRO_FRAME_COUNT)
)


def render_intro_frame(
    frame: int, size: tuple[int, int], presentation: LobbyPresentation
) -> str:
    """Reveal fixed columns in a centered region without scrolling or queries."""
    width, height = min(size[0], 240), min(size[1], 100)
    left = (width - INTRO_ART_WIDTH) // 2 + 1
    top = (height - INTRO_ART_HEIGHT) // 2 + 1
    parts = []
    for row, line in enumerate(INTRO_FRAMES[frame]):
        parts.append(f"\x1b[{top + row};{left}H")
        # Color belongs to artwork columns, so revealed areas keep their color
        # as the edge advances. Monochrome uses exactly the same geometry.
        for band, role in enumerate(("heading", "reference", "action")):
            start = band * INTRO_ART_WIDTH // 3
            end = (band + 1) * INTRO_ART_WIDTH // 3
            parts.append(presentation.style(line[start:end], role))
    return "".join(parts)


@dataclass(frozen=True)
class IntroResult:
    reason: str
    frames: int = 0
    bytes_sent: int = 0


def run_intro(
    reader: LobbyInput,
    *,
    mode: str,
    terminal_type: object,
    presentation: LobbyPresentation,
    dimensions: Callable[[], tuple[int, int]],
) -> IntroResult:
    """Finish motion and input cleanup before the caller can draw a prompt.

    Only received bytes are discarded. Arbitrarily delayed paste cannot be
    distinguished from a new key by an SSH byte stream.
    """
    if mode != "auto":
        return IntroResult("off")
    capabilities = terminal_capabilities(terminal_type)
    if not (capabilities.motion and capabilities.clear):
        return IntroResult("unsupported")
    # The full-screen reveal needs a clean display at both boundaries. Honor
    # the operator's clearing override by proceeding directly to static text.
    if not presentation.clear:
        return IntroResult("clearing_off")
    size = dimensions()
    if size[0] < INTRO_MIN_COLUMNS or size[1] < INTRO_MIN_ROWS:
        return IntroResult("small_terminal")

    cleanup = presentation.reset + CLEAR
    cleanup_bytes = len(cleanup.encode("ascii"))
    started = reader.monotonic()
    deadline = started + INTRO_DURATION
    interval = INTRO_DURATION / INTRO_FRAME_COUNT
    frame_times = tuple(
        started + index * interval for index in range(INTRO_FRAME_COUNT)
    )
    last_frame = -1
    frames = 0
    sent = 0
    reason = "complete"
    LOGGER.debug(
        "SSH lobby introduction started width=%s height=%s duration_ms=2000",
        *size,
    )
    try:
        while True:
            reader.check()
            # Check ready input even when a previous write used the remaining
            # duration. Cancellation must not become a welcome continuation.
            key = reader.poll_key(reader.monotonic())
            if key is not None:
                reader.discard_boundary(bytes([key]))
                reason = "skipped"
                break
            if dimensions() != size:
                reason = "resized"
                break
            now = reader.monotonic()
            if now >= deadline:
                break
            frame = bisect_right(frame_times, now) - 1
            if frame > last_frame:
                payload = (cleanup if not frames else "") + render_intro_frame(
                    frame, size, presentation
                )
                payload_bytes = len(payload.encode("ascii"))
                if (
                    frames >= INTRO_FRAME_COUNT
                    or sent + payload_bytes + cleanup_bytes > INTRO_MAX_BYTES
                ):
                    reason = "budget"
                    break
                # A slow write advances the clock; the next iteration chooses
                # the current frame instead of replaying obsolete ones.
                sent += payload_bytes
                frames += 1
                last_frame = frame
                reader.send(payload)
                continue
            next_frame = (
                frame_times[frame + 1] if frame + 1 < len(frame_times) else deadline
            )
            key = reader.poll_key(next_frame)
            if key is not None:
                reader.discard_boundary(bytes([key]))
                reason = "skipped"
                break
        # Consume the finite boundary snapshot even on normal expiry/resize.
        # Empty drains do not count as activity or extend the idle deadline.
        reader.discard_boundary()
        reader.check()
    except LobbyClosed as exc:
        reason = exc.reason
        raise
    except (OSError, EOFError):
        reason = "disconnect"
        raise
    except Exception:
        reason = "failure"
        raise
    finally:
        if frames and not reader.channel.closed:
            try:
                reader.send(cleanup)
                sent += cleanup_bytes
            except (OSError, EOFError):
                pass
        LOGGER.debug(
            "SSH lobby introduction reason=%s frames=%s bytes=%s elapsed_ms=%s",
            reason,
            frames,
            sent,
            round((reader.monotonic() - started) * 1000),
        )
    return IntroResult(reason, frames, sent)
