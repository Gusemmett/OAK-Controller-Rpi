#!/usr/bin/env python3
"""
OAK Camera USB detection module.

Provides functions to detect connected OAK cameras via DepthAI without booting them.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import depthai as dai

logger = logging.getLogger(__name__)


@dataclass
class OAKDeviceInfo:
    """Information about a detected OAK device."""
    mxid: str
    state: str
    name: Optional[str] = None


def detect_oak_cameras() -> List[OAKDeviceInfo]:
    """
    Detect all available OAK cameras connected via USB.

    This function uses DepthAI's getAllAvailableDevices() which queries
    connected devices without booting them.

    Returns:
        List of OAKDeviceInfo for each detected camera.
        Empty list if no cameras found or on error.
    """
    try:
        devices = dai.Device.getAllAvailableDevices()
        result = []

        for device_info in devices:
            oak_info = OAKDeviceInfo(
                mxid=device_info.getMxId(),
                state=str(device_info.state),
                name=getattr(device_info, 'name', None)
            )
            result.append(oak_info)
            logger.debug(f"Detected OAK device: {oak_info.mxid} (state: {oak_info.state})")

        return result

    except Exception as e:
        logger.error(f"Error detecting OAK cameras: {e}")
        return []


def is_oak_camera_connected() -> bool:
    """
    Check if at least one OAK camera is connected and available.

    Returns:
        True if a camera is connected, False otherwise.
    """
    devices = detect_oak_cameras()
    connected = len(devices) > 0

    if connected:
        logger.debug(f"OAK camera connected: {devices[0].mxid}")
    else:
        logger.debug("No OAK camera detected")

    return connected


def get_primary_oak_device() -> Optional[OAKDeviceInfo]:
    """
    Get the primary (first) OAK device if available.

    Returns:
        OAKDeviceInfo for the first detected device, or None if no devices.
    """
    devices = detect_oak_cameras()
    return devices[0] if devices else None
