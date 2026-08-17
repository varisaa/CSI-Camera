# MIT License
# Copyright (c) 2019-2022 JetsonHacks

# Using a CSI camera (such as the Raspberry Pi Version 2) connected to a
# NVIDIA Jetson Nano Developer Kit using OpenCV
# Drivers for the camera and OpenCV are included in the base image

import argparse
import re
import subprocess
import cv2

# Maps the hex control IDs guvcview writes into its .gpfl profile files
# (File > Save Control Profile, e.g. ~/work/camera-settings/*.gpfl) to the
# v4l2-ctl control names used elsewhere in this file. These are the
# Global Shutter Camera's standard UVC control IDs (v4l2-ctl
# --list-ctrls-menus). "Privacy" (0x009a0910) is deliberately excluded -
# this camera errors querying it ("error 32 getting ext_ctrl Privacy") and
# guvcview saves a nonsense value for it (a leftover white-balance-Kelvin
# number), so it's skipped rather than fed to v4l2-ctl.
GPFL_CONTROL_MAP = {
    "0x00980900": "brightness",
    "0x00980901": "contrast",
    "0x00980902": "saturation",
    "0x00980903": "hue",
    "0x0098090c": "white_balance_automatic",
    "0x00980910": "gamma",
    "0x00980913": "gain",
    "0x00980918": "power_line_frequency",
    "0x0098091a": "white_balance_temperature",
    "0x0098091b": "sharpness",
    "0x0098091c": "backlight_compensation",
    "0x009a0901": "auto_exposure",
    "0x009a0902": "exposure_time_absolute",
    "0x009a0903": "exposure_dynamic_framerate",
    "0x009a0908": "pan_absolute",
    "0x009a0909": "tilt_absolute",
    "0x009a090a": "focus_absolute",
    "0x009a090c": "focus_automatic_continuous",
    "0x009a090d": "zoom_absolute",
}

# Controls that switch another control between auto/manual - these have to
# be applied in a separate, earlier v4l2-ctl call than the values that
# depend on them (see the long comment in gstreamer_pipeline_usb_yuyv).
GPFL_MODE_CONTROLS = {"auto_exposure", "white_balance_automatic", "focus_automatic_continuous"}


def parse_gpfl_profile(path):
    """Parse a guvcview .gpfl profile file into {control_name: value}."""
    controls = {}
    line_re = re.compile(r"ID\{(0x[0-9a-fA-F]+)\}.*=VAL\{(-?\d+)\}")
    with open(path) as f:
        for line in f:
            m = line_re.search(line)
            if not m:
                continue
            control_id, value = m.group(1).lower(), m.group(2)
            name = GPFL_CONTROL_MAP.get(control_id)
            if name:
                controls[name] = value
    return controls


# Dependent-value controls that this camera reports as read-only/inactive
# whenever their governing mode control is in its "auto" state - maps each
# to (mode control, the mode value that makes it inactive). Skipped from
# the batch below when that condition holds, since writing them then is a
# no-op at best and a noisy "Permission denied" at worst (seen for
# white_balance_temperature specifically; harmless either way, but skipping
# keeps the output clean and matches what --list-ctrls-menus itself calls
# out via its "flags=inactive" marker).
GPFL_INACTIVE_WHEN = {
    "white_balance_temperature": ("white_balance_automatic", "1"),
    "exposure_time_absolute": ("auto_exposure", "3"),
    "focus_absolute": ("focus_automatic_continuous", "1"),
}


def apply_v4l2_profile(device, controls):
    """Apply a {control_name: value} dict via v4l2-ctl, in two passes so
    mode-switch controls (auto_exposure etc.) land before the values that
    depend on them - see the comment in gstreamer_pipeline_usb_yuyv for why
    this camera needs that split. Silently drops dependent values that the
    profile itself leaves inactive (see GPFL_INACTIVE_WHEN)."""
    controls = {
        k: v
        for k, v in controls.items()
        if not (
            k in GPFL_INACTIVE_WHEN
            and controls.get(GPFL_INACTIVE_WHEN[k][0]) == GPFL_INACTIVE_WHEN[k][1]
        )
    }
    mode_ctrls = {k: v for k, v in controls.items() if k in GPFL_MODE_CONTROLS}
    other_ctrls = {k: v for k, v in controls.items() if k not in GPFL_MODE_CONTROLS}
    for group in (mode_ctrls, other_ctrls):
        if not group:
            continue
        set_arg = "--set-ctrl=" + ",".join("%s=%s" % (k, v) for k, v in group.items())
        subprocess.run(["v4l2-ctl", "-d", device, set_arg], check=False)

"""
gstreamer_pipeline_csi returns a GStreamer pipeline for capturing from a CSI camera
Flip the image by setting the flip_method (most common values: 0 and 2)
display_width and display_height determine the size of the window on the screen
Default 1920x1080 displayd in a 1/4 size window
"""

def gstreamer_pipeline_csi(
    sensor_id=0,
    capture_width=1920,
    capture_height=1080,
    display_width=960,
    display_height=540,
    framerate=60,
    flip_method=0,
):
    return (
        "nvarguscamerasrc sensor-id=%d ! "
        "video/x-raw(memory:NVMM), width=(int)%d, height=(int)%d, framerate=(fraction)%d/1 ! "
        "nvvidconv flip-method=%d ! "
        "video/x-raw, width=(int)%d, height=(int)%d, format=(string)BGRx ! "
        "videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (
            sensor_id,
            capture_width,
            capture_height,
            framerate,
            flip_method,
            display_width,
            display_height,
        )
    )


"""
gstreamer_pipeline_usb returns a GStreamer pipeline for capturing from a USB (UVC) camera
via v4l2src, decoding its MJPG stream.
"""

def gstreamer_pipeline_usb(
    device="/dev/v4l/by-id/usb-Global_Shutter_Camera_Global_Shutter_Camera_2602040001-video-index0",
    capture_width=1920,
    capture_height=1080,
    framerate=30,
    profile=None,
):
    # 60Hz anti-flicker (US mains) - resets to the driver default (50Hz) on
    # unplug/reboot, so it needs to be reapplied here every time.
    subprocess.run(
        ["v4l2-ctl", "-d", device, "--set-ctrl=power_line_frequency=2"],
        check=False,
    )
    if profile:
        apply_v4l2_profile(device, parse_gpfl_profile(profile))
    return (
        "v4l2src device=%s ! "
        "image/jpeg, width=(int)%d, height=(int)%d, framerate=(fraction)%d/1 ! "
        "jpegdec ! videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (device, capture_width, capture_height, framerate)
    )


"""
gstreamer_pipeline_usb_yuyv returns a GStreamer pipeline for capturing uncompressed
YUYV from a USB (UVC) camera - no MJPG/jpegdec involved. Needs a SuperSpeed (USB3)
link at the camera's max resolution; see ai-context/cheatsheet.md for bandwidth notes.
"""

def gstreamer_pipeline_usb_yuyv(
    device="/dev/v4l/by-id/usb-Global_Shutter_Camera_Global_Shutter_Camera_2602040001-video-index0",
    capture_width=1920,
    capture_height=1080,
    framerate=30,
    profile=None,
):
    subprocess.run(
        ["v4l2-ctl", "-d", device, "--set-ctrl=power_line_frequency=2"],
        check=False,
    )
    # IMPORTANT: controls must be set before the device is opened for
    # streaming below - v4l2-ctl writes from a second process while
    # v4l2src already has the device open are silently accepted (readback
    # shows the new value) but never reach the sensor.
    #
    # Mode-switch controls (auto_exposure, focus_automatic_continuous, ...)
    # have to land in a separate, earlier v4l2-ctl call than the absolute
    # values that depend on manual mode being active - setting all of them
    # in one combined --set-ctrl intermittently fails outright ("Permission
    # denied" from VIDIOC_S_EXT_CTRLS, nothing applied) on this camera's
    # firmware. apply_v4l2_profile() below handles that split.
    if profile:
        # --profile PATH was passed: use exactly what's in that .gpfl file
        # (e.g. one saved from guvcview - File > Save Control Profile, or
        # a canned one under ~/work/camera-settings/) instead of the
        # built-in tuning below.
        apply_v4l2_profile(device, parse_gpfl_profile(profile))
    else:
        # Tuned 2026-08-10 against an outdoor backlit scene (shaded deck,
        # bright sky beyond) at this exact resolution - see
        # ai-context/usb-camera-settings-log.md for the before/after
        # evaluation. auto_exposure=3 (the driver default) pins exposure
        # at its ceiling in backlit scenes, blowing out highlights and
        # causing purple fringing at clipped edges; auto-continuous focus
        # didn't converge as sharp as a fixed manual point on this test
        # subject. Reasonable general-purpose defaults but not
        # scene-proof - pass --profile with a scene-specific .gpfl
        # instead if this doesn't suit what's in frame.
        apply_v4l2_profile(
            device,
            {
                "auto_exposure": 1,
                "focus_automatic_continuous": 0,
                "exposure_time_absolute": 800,
                "gain": 160,
                "focus_absolute": 600,
            },
        )
    return (
        "v4l2src device=%s ! "
        "video/x-raw, format=(string)YUY2, width=(int)%d, height=(int)%d, framerate=(fraction)%d/1 ! "
        "videoconvert ! "
        "video/x-raw, format=(string)BGR ! appsink"
        % (device, capture_width, capture_height, framerate)
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Show a single camera feed")
    parser.add_argument(
        "camera",
        nargs="?",
        default="0",
        help="Which camera to show: '0' or '1' (CSI sensor-id), or 'usb' (USB webcam). Default: 0",
    )
    parser.add_argument(
        "--usb-device",
        default="/dev/v4l/by-id/usb-Global_Shutter_Camera_Global_Shutter_Camera_2602040001-video-index0",
        help="V4L2 device path to use when camera='usb'. Default: Global Shutter Camera (stable by-id path).",
    )
    parser.add_argument(
        "--yuyv",
        action="store_true",
        help="Capture uncompressed YUYV at the USB camera's max resolution (2592x1944@30) "
             "instead of the default 1920x1080 MJPG. Requires a SuperSpeed (USB3) link.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Path to a guvcview .gpfl control profile (e.g. one under "
             "~/work/camera-settings/, or one you saved yourself via guvcview's "
             "File > Save Control Profile) to apply to the USB camera before "
             "opening it, instead of this script's built-in tuning. Only "
             "affects camera='usb'.",
    )
    return parser.parse_args()


def show_camera(camera, usb_device, yuyv=False, profile=None):
    if camera == "usb":
        window_title = "USB Camera (YUYV 2592x1944)" if yuyv else "USB Camera"
        pipeline = (
            gstreamer_pipeline_usb_yuyv(device=usb_device, profile=profile)
            if yuyv
            else gstreamer_pipeline_usb(device=usb_device, profile=profile)
        )
    else:
        sensor_id = int(camera)
        window_title = "CSI Camera %d" % sensor_id
        pipeline = gstreamer_pipeline_csi(sensor_id=sensor_id, flip_method=0)

    print(pipeline)
    video_capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if video_capture.isOpened():
        try:
            window_handle = cv2.namedWindow(window_title, cv2.WINDOW_AUTOSIZE)
            while True:
                ret_val, frame = video_capture.read()
                # Check to see if the user closed the window
                # Under GTK+ (Jetson Default), WND_PROP_VISIBLE does not work correctly. Under Qt it does
                # GTK - Substitute WND_PROP_AUTOSIZE to detect if window has been closed by user
                if cv2.getWindowProperty(window_title, cv2.WND_PROP_AUTOSIZE) >= 0:
                    cv2.imshow(window_title, frame)
                else:
                    break
                keyCode = cv2.waitKey(10) & 0xFF
                # Stop the program on the ESC key or 'q'
                if keyCode == 27 or keyCode == ord('q'):
                    break
        finally:
            video_capture.release()
            cv2.destroyAllWindows()
    else:
        print("Error: Unable to open camera")


if __name__ == "__main__":
    args = parse_args()
    show_camera(args.camera, args.usb_device, args.yuyv, args.profile)
