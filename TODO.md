# TODO

(nothing open)

## Done

- [x] **dB-mode volume control.** Dragging/+/- in dB mode used to overshoot and
      pin to max, because the dB→% conversion assumed PVOL spanned the limited
      range. Fixed by setting volume in dB directly via `Z1VOL` (the unit accepts
      writes, 0.5 dB steps — verified): `POST /api/volume {"db": -50}` →
      `set_volume_db`. dB mode now sends exact dB for slider/nudge/manual entry;
      % mode still uses PVOL.

- [x] **Listening-mode selection.** Reading worked but setting always failed:
      `anthemav` sends a zero-padded value (`Z1ALM06`) which the AVM 70/90
      rejects with `!E`; the x40 firmware wants the un-padded form (`Z1ALM6`,
      verified on the unit). The controller now sends `Z1ALM<n>` directly. Note:
      the mode is still only changeable while a live audio signal is present
      (the receiver locks it to "None" when idle).

- [x] **Maximum volume limit (query + set).** Command: `GCMMV` (main-zone max
      volume in dB, 0.5 dB steps; `GCZ2MMV` is Zone 2). Registered in the
      anthemav lookup table; exposed as `max_volume_db` in the receiver state;
      `POST /api/max_volume` sets it. The volume slider scales its dB range to
      this value, and the Settings "Maximum volume" field reads/writes it.
