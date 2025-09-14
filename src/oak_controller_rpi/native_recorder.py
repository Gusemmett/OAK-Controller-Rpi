#!/usr/bin/env python3

import asyncio
import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from enum import Enum

import depthai as dai

logger = logging.getLogger(__name__)

class RecorderState(Enum):
    STOPPED = "stopped"
    INITIALIZING = "initializing"
    READY = "ready"
    RECORDING = "recording"
    ERROR = "error"

class VideoSaver(dai.node.HostNode):
    """Host node for saving H.265 video data to file"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filename = None
        self._fh = None

    def build(self, *link_args, filename="video.h265"):
        self.link_args(*link_args)
        self.filename = filename
        return self

    def process(self, pkt):
        if self._fh is None:
            os.makedirs(os.path.dirname(self.filename) or ".", exist_ok=True)
            self._fh = open(self.filename, "wb")
        pkt.getData().tofile(self._fh)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

class TsLogger(dai.node.HostNode):
    """Host node for logging timestamps and frame indices"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = None
        self._w = None
        self._f = None

    def build(self, *link_args, path="stream.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def process(self, pkt):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            self._w.writerow(["ts_ns", "frame_idx"])
        
        # Get device timestamp, fallback to host timestamp
        ts = getattr(pkt, "getTimestampDevice", None)
        ts = ts() if ts else pkt.getTimestamp()
        ts_ns = int(ts.total_seconds() * 1e9)
        self._w.writerow([ts_ns, pkt.getSequenceNum()])

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None

class NativeOAKRecorder:
    """Simplified OAK stereo recorder: build, start, stop. No warmup, no IMU."""

    def __init__(self,
                 width: int = 1280,
                 height: int = 720,
                 fps: float = 30.0,
                 is_camera_upside_down: bool = True):

        self.width = width
        self.height = height
        self.fps = fps
        # Assign sockets based on orientation. Normal: left=B, right=C. Upside down: swap.
        if is_camera_upside_down:
            left_socket_name = "CAM_C"
            right_socket_name = "CAM_B"
        else:
            left_socket_name = "CAM_B"
            right_socket_name = "CAM_C"

        self.left_socket = self._parse_socket(left_socket_name)
        self.right_socket = self._parse_socket(right_socket_name)
        self.is_camera_upside_down = is_camera_upside_down

        # State
        self.state = RecorderState.STOPPED
        self._pipeline: Optional[dai.Pipeline] = None
        self._savers: Dict[str, Any] = {}
        self._loggers: Dict[str, Any] = {}
        self._output_dir: Optional[Path] = None
        self._recording_start_time: Optional[float] = None

    def _parse_socket(self, name: str) -> dai.CameraBoardSocket:
        try:
            return getattr(dai.CameraBoardSocket, name)
        except AttributeError:
            raise ValueError(f"Invalid socket {name}. Use CAM_A, CAM_B, CAM_C, CAM_D.")

    def _build_pipeline(self) -> Tuple[dai.Pipeline, Dict[str, Any]]:
        """Build a simple DepthAI pipeline with Sync + Demux + H.264 encoders."""
        logger.info("Building DepthAI pipeline...")

        p = dai.Pipeline()

        camL = p.create(dai.node.Camera).build(self.left_socket, sensorFps=self.fps)
        camR = p.create(dai.node.Camera).build(self.right_socket, sensorFps=self.fps)

        outL = camL.requestOutput((self.width, self.height), type=dai.ImgFrame.Type.RAW8, fps=self.fps)
        outR = camR.requestOutput((self.width, self.height), type=dai.ImgFrame.Type.RAW8, fps=self.fps)

        # Optionally flip images if the camera is mounted upside down
        sourceL = outL
        sourceR = outR
        if self.is_camera_upside_down:
            manipL = p.create(dai.node.ImageManip)
            manipL.initialConfig.addFlipVertical()
            manipL.initialConfig.addFlipHorizontal()
            outL.link(manipL.inputImage)
            sourceL = manipL.out

            manipR = p.create(dai.node.ImageManip)
            manipR.initialConfig.addFlipVertical()
            manipR.initialConfig.addFlipHorizontal()
            outR.link(manipR.inputImage)
            sourceR = manipR.out

        sync = p.create(dai.node.Sync)
        sourceL.link(sync.inputs["left"])
        sourceR.link(sync.inputs["right"])

        demux = p.create(dai.node.MessageDemux)
        sync.out.link(demux.input)

        encL = p.create(dai.node.VideoEncoder).build(
            demux.outputs["left"], frameRate=self.fps,
            profile=dai.VideoEncoderProperties.Profile.H264_MAIN
        )
        encR = p.create(dai.node.VideoEncoder).build(
            demux.outputs["right"], frameRate=self.fps,
            profile=dai.VideoEncoderProperties.Profile.H264_MAIN
        )

        saverL = p.create(VideoSaver).build(encL.out)
        saverR = p.create(VideoSaver).build(encR.out)
        loggerL = p.create(TsLogger).build(encL.out)
        loggerR = p.create(TsLogger).build(encR.out)

        nodes = {"saverL": saverL, "saverR": saverR, "loggerL": loggerL, "loggerR": loggerR}
        logger.info("DepthAI pipeline built")
        return p, nodes

    def _dump_calibration_from_running_device(self):
        """Export calibration with intrinsics adjusted for vertical flip."""
        if not self._output_dir:
            logger.error("No output directory set for calibration export")
            return

        calibration_path = self._output_dir / "calibration.json"
        data: Dict[str, Any] = {}

        try:
            with dai.Device() as cal_device:
                cal = cal_device.readCalibration()

                logger.info(cal)
                logger.info(cal.getStereoRightCameraId())

                def cam_block(sock):
                    # Intrinsics
                    if hasattr(cal, "getCameraIntrinsics"):
                        K = cal.getCameraIntrinsics(sock, self.width, self.height)
                    elif hasattr(cal, "getCameraMatrix"):
                        try:
                            K = cal.getCameraMatrix(sock, self.width, self.height)
                        except Exception:
                            K = cal.getCameraMatrix(sock)
                    else:
                        K = None

                    # Distortion
                    dist = None
                    for name in ("getDistortionCoefficients", "getDistortionCoeffs", "getDistortion"):
                        if hasattr(cal, name):
                            dist = getattr(cal, name)(sock)
                            break

                    fov = cal.getFov(sock) if hasattr(cal, "getFov") else None

                    cam = {
                        "socket": getattr(sock, "name", str(sock)),
                        "width": self.width,
                        "height": self.height,
                        "intrinsics": K,
                        "distortion": dist,
                        "fov_deg": fov,
                    }

                    return cam

                data = {
                    "device_mxid": cal_device.getMxId() if hasattr(cal_device, "getMxId") else None,
                    "right": cam_block(self.left_socket),
                    "left": cam_block(self.right_socket),
                }

                # Extrinsics left->right unchanged
                if hasattr(cal, "getCameraExtrinsics"):
                    try:
                        E = cal.getCameraExtrinsics(self.right_socket, self.left_socket)
                        if E:
                            R = [row[:3] for row in E[:3]]
                            t = [E[0][3], E[1][3], E[2][3]]
                            data["extrinsics_left_to_right"] = {"R": R, "T": t, "matrix_4x4": E}
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Failed to read calibration from device: {e}")
            data = {"error": f"Failed to read calibration from device: {e}"}

        try:
            with open(calibration_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Calibration data saved to {calibration_path}")
        except Exception as e:
            logger.error(f"Failed to write calibration file: {e}")
    
    async def initialize_cameras(self, output_dir: Path) -> bool:
        """Build the pipeline and prepare file paths. No device warmup."""
        def _sync_init():
            try:
                logger.info("=== INITIALIZING CAMERAS ===")
                self.state = RecorderState.INITIALIZING
                self._output_dir = output_dir

                # Build pipeline
                self._pipeline, nodes = self._build_pipeline()
                self._savers = {k: v for k, v in nodes.items() if 'saver' in k}
                self._loggers = {k: v for k, v in nodes.items() if 'logger' in k}

                # Ensure output directory exists
                output_dir.mkdir(parents=True, exist_ok=True)

                # Set file paths (.h264 + .csv)
                left_h264 = output_dir / "left.h264"
                right_h264 = output_dir / "right.h264"
                left_csv = output_dir / "left.csv"
                right_csv = output_dir / "right.csv"

                nodes['saverL'].filename = str(left_h264)
                nodes['saverR'].filename = str(right_h264)
                nodes['loggerL'].path = str(left_csv)
                nodes['loggerR'].path = str(right_csv)

                self.state = RecorderState.READY
                logger.info("Initialization complete. Recorder READY.")
                return True

            except Exception as e:
                logger.error(f"Failed to initialize cameras: {e}")
                self.state = RecorderState.ERROR
                return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_init)
    
    async def warmup_cameras(self, warmup_duration: float = 0.0) -> bool:
        if not self._pipeline:
            logger.error("Pipeline must be initialized before warmup")
            return False
        # Cuurently no-op. Immediately mark as ready
        self.state = RecorderState.READY
        return True
    
    def start_recording(self) -> bool:
        """Start recording by starting the pipeline."""
        try:
            if not self._pipeline:
                raise RuntimeError("Pipeline not initialized. Call initialize_cameras first.")
            self._pipeline.start()
            self.state = RecorderState.RECORDING
            self._recording_start_time = time.time()
            logger.info("Recording started")
            return True
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self.state = RecorderState.ERROR
            return False
    
    def stop_recording(self) -> bool:
        """Stop recording, close files, and reset state."""
        try:
            if self._pipeline and hasattr(self._pipeline, 'isRunning') and self._pipeline.isRunning():
                self._pipeline.stop()
                self._pipeline.wait()

            # Close host nodes
            for saver in self._savers.values():
                try:
                    saver.close()
                except Exception:
                    pass
            for logger_node in self._loggers.values():
                try:
                    logger_node.close()
                except Exception:
                    pass

            # Dump calibration after stopping pipeline
            try:
                self._dump_calibration_from_running_device()
            except Exception as e:
                logger.warning(f"Calibration dump failed: {e}")

            self.state = RecorderState.STOPPED
            logger.info("Recording stopped")
            return True
        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            self.state = RecorderState.ERROR
            return False
    
    def get_state(self) -> Dict[str, Any]:
        current_time = time.time()
        timing: Dict[str, float] = {}
        if self._recording_start_time and self.state == RecorderState.RECORDING:
            timing['recording_elapsed'] = current_time - self._recording_start_time
        return {
            'state': self.state.value,
            'output_dir': str(self._output_dir) if self._output_dir else None,
            'timing': timing,
            'config': {
                'width': self.width,
                'height': self.height,
                'fps': self.fps
            }
        }
    
    def cleanup(self):
        if self.state == RecorderState.RECORDING:
            self.stop_recording()
        self._pipeline = None
        self._savers = {}
        self._loggers = {}
        self._output_dir = None

    @staticmethod
    def flip_K_180(K, W, H):
        K = [row[:] for row in K]
        K[0][2] = (W - 1) - K[0][2]   # cx'
        K[1][2] = (H - 1) - K[1][2]   # cy'
        K[0][0] = -K[0][0]            # fx'
        K[1][1] = -K[1][1]            # fy'
        K[0][1] = -K[0][1]            # s'
        return K