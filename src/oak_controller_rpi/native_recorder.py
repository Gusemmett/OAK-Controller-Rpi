#!/usr/bin/env python3

import asyncio
import csv
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from enum import Enum

import depthai as dai

logger = logging.getLogger(__name__)

class RecorderState(Enum):
    STOPPED = "stopped"
    INITIALIZING = "initializing" 
    WARMING_UP = "warming_up"
    READY = "ready"
    RECORDING = "recording"
    STOPPING = "stopping"
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

class IMUSaver(dai.node.HostNode):
    """Host node for saving IMU data to CSV"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = None
        self._w = None
        self._f = None

    def build(self, *link_args, path="imu.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def process(self, imuData):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            self._w.writerow(["ts_ns","gyro_x","gyro_y","gyro_z","accel_x","accel_y","accel_z"])

        # Process IMU packets
        for pkt in imuData.packets:
            accel = getattr(pkt, "acceleroMeter", None)
            gyro = getattr(pkt, "gyroscope", None)
            if accel is None or gyro is None:
                continue

            # Get timestamp
            ts_func = getattr(accel, "getTimestampDevice", None) or accel.getTimestamp
            ts = ts_func()
            ts_ns = int(ts.total_seconds() * 1e9)

            self._w.writerow([ts_ns, gyro.x, gyro.y, gyro.z, accel.x, accel.y, accel.z])

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None

class NativeOAKRecorder:
    """Native OAK camera recorder with warmup and synchronization capabilities"""
    
    def __init__(self, 
                 width: int = 1280,
                 height: int = 720,
                 fps: float = 25.0,
                 bitrate_kbps: int = 4000,
                 left_socket: str = "CAM_B",
                 right_socket: str = "CAM_C",
                 imu_rate_hz: int = 200):
        
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self.left_socket = self._parse_socket(left_socket)
        self.right_socket = self._parse_socket(right_socket)
        self.imu_rate_hz = imu_rate_hz
        
        # State management
        self.state = RecorderState.STOPPED
        self._pipeline: Optional[dai.Pipeline] = None
        self._savers = {}
        self._loggers = {}
        self._output_dir: Optional[Path] = None
        self._recording_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Performance tracking
        self._init_start_time: Optional[float] = None
        self._warmup_start_time: Optional[float] = None
        self._recording_start_time: Optional[float] = None
        
    def _parse_socket(self, name: str) -> dai.CameraBoardSocket:
        """Parse socket name to CameraBoardSocket enum"""
        try:
            return getattr(dai.CameraBoardSocket, name)
        except AttributeError:
            raise ValueError(f"Invalid socket {name}. Use CAM_A, CAM_B, CAM_C, CAM_D.")
    
    def _build_pipeline(self) -> Tuple[dai.Pipeline, Dict[str, Any]]:
        """Build the DepthAI pipeline for dual camera recording"""
        logger.info("Building DepthAI pipeline...")
        
        pipeline = dai.Pipeline()
        
        # Create cameras
        camL = pipeline.create(dai.node.Camera).build(self.left_socket, sensorFps=self.fps)
        camR = pipeline.create(dai.node.Camera).build(self.right_socket, sensorFps=self.fps)
        
        # Camera outputs
        outL = camL.requestOutput(
            (self.width, self.height),
            type=dai.ImgFrame.Type.NV12,
            resizeMode=dai.ImgResizeMode.LETTERBOX,
            fps=self.fps,
        )
        outR = camR.requestOutput(
            (self.width, self.height),
            type=dai.ImgFrame.Type.NV12,
            resizeMode=dai.ImgResizeMode.LETTERBOX,
            fps=self.fps,
        )
        
        # Hardware synchronization
        sync = pipeline.create(dai.node.Sync)
        sync.setRunOnHost(False)
        outL.link(sync.inputs["left"])
        outR.link(sync.inputs["right"])
        
        # Video encoders
        encL = pipeline.create(dai.node.VideoEncoder).build(
            outL, frameRate=self.fps, profile=dai.VideoEncoderProperties.Profile.H265_MAIN
        )
        encR = pipeline.create(dai.node.VideoEncoder).build(
            outR, frameRate=self.fps, profile=dai.VideoEncoderProperties.Profile.H265_MAIN
        )
        
        # Host nodes for saving data
        saverL = pipeline.create(VideoSaver).build(encL.out)
        saverR = pipeline.create(VideoSaver).build(encR.out)
        loggerL = pipeline.create(TsLogger).build(encL.out)
        loggerR = pipeline.create(TsLogger).build(encR.out)
        
        # IMU
        imu = pipeline.create(dai.node.IMU)
        imu.enableIMUSensor(dai.IMUSensor.ACCELEROMETER_RAW, self.imu_rate_hz)
        imu.enableIMUSensor(dai.IMUSensor.GYROSCOPE_RAW, self.imu_rate_hz)
        imu.setBatchReportThreshold(10)
        imu.setMaxBatchReports(20)
        imu_saver = pipeline.create(IMUSaver).build(imu.out)
        
        nodes = {
            'saverL': saverL,
            'saverR': saverR,
            'loggerL': loggerL,
            'loggerR': loggerR,
            'imu_saver': imu_saver
        }
        
        logger.info("DepthAI pipeline built successfully")
        return pipeline, nodes
    
    def _dump_calibration(self, output_dir: Path):
        """Export camera calibration data"""
        calibration_path = output_dir / "calibration.json"
        data = {}
        
        try:
            # Temporarily connect to device for calibration
            logger.debug("Attempting temporary device connection for calibration...")
            with dai.Device() as temp_device:
                logger.debug("Temporary device connected successfully")
                cal = temp_device.readCalibration()
                
                def get_cam_data(sock):
                    # Intrinsics
                    K = None
                    if hasattr(cal, "getCameraIntrinsics"):
                        K = cal.getCameraIntrinsics(sock, self.width, self.height)
                    elif hasattr(cal, "getCameraMatrix"):
                        try:
                            K = cal.getCameraMatrix(sock, self.width, self.height)
                        except Exception:
                            K = cal.getCameraMatrix(sock)
                    
                    # Distortion
                    dist = None
                    for name in ("getDistortionCoefficients", "getDistortionCoeffs", "getDistortion"):
                        if hasattr(cal, name):
                            dist = getattr(cal, name)(sock)
                            break
                    
                    fov = cal.getFov(sock) if hasattr(cal, "getFov") else None
                    return {
                        "socket": getattr(sock, "name", str(sock)),
                        "width": self.width,
                        "height": self.height,
                        "intrinsics": K,
                        "distortion": dist,
                        "fov_deg": fov,
                    }
                
                data = {
                    "device_mxid": temp_device.getMxId() if hasattr(temp_device, "getMxId") else None,
                    "left": get_cam_data(self.left_socket),
                    "right": get_cam_data(self.right_socket),
                }
                
                # Extrinsics (left -> right)
                if hasattr(cal, "getCameraExtrinsics"):
                    try:
                        E = cal.getCameraExtrinsics(self.left_socket, self.right_socket)
                        if E:
                            R = [row[:3] for row in E[:3]]
                            t = [E[0][3], E[1][3], E[2][3]]
                            data["extrinsics_left_to_right"] = {"R": R, "T": t, "matrix_4x4": E}
                    except Exception:
                        pass
                        
        except Exception as e:
            data = {"error": f"Failed to read calibration: {e}"}
        
        with open(calibration_path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Calibration data saved to {calibration_path}")
    
    def _dump_calibration_from_running_device(self):
        """Export calibration from device connected via running pipeline"""
        if not self._output_dir:
            logger.error("No output directory set for calibration export")
            return
            
        calibration_path = self._output_dir / "calibration.json"
        logger.debug(f"Exporting calibration to: {calibration_path}")
        
        data = {}
        
        try:
            # Try to get device from running pipeline
            logger.debug("Attempting to get device from running pipeline...")
            
            # In DepthAI v3, we need to use a different approach for running pipelines
            # Create a temporary connection to get calibration data
            logger.debug("Creating temporary device connection for calibration...")
            with dai.Device() as cal_device:
                logger.debug("Temporary calibration device connected")
                
                cal = cal_device.readCalibration()
                logger.debug("Calibration data read from device")
                
                def get_cam_data(sock):
                    logger.debug(f"Getting calibration data for socket: {sock}")
                    # Intrinsics
                    K = None
                    if hasattr(cal, "getCameraIntrinsics"):
                        K = cal.getCameraIntrinsics(sock, self.width, self.height)
                        logger.debug(f"Got intrinsics matrix for {sock}")
                    elif hasattr(cal, "getCameraMatrix"):
                        try:
                            K = cal.getCameraMatrix(sock, self.width, self.height)
                            logger.debug(f"Got camera matrix for {sock}")
                        except Exception:
                            K = cal.getCameraMatrix(sock)
                            logger.debug(f"Got camera matrix (fallback) for {sock}")
                    
                    # Distortion
                    dist = None
                    for name in ("getDistortionCoefficients", "getDistortionCoeffs", "getDistortion"):
                        if hasattr(cal, name):
                            dist = getattr(cal, name)(sock)
                            logger.debug(f"Got distortion coefficients via {name} for {sock}")
                            break
                    
                    fov = cal.getFov(sock) if hasattr(cal, "getFov") else None
                    if fov:
                        logger.debug(f"Got FOV for {sock}: {fov}")
                    
                    return {
                        "socket": getattr(sock, "name", str(sock)),
                        "width": self.width,
                        "height": self.height,
                        "intrinsics": K,
                        "distortion": dist,
                        "fov_deg": fov,
                    }
                
                data = {
                    "device_mxid": cal_device.getMxId() if hasattr(cal_device, "getMxId") else None,
                    "left": get_cam_data(self.left_socket),
                    "right": get_cam_data(self.right_socket),
                }
                
                logger.debug(f"Device MX ID: {data.get('device_mxid')}")
                
                # Extrinsics (left -> right)
                if hasattr(cal, "getCameraExtrinsics"):
                    try:
                        E = cal.getCameraExtrinsics(self.left_socket, self.right_socket)
                        if E:
                            R = [row[:3] for row in E[:3]]
                            t = [E[0][3], E[1][3], E[2][3]]
                            data["extrinsics_left_to_right"] = {"R": R, "T": t, "matrix_4x4": E}
                            logger.debug("Successfully extracted extrinsics")
                    except Exception as e:
                        logger.debug(f"Could not get extrinsics: {e}")
                        
        except Exception as e:
            error_msg = f"Failed to read calibration from device: {e}"
            logger.error(error_msg)
            logger.exception("Full calibration error details:")
            data = {"error": error_msg}
        
        try:
            with open(calibration_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Calibration data saved to {calibration_path}")
        except Exception as e:
            logger.error(f"Failed to write calibration file: {e}")
    
    async def initialize_cameras(self, output_dir: Path) -> bool:
        """Initialize cameras and pipeline (async wrapper for sync operations)"""
        def _sync_init():
            try:
                logger.info("=== INITIALIZING CAMERAS ===")
                logger.debug(f"Output directory: {output_dir}")
                logger.debug(f"Camera config: {self.width}x{self.height} @ {self.fps}fps")
                logger.debug(f"Left socket: {self.left_socket}, Right socket: {self.right_socket}")
                logger.debug(f"IMU rate: {self.imu_rate_hz}Hz, Bitrate: {self.bitrate_kbps}kbps")
                
                self.state = RecorderState.INITIALIZING
                self._init_start_time = time.time()
                self._output_dir = output_dir
                logger.debug(f"State changed to: {self.state.value}")
                
                # Build pipeline
                logger.debug("Building DepthAI pipeline...")
                pipeline_start = time.time()
                self._pipeline, nodes = self._build_pipeline()
                pipeline_time = time.time() - pipeline_start
                logger.debug(f"Pipeline built in {pipeline_time:.3f}s")
                
                self._savers = {k: v for k, v in nodes.items() if 'saver' in k}
                self._loggers = {k: v for k, v in nodes.items() if 'logger' in k}
                logger.debug(f"Created {len(self._savers)} savers and {len(self._loggers)} loggers")
                
                # Set up file paths
                logger.debug("Creating output directory and setting file paths...")
                output_dir.mkdir(parents=True, exist_ok=True)
                
                left_h265 = output_dir / "left.h265"
                right_h265 = output_dir / "right.h265"
                left_csv = output_dir / "left.csv"
                right_csv = output_dir / "right.csv"
                imu_csv = output_dir / "imu.csv"
                
                nodes['saverL'].filename = str(left_h265)
                nodes['saverR'].filename = str(right_h265)
                nodes['loggerL'].path = str(left_csv)
                nodes['loggerR'].path = str(right_csv)
                nodes['imu_saver'].path = str(imu_csv)
                
                logger.debug(f"Video files: {left_h265}, {right_h265}")
                logger.debug(f"CSV files: {left_csv}, {right_csv}, {imu_csv}")
                
                # Note: Device connection will happen when pipeline starts
                logger.info("Pipeline ready for device connection")
                logger.debug("Device will be connected when pipeline starts")
                
                # Skip calibration during initialization for speed - will export after recording
                logger.debug("Calibration export will occur after recording stops")
                cal_time = 0
                
                init_time = time.time() - self._init_start_time
                logger.info(f"Camera initialization completed successfully in {init_time:.3f}s")
                logger.debug("Initialization breakdown:")
                logger.debug(f"  Pipeline build: {pipeline_time:.3f}s")
                logger.debug(f"  Calibration export: {cal_time:.3f}s")
                return True
                
            except Exception as e:
                logger.error(f"Failed to initialize cameras: {e}")
                logger.exception("Full initialization error details:")
                self.state = RecorderState.ERROR
                logger.debug(f"State changed to: {self.state.value}")
                return False
        
        # Run sync initialization in thread pool
        logger.debug("Running initialization in thread pool...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync_init)
        logger.debug(f"Initialization async wrapper completed, result: {result}")
        return result
    
    async def warmup_cameras(self, warmup_duration: float = 2.0) -> bool:
        """Warm up cameras without recording (allows auto-exposure to settle)"""
        def _sync_warmup():
            try:
                logger.info("=== WARMING UP CAMERAS ===")
                logger.debug(f"Requested warmup duration: {warmup_duration:.3f}s")
                logger.debug(f"Current state: {self.state.value}")
                
                self.state = RecorderState.WARMING_UP
                self._warmup_start_time = time.time()
                logger.debug(f"State changed to: {self.state.value}")
                
                if not self._pipeline:
                    error_msg = "Pipeline must be initialized before warming up"
                    logger.error(error_msg)
                    logger.debug(f"Pipeline exists: {self._pipeline is not None}")
                    raise RuntimeError(error_msg)
                
                # Start pipeline for warmup (but don't save data yet)
                logger.info("Starting pipeline for camera warmup...")
                logger.debug("Calling pipeline.start()...")
                pipeline_start_time = time.time()
                
                try:
                    self._pipeline.start()
                    pipeline_start_duration = time.time() - pipeline_start_time
                    logger.debug(f"Pipeline started successfully in {pipeline_start_duration:.3f}s")
                    
                    # Check if pipeline is running
                    if hasattr(self._pipeline, 'isRunning'):
                        is_running = self._pipeline.isRunning()
                        logger.debug(f"Pipeline running status: {is_running}")
                    
                except Exception as e:
                    logger.error(f"Failed to start pipeline for warmup: {e}")
                    logger.exception("Pipeline start error details:")
                    raise
                
                logger.info(f"Warming up cameras for {warmup_duration}s...")
                logger.debug("Beginning warmup sleep period...")
                
                # Sleep in smaller chunks to allow for monitoring
                sleep_interval = 0.5
                total_slept = 0.0
                while total_slept < warmup_duration:
                    chunk_sleep = min(sleep_interval, warmup_duration - total_slept)
                    time.sleep(chunk_sleep)
                    total_slept += chunk_sleep
                    logger.debug(f"Warmup progress: {total_slept:.1f}s / {warmup_duration:.1f}s")
                
                warmup_time = time.time() - self._warmup_start_time
                logger.info(f"Camera warmup completed in {warmup_time:.3f}s")
                logger.debug(f"Actual warmup time vs requested: {warmup_time:.3f}s vs {warmup_duration:.3f}s")
                
                self.state = RecorderState.READY
                logger.debug(f"State changed to: {self.state.value}")
                logger.info("Cameras are now warmed up and ready for recording")
                return True
                
            except Exception as e:
                logger.error(f"Failed to warm up cameras: {e}")
                logger.exception("Full warmup error details:")
                self.state = RecorderState.ERROR
                logger.debug(f"State changed to: {self.state.value}")
                return False
        
        # Run sync warmup in thread pool
        logger.debug("Running warmup in thread pool...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync_warmup)
        logger.debug(f"Warmup async wrapper completed, result: {result}")
        return result
    
    def start_recording(self) -> bool:
        """Start recording (cameras should already be warmed up)"""
        try:
            logger.info("=== STARTING RECORDING ===")
            logger.debug(f"Current state: {self.state.value}")
            logger.debug(f"Pipeline exists: {self._pipeline is not None}")
            
            if self.state != RecorderState.READY:
                error_msg = f"Cannot start recording from state: {self.state.value}"
                logger.error(error_msg)
                logger.debug("Expected state: READY")
                logger.debug("Ensure cameras are initialized and warmed up first")
                raise RuntimeError(error_msg)
            
            if not self._pipeline:
                error_msg = "Pipeline not available for recording"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
                
            # Check if pipeline is already running from warmup
            pipeline_was_running = False
            if hasattr(self._pipeline, 'isRunning'):
                pipeline_was_running = self._pipeline.isRunning()
                logger.debug(f"Pipeline already running: {pipeline_was_running}")
            
            self.state = RecorderState.RECORDING
            self._recording_start_time = time.time()
            self._stop_event.clear()
            logger.debug(f"State changed to: {self.state.value}")
            logger.debug(f"Recording start time: {self._recording_start_time}")
            
            # Pipeline should already be running from warmup, but verify
            if pipeline_was_running:
                logger.info("Using already-running pipeline from warmup phase")
            else:
                logger.warning("Pipeline not running - this may indicate warmup was skipped")
                logger.debug("Starting pipeline now...")
                try:
                    self._pipeline.start()
                    logger.debug("Pipeline started for recording")
                except Exception as e:
                    logger.error(f"Failed to start pipeline for recording: {e}")
                    raise
            
            logger.info("Recording started successfully")
            logger.debug("Video data will now be saved to files")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            logger.exception("Full recording start error details:")
            self.state = RecorderState.ERROR
            logger.debug(f"State changed to: {self.state.value}")
            return False
    
    def stop_recording(self) -> bool:
        """Stop recording and clean up resources"""
        try:
            logger.info("=== STOPPING RECORDING ===")
            logger.debug(f"Current state: {self.state.value}")
            logger.debug(f"Pipeline exists: {self._pipeline is not None}")
            
            self.state = RecorderState.STOPPING
            logger.debug(f"State changed to: {self.state.value}")
            
            # Stop pipeline
            if self._pipeline:
                logger.debug("Checking pipeline status...")
                pipeline_was_running = False
                if hasattr(self._pipeline, 'isRunning'):
                    pipeline_was_running = self._pipeline.isRunning()
                    logger.debug(f"Pipeline was running: {pipeline_was_running}")
                
                if pipeline_was_running:
                    logger.info("Stopping pipeline...")
                    logger.debug("Calling pipeline.stop()...")
                    stop_start = time.time()
                    
                    try:
                        self._pipeline.stop()
                        logger.debug("Pipeline stop called, now waiting...")
                        self._pipeline.wait()
                        stop_duration = time.time() - stop_start
                        logger.debug(f"Pipeline stopped successfully in {stop_duration:.3f}s")
                    except Exception as e:
                        logger.error(f"Error stopping pipeline: {e}")
                        logger.exception("Pipeline stop error details:")
                else:
                    logger.debug("Pipeline was not running")
            else:
                logger.debug("No pipeline to stop")
            
            # Close all savers and loggers
            logger.debug("Closing savers and loggers...")
            saver_count = len(self._savers)
            logger_count = len(self._loggers)
            logger.debug(f"Closing {saver_count} savers and {logger_count} loggers")
            
            for name, saver in self._savers.items():
                logger.debug(f"Closing saver: {name}")
                try:
                    saver.close()
                    logger.debug(f"Saver {name} closed successfully")
                except Exception as e:
                    logger.error(f"Error closing saver {name}: {e}")
                    
            for name, logger_node in self._loggers.items():
                logger.debug(f"Closing logger: {name}")
                try:
                    logger_node.close()
                    logger.debug(f"Logger {name} closed successfully")
                except Exception as e:
                    logger.error(f"Error closing logger {name}: {e}")
            
            # Device connection is managed by pipeline, no need to close explicitly
            logger.debug("Device connection will be closed with pipeline")
            
            # Export calibration data after pipeline stops and device is freed
            logger.info("Exporting calibration data after recording...")
            cal_start = time.time()
            try:
                self._dump_calibration_from_running_device()
                cal_duration = time.time() - cal_start
                logger.info(f"Calibration export completed in {cal_duration:.3f}s")
            except Exception as e:
                logger.error(f"Failed to export calibration: {e}")
                logger.exception("Calibration export error details:")
            
            # Calculate recording duration
            recording_duration = 0
            if self._recording_start_time:
                recording_duration = time.time() - self._recording_start_time
                logger.debug(f"Recording duration: {recording_duration:.3f}s")
            else:
                logger.debug("No recording start time available")
            
            logger.info(f"Recording stopped successfully. Duration: {recording_duration:.3f}s")
            
            self.state = RecorderState.STOPPED
            logger.debug(f"State changed to: {self.state.value}")
            
            # Log final file information
            if self._output_dir:
                logger.debug(f"Recording files saved in: {self._output_dir}")
                try:
                    # Check if files exist and their sizes
                    left_h265 = self._output_dir / "left.h265"
                    right_h265 = self._output_dir / "right.h265"
                    
                    if left_h265.exists():
                        size_mb = left_h265.stat().st_size / 1024 / 1024
                        logger.debug(f"Left video: {left_h265} ({size_mb:.1f} MB)")
                    else:
                        logger.warning(f"Left video file not found: {left_h265}")
                        
                    if right_h265.exists():
                        size_mb = right_h265.stat().st_size / 1024 / 1024
                        logger.debug(f"Right video: {right_h265} ({size_mb:.1f} MB)")
                    else:
                        logger.warning(f"Right video file not found: {right_h265}")
                        
                except Exception as e:
                    logger.debug(f"Error checking output files: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            logger.exception("Full recording stop error details:")
            self.state = RecorderState.ERROR
            logger.debug(f"State changed to: {self.state.value}")
            return False
    
    def get_state(self) -> Dict[str, Any]:
        """Get current recorder state and timing information"""
        current_time = time.time()
        
        timing = {}
        if self._init_start_time:
            if self.state == RecorderState.INITIALIZING:
                timing['init_elapsed'] = current_time - self._init_start_time
            else:
                timing['init_duration'] = self._warmup_start_time - self._init_start_time if self._warmup_start_time else None
        
        if self._warmup_start_time:
            if self.state == RecorderState.WARMING_UP:
                timing['warmup_elapsed'] = current_time - self._warmup_start_time
            else:
                timing['warmup_duration'] = (self._recording_start_time or current_time) - self._warmup_start_time
        
        if self._recording_start_time and self.state == RecorderState.RECORDING:
            timing['recording_elapsed'] = current_time - self._recording_start_time
        
        return {
            'state': self.state.value,
            'output_dir': str(self._output_dir) if self._output_dir else None,
            'timing': timing,
            'config': {
                'width': self.width,
                'height': self.height,
                'fps': self.fps,
                'bitrate_kbps': self.bitrate_kbps
            }
        }
    
    def cleanup(self):
        """Clean up all resources"""
        if self.state != RecorderState.STOPPED:
            self.stop_recording()
            
        self._pipeline = None
        self._device = None
        self._savers = {}
        self._loggers = {}
        self._output_dir = None