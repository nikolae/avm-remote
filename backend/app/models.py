"""Pydantic schemas for receiver state and command payloads."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Input(BaseModel):
    """A selectable source input on the receiver."""

    number: int
    name: str


class ReceiverState(BaseModel):
    """Full snapshot of the main-zone state pushed to clients.

    Mirrors the subset of the anthemav protocol we expose in the UI. Fields are
    deliberately tolerant of an unpowered/uninitialised receiver (empty strings,
    sensible defaults) so the frontend can always render something.
    """

    connected: bool = False
    model: str = ""
    power: bool = False
    volume: int = 0  # 0-100 (PVOL on x40)
    volume_db: float | None = None  # actual attenuation in dB (Z1VOL), if reported
    max_volume_db: float | None = None  # main-zone max volume limit (GCMMV), if known
    mute: bool = False

    input_number: int = 0
    input_name: str = ""
    inputs: list[Input] = Field(default_factory=list)

    listening_mode: str = ""
    listening_modes: list[str] = Field(default_factory=list)

    # Now-playing / status (read-only)
    audio_format: str = ""
    audio_channels: str = ""
    audio_input_name: str = ""
    sample_rate: int | None = None
    video_resolution: str = ""


# --- Command payloads ---------------------------------------------------------


class PowerCommand(BaseModel):
    on: bool


class VolumeCommand(BaseModel):
    # Either set an absolute level (0-100) or nudge by a relative step.
    level: int | None = Field(default=None, ge=0, le=100)
    step: int | None = None


class MuteCommand(BaseModel):
    # Omit `on` to toggle the current state.
    on: bool | None = None


class InputCommand(BaseModel):
    number: int = Field(ge=1, le=99)


class ModeCommand(BaseModel):
    # Listening mode by its display name (e.g. "Dolby Surround").
    mode: str


class MaxVolumeCommand(BaseModel):
    # Main-zone maximum volume limit, in dB (the receiver uses 0.5 dB steps).
    db: float = Field(ge=-90, le=10)
