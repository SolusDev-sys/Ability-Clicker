#!/usr/bin/env python3
"""Click LMB / RMB when an ability icon's border matches its "ready" color.

Watches small screen regions (ability icons) and presses the mapped mouse
button whenever the region's color falls in the configured "ready" range.

Install:
    pip install opencv-python numpy mss pynput

Usage:
    python ability_clicker.py --pick   # drag boxes over icons, prints config values
    python ability_clicker.py --dry    # detect only, no clicking (for tuning)
    python ability_clicker.py          # run for real

Hotkeys while running:
    F8 = pause / resume        F9 = quit
"""
import argparse
import ctypes
import sys
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from mss import MSS
from pynput import keyboard
from pynput.mouse import Button
from pynput.mouse import Controller as Mouse

# ----------------------------- CONFIG ---------------------------------------
POLL_INTERVAL = 0.01  # seconds between checks (~100 Hz)
HOLD_TIME = 0.02  # seconds each click is held; some games miss 0 ms clicks

PROCESS_PER_MONITOR_DPI_AWARE = 2  # value for SetProcessDpiAwareness
PICK_VIEW_MAX_WIDTH = 1600  # downscale the --pick screenshot to this width
PICK_HSV_TOLERANCE = np.array([10, 60, 60])  # +/- around the picked mean HSV

# The ROI is a thin strip along the TOP EDGE of each icon's cream border
# (about 40-50 px wide, 3-4 px tall, fully inside the border). Get the
# coordinates with `--pick` (only use its roi output; the HSV below is
# already tuned from a screenshot of the game).
#
# Measured HSV (OpenCV scale):
#   ready border     H 16-21  S 76-114  V 103-226   <- bright cream
#   cooldown border  V 22-92                        <- dark
#   cooldown red wedge  H 0-6  S ~160  V <= 64      <- dark, so V >= 130
#                                                      excludes it
CREAM_LOWER = (10, 50, 130)
CREAM_UPPER = (30, 140, 255)


@dataclass
class Ability:
    """One watched icon: where to look, what "ready" looks like, what to press."""

    name: str
    button: Button
    roi: tuple[int, int, int, int]  # (left, top, width, height), screen pixels
    hsv_lower: tuple[int, int, int]  # OpenCV HSV: H 0-179, S 0-255, V 0-255
    hsv_upper: tuple[int, int, int]
    min_ratio: float = 0.6  # fraction of ROI pixels that must match
    min_interval: float = 0.08  # seconds between presses of this ability

    # Runtime state, not configuration.
    last_press: float = field(default=0.0, init=False)
    was_ready: bool = field(default=False, init=False)
    lower_bound: np.ndarray = field(init=False, repr=False, compare=False)
    upper_bound: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Build the arrays once instead of on every poll.
        self.lower_bound = np.array(self.hsv_lower)
        self.upper_bound = np.array(self.hsv_upper)


def default_abilities() -> list[Ability]:
    """Return the built-in ability list. Replace the ROIs with --pick output."""
    return [
        Ability(
            "LMB",
            Button.left,
            roi=(644, 960, 68, 4),
            hsv_lower=CREAM_LOWER,
            hsv_upper=CREAM_UPPER,
        ),
        Ability(
            "RMB",
            Button.right,
            roi=(1210, 960, 61, 4),
            hsv_lower=CREAM_LOWER,
            hsv_upper=CREAM_UPPER,
        ),
    ]


# ----------------------------- SETUP ----------------------------------------
def enable_dpi_awareness() -> None:
    """Make screen coordinates match real pixels on scaled Windows displays."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except (AttributeError, OSError):
        pass  # Older Windows: fall back to default DPI handling.


# ----------------------------- DETECTION ------------------------------------
def ready_ratio(sct: MSS, ability: Ability) -> float:
    """Return the fraction of ROI pixels inside the ability's ready color range."""
    left, top, width, height = ability.roi
    shot = np.asarray(
        sct.grab({"left": left, "top": top, "width": width, "height": height})
    )
    bgr = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, ability.lower_bound, ability.upper_bound)
    return cv2.countNonZero(mask) / mask.size


def click(mouse: Mouse, button: Button) -> None:
    """Press and release a mouse button, holding it briefly."""
    mouse.press(button)
    time.sleep(HOLD_TIME)
    mouse.release(button)


# ----------------------------- MAIN LOOP ------------------------------------
def run(abilities: list[Ability], dry: bool) -> None:
    """Poll every ability and click when it becomes ready, until F9 is pressed."""
    mouse = Mouse()
    enabled = threading.Event()
    enabled.set()
    stop = threading.Event()

    def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
        if key == keyboard.Key.f8:
            if enabled.is_set():
                enabled.clear()
                print("PAUSED")
            else:
                enabled.set()
                print("RESUMED")
        elif key == keyboard.Key.f9:
            stop.set()
            return False  # stops the listener
        return None

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    print(f"Running ({'DRY RUN' if dry else 'LIVE'}). F8 = pause/resume, F9 = quit.")

    try:
        with MSS() as sct:
            while not stop.is_set():
                if enabled.is_set():
                    for ability in abilities:
                        process_ability(sct, mouse, ability, dry)
                stop.wait(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


def process_ability(sct: MSS, mouse: Mouse, ability: Ability, dry: bool) -> None:
    """Check one ability; report state changes (dry) or click it (live)."""
    ratio = ready_ratio(sct, ability)
    ready = ratio >= ability.min_ratio

    if dry:
        if ready != ability.was_ready:
            state = "READY" if ready else "cooldown"
            print(f"{ability.name}: {state} (match {ratio:.0%})")
    elif ready:
        now = time.perf_counter()  # fresh per ability; click() sleeps
        if now - ability.last_press >= ability.min_interval:
            click(mouse, ability.button)
            ability.last_press = now

    ability.was_ready = ready


# ----------------------------- ROI / COLOR PICKER ---------------------------
def pick_mode(abilities: list[Ability]) -> None:
    """Drag a box over each icon (while READY); print config values to paste in."""
    with MSS() as sct:
        monitor = sct.monitors[1]  # primary monitor
        image = cv2.cvtColor(np.asarray(sct.grab(monitor)), cv2.COLOR_BGRA2BGR)

    scale = min(1.0, PICK_VIEW_MAX_WIDTH / image.shape[1])
    view = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image

    for ability in abilities:
        print(
            f"\nDrag a small box over the {ability.name} icon in its READY "
            "state, then press ENTER (C to skip)."
        )
        x, y, w, h = cv2.selectROI(
            f"Select {ability.name}", view, showCrosshair=False
        )
        cv2.destroyAllWindows()
        if w == 0 or h == 0:
            continue
        x, y, w, h = (int(v / scale) for v in (x, y, w, h))

        hsv = cv2.cvtColor(image[y : y + h, x : x + w], cv2.COLOR_BGR2HSV)
        mean = hsv.reshape(-1, 3).mean(axis=0)
        lower = tuple(np.maximum(mean - PICK_HSV_TOLERANCE, 0).astype(int).tolist())
        upper = tuple(
            np.minimum(mean + PICK_HSV_TOLERANCE, [179, 255, 255]).astype(int).tolist()
        )

        print(f"  roi={(monitor['left'] + x, monitor['top'] + y, w, h)},")
        print(f"  hsv_lower={lower}, hsv_upper={upper}")
        print(f"  (mean HSV of selection: {tuple(int(v) for v in mean)})")


# ----------------------------- ENTRY POINT ----------------------------------
def main() -> None:
    """Parse arguments and run the requested mode."""
    enable_dpi_awareness()

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pick", action="store_true", help="select ROIs and print colors"
    )
    parser.add_argument(
        "--dry", action="store_true", help="detect only, never click"
    )
    args = parser.parse_args()

    abilities = default_abilities()
    if args.pick:
        pick_mode(abilities)
    else:
        run(abilities, dry=args.dry)


if __name__ == "__main__":
    main()