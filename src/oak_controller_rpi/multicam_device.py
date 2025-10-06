#!/usr/bin/env python3

import asyncio
import logging
import socket
import time
import uuid
import zipfile
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Any

from zeroconf import ServiceInfo, Zeroconf

from .native_recorder import NativeOAKRecorder, RecorderState
from .post_process import StereoPostProcess


class DeviceStatus(Enum):
    """Device status enum matching Swift DeviceStatus schema"""
    READY = "ready"
    RECORDING = "recording"
    STOPPING = "stopping"
    ERROR = "error"
    SCHEDULED_RECORDING_ACCEPTED = "scheduled_recording_accepted"
    RECORDING_STOPPED = "recording_stopped"
    COMMAND_RECEIVED = "command_received"
    TIME_NOT_SYNCHRONIZED = "time_not_synchronized"
    FILE_NOT_FOUND = "file_not_found"


logger = logging.getLogger(__name__)


class MultiCamDevice:
    def __init__(self, port: int = 8080, videos_dir: str = "/home/pi/videos", enable_slam: bool = False):
        self.port = port
        self.videos_dir = Path(videos_dir)
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.enable_slam = enable_slam
        
        # Generate persistent device ID
        self.device_id = self._get_or_create_device_id()
        
        # State
        self.is_recording = False
        self.current_file_id: Optional[str] = None
        self.native_recorder: Optional[NativeOAKRecorder] = None
        self.status = DeviceStatus.READY.value
        
        # File mapping for GET_VIDEO
        self.file_map: Dict[str, Path] = {}
        self._scan_existing_videos()
        
        # mDNS service
        self.zeroconf = Zeroconf()
        self.service_info = None
        
    def _get_or_create_device_id(self) -> str:
        device_id_file = Path.home() / ".multicam_device_id"
        if device_id_file.exists():
            return device_id_file.read_text().strip()
        
        device_id = str(uuid.uuid4())
        device_id_file.write_text(device_id)
        return device_id
    
    def _scan_existing_videos(self):
        """Scan videos directory and populate file_map with ZIP archives"""
        for zip_file in self.videos_dir.glob("*.zip"):
            file_id = zip_file.stem
            self.file_map[file_id] = zip_file
            logger.debug(f"Found existing video archive: {file_id} -> {zip_file}")
    
    async def start_mdns(self):
        """Start mDNS service advertisement"""
        service_name = f"multiCam-oak-{self.device_id}._multicam._tcp.local."
        
        # Get local IP address - connect to external address to determine local interface
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
        except Exception:
            # Fallback to getting IP from network interfaces
            local_ip = "127.0.0.1"
            try:
                import subprocess
                result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
                if result.returncode == 0 and result.stdout.strip():
                    local_ip = result.stdout.strip().split()[0]
            except Exception:
                pass
        
        self.service_info = ServiceInfo(
            "_multicam._tcp.local.",
            service_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={"deviceId": self.device_id},
        )
        
        await self.zeroconf.async_register_service(self.service_info)
        logger.info(f"mDNS service registered: {service_name} on {local_ip}:{self.port}")
        logger.debug(f"Service info: {self.service_info}")
    
    async def stop_mdns(self):
        """Stop mDNS service advertisement"""
        if self.service_info:
            await self.zeroconf.async_unregister_service(self.service_info)
            self.service_info = None
        await self.zeroconf.async_close()
    
    async def start_recording(self, scheduled_time: Optional[float] = None) -> Dict[str, Any]:
        """Start recording, optionally at scheduled time"""
        logger.info(f"START_RECORDING request received. Current recording state: {self.is_recording}")
        
        if self.is_recording:
            logger.warning("Recording already in progress, rejecting new start request")
            return {"status": DeviceStatus.RECORDING.value, "isRecording": True}
        
        current_time = time.time()
        logger.info(f"Current time: {current_time}, Scheduled time: {scheduled_time}")
        
        if scheduled_time and scheduled_time >= current_time + 0.01:  # 10ms threshold
            # Schedule recording with camera warmup during delay
            delay = scheduled_time - current_time
            logger.info(f"Scheduling recording to start in {delay:.3f} seconds with camera warmup")
            
            # Generate file ID and setup output directory now
            self.current_file_id = f"video_{int(time.time())}"
            output_dir = self.videos_dir / self.current_file_id
            logger.info(f"Pre-generated file ID: {self.current_file_id}")
            
            asyncio.create_task(self._delayed_start_recording_with_warmup(delay, output_dir))
            return {"status": DeviceStatus.SCHEDULED_RECORDING_ACCEPTED.value, "isRecording": False}
        else:
            # Start immediately
            logger.info("Starting recording immediately")
            await self._start_recording_now()
            return {"status": DeviceStatus.COMMAND_RECEIVED.value, "isRecording": self.is_recording}
    
    async def _delayed_start_recording_with_warmup(self, delay: float, output_dir: Path):
        """Start recording after delay, using the delay time to warm up cameras"""
        logger.info("=== SCHEDULED RECORDING WITH CAMERA WARMUP ===")
        logger.debug(f"File ID: {self.current_file_id}")
        logger.debug(f"Output directory: {output_dir}")
        logger.info(f"Total delay: {delay:.3f}s, using time for camera initialization and warmup")
        
        function_start_time = time.time()
        logger.debug(f"Function start time: {function_start_time}")
        
        try:
            # Create native recorder immediately
            logger.debug("Creating NativeOAKRecorder instance...")
            self.native_recorder = NativeOAKRecorder()
            logger.debug("Native recorder created successfully")
            
            # Phase 1: Initialize cameras (typically takes ~1-2 seconds)
            logger.info("Phase 1: Initializing cameras during delay period...")
            logger.debug("Starting camera initialization...")
            init_start_time = time.time()
            
            init_success = await self.native_recorder.initialize_cameras(output_dir)
            init_duration = time.time() - init_start_time
            logger.debug(f"Initialization result: {init_success}")
            logger.debug(f"Initialization took: {init_duration:.3f}s")
            
            if not init_success:
                error_msg = "Failed to initialize cameras during delay period"
                logger.error(error_msg)
                self.status = DeviceStatus.ERROR.value
                logger.debug("Aborting scheduled recording due to initialization failure")
                return
            
            logger.info(f"Camera initialization completed in {init_duration:.3f}s")
            
            # Phase 2: Use remaining time for camera warmup
            remaining_time = delay - init_duration
            logger.debug(f"Time remaining after init: {remaining_time:.3f}s")
            logger.debug(f"Minimum warmup threshold: 0.5s")
            
            if remaining_time > 0.5:  # At least 500ms for warmup
                warmup_duration = max(1.0, remaining_time - 0.2)  # Reserve 200ms buffer
                logger.info(f"Phase 2: Warming up cameras for {warmup_duration:.3f}s...")
                logger.debug(f"Warmup calculation: max(1.0, {remaining_time:.3f} - 0.2) = {warmup_duration:.3f}s")
                
                warmup_start_time = time.time()
                warmup_success = await self.native_recorder.warmup_cameras(warmup_duration)
                actual_warmup_time = time.time() - warmup_start_time
                logger.debug(f"Warmup result: {warmup_success}")
                logger.debug(f"Actual warmup time: {actual_warmup_time:.3f}s")
                
                if not warmup_success:
                    error_msg = "Failed to warm up cameras during delay period"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    logger.debug("Aborting scheduled recording due to warmup failure")
                    return
                    
                logger.info("Camera warmup completed successfully")
            else:
                logger.warning(f"Insufficient time for full warmup ({remaining_time:.3f}s remaining)")
                logger.debug("Performing minimal warmup (0.5s)...")
                minimal_warmup_start = time.time()
                await self.native_recorder.warmup_cameras(0.5)
                minimal_warmup_time = time.time() - minimal_warmup_start
                logger.debug(f"Minimal warmup completed in {minimal_warmup_time:.3f}s")
            
            # Phase 3: Calculate remaining time until scheduled start
            current_time = time.time()
            elapsed_total = current_time - function_start_time
            final_wait = delay - elapsed_total
            
            logger.debug(f"Timing calculations:")
            logger.debug(f"  Function start: {function_start_time}")
            logger.debug(f"  Current time: {current_time}")
            logger.debug(f"  Total elapsed: {elapsed_total:.3f}s")
            logger.debug(f"  Original delay: {delay:.3f}s")
            logger.debug(f"  Final wait needed: {final_wait:.3f}s")
            
            if final_wait > 0.01:
                logger.info(f"Phase 3: Final wait {final_wait:.3f}s until scheduled time...")
                logger.debug("Starting final sleep...")
                await asyncio.sleep(final_wait)
                logger.debug("Final sleep completed")
            else:
                logger.info(f"Ready for immediate start (used {elapsed_total:.3f}s of {delay:.3f}s delay)")
                if final_wait < -0.1:
                    logger.warning(f"Schedule overrun by {-final_wait:.3f}s - recording may start late")
            
            # Phase 4: Start recording with pre-warmed cameras
            logger.info("Phase 4: Starting recording with pre-warmed cameras!")
            logger.debug("Calling _start_recording_now()...")
            record_start_time = time.time()
            
            await self._start_recording_now()
            
            record_call_time = time.time() - record_start_time
            total_function_time = time.time() - function_start_time
            
            logger.debug(f"Recording start call took: {record_call_time:.3f}s")
            logger.debug(f"Total scheduled recording function time: {total_function_time:.3f}s")
            logger.info("Scheduled recording with warmup completed!")
            
        except Exception as e:
            error_msg = f"Failed during scheduled recording with warmup: {e}"
            logger.error(error_msg)
            logger.exception("Full exception details:")
            logger.debug(f"Error occurred {time.time() - function_start_time:.3f}s into the function")
            self.status = DeviceStatus.ERROR.value
    
    async def _start_recording_now(self):
        """Actually start the recording process using native recorder"""
        logger.info("=== STARTING NATIVE RECORDING PROCESS ===")
        try:
            if self.native_recorder and self.native_recorder.get_state()['state'] == RecorderState.READY.value:
                # Cameras are already warmed up, start recording immediately
                logger.info("Using pre-warmed cameras")
                success = self.native_recorder.start_recording()
                if success:
                    self.is_recording = True
                    self.status = DeviceStatus.RECORDING.value
                    logger.info(f"Native recording started successfully: {self.current_file_id}")
                else:
                    error_msg = "Failed to start native recording"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    self.is_recording = False
            else:
                # No pre-warmed recorder, initialize from scratch
                logger.info("Initializing cameras from scratch (no warmup)")
                self.current_file_id = f"video_{int(time.time())}"
                output_dir = self.videos_dir / self.current_file_id
                logger.info(f"Generated file ID: {self.current_file_id}")
                logger.info(f"Output directory: {output_dir}")
                
                # Create native recorder
                self.native_recorder = NativeOAKRecorder(enable_slam=self.enable_slam)
                
                # Initialize cameras
                logger.info("Initializing cameras...")
                init_success = await self.native_recorder.initialize_cameras(output_dir)
                if not init_success:
                    error_msg = "Failed to initialize cameras"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    self.is_recording = False
                    return
                
                # Quick warmup (minimal delay for immediate recording)
                logger.info("Quick camera warmup...")
                warmup_success = await self.native_recorder.warmup_cameras(warmup_duration=1.0)
                if not warmup_success:
                    error_msg = "Failed to warm up cameras"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    self.is_recording = False
                    return
                
                # Start recording
                success = self.native_recorder.start_recording()
                if success:
                    self.is_recording = True
                    self.status = DeviceStatus.RECORDING.value
                    logger.info(f"Native recording started successfully: {self.current_file_id}")
                else:
                    error_msg = "Failed to start native recording after initialization"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    self.is_recording = False
            
        except Exception as e:
            error_msg = f"Failed to start native recording: {e}"
            logger.error(error_msg)
            logger.exception("Full exception details:")
            self.status = DeviceStatus.ERROR.value
            self.is_recording = False
    
    async def stop_recording(self) -> Dict[str, Any]:
        """Stop active recording using native recorder"""
        logger.info("=== STOPPING NATIVE RECORDING PROCESS ===")
        logger.info(f"Current recording state: is_recording={self.is_recording}, recorder={self.native_recorder is not None}")
        
        if not self.is_recording or not self.native_recorder:
            logger.warning("Stop recording requested but not currently recording")
            return {"status": DeviceStatus.ERROR.value, "isRecording": False}
        
        try:
            # Stop the native recorder
            logger.info("Stopping native recorder...")
            success = self.native_recorder.stop_recording()
            if not success:
                logger.error("Failed to stop native recorder")
            
            # Create ZIP archive of recorded data
            logger.info("Starting video finalization process - creating ZIP archive")
            await self._finalize_recording()
            
            self.is_recording = False
            self.status = DeviceStatus.READY.value
            logger.info(f"Recording stopped successfully. File ID: {self.current_file_id}")
            
            response = {
                "status": DeviceStatus.RECORDING_STOPPED.value,
                "isRecording": False,
                "fileId": self.current_file_id
            }
            
            temp_file_id = self.current_file_id
            self.current_file_id = None
            
            # Clean up the recorder instance
            if self.native_recorder:
                self.native_recorder.cleanup()
                self.native_recorder = None
            
            logger.info(f"Stop recording response: {response}")
            return response
            
        except Exception as e:
            error_msg = f"Error stopping native recording: {e}"
            self.status = error_msg
            logger.error(error_msg)
            logger.exception("Full exception details:")
            return {"status": DeviceStatus.ERROR.value, "isRecording": self.is_recording}
    
    async def _finalize_recording(self):
        """Run stereo post-processing (MP4 + stats) and zip results, update file_map."""
        if not self.current_file_id:
            logger.warning("No current file ID for finalization")
            return

        output_dir = self.videos_dir / self.current_file_id
        if not output_dir.exists():
            logger.error(f"Output directory does not exist: {output_dir}")
            return

        # Required inputs
        left_h264 = output_dir / "left.h264"
        right_h264 = output_dir / "right.h264"
        left_csv = output_dir / "left.csv"
        right_csv = output_dir / "right.csv"
        rgb_h264 = output_dir / "rgb.h264"
        rgb_csv = output_dir / "rgb.csv"

        def run_finalize():
            try:
                if all(p.exists() for p in (left_h264, right_h264, left_csv, right_csv)):
                    # Check if RGB files exist
                    rgb_h264_param = rgb_h264 if rgb_h264.exists() else None
                    rgb_csv_param = rgb_csv if rgb_csv.exists() else None
                    
                    spp = StereoPostProcess(left_h264, left_csv, right_h264, right_csv, 
                                          rgb_h264=rgb_h264_param, rgb_csv=rgb_csv_param)
                    res = spp.finalize(output_dir=output_dir)
                    return res
                else:
                    # Fallback: just zip whatever is present
                    zip_path = output_dir.with_suffix('.zip')
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for file_path in output_dir.rglob('*'):
                            if file_path.is_file():
                                zipf.write(file_path, file_path.relative_to(output_dir))
                    return {"zip_path": str(zip_path), "zip_ok": zip_path.exists() and zip_path.stat().st_size > 0}
            except Exception as e:
                logger.error(f"Finalize failed: {e}")
                return {"error": str(e)}

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_finalize)

        # Register produced ZIP if present
        zip_path = None
        if isinstance(result, dict):
            zp = result.get("zip_path")
            if zp:
                zip_path = Path(zp)
        if zip_path and zip_path.exists():
            self.file_map[self.current_file_id] = zip_path
            try:
                sz_mb = zip_path.stat().st_size / 1024 / 1024
            except Exception:
                sz_mb = 0.0
            logger.info(f"Finalized recording. ZIP: {zip_path} ({sz_mb:.1f} MB)")
        else:
            logger.error("Finalization did not produce a ZIP archive")
    
    def get_device_status(self) -> Dict[str, Any]:
        """Get current device status including native recorder state"""
        status_response = {
            "status": self.status,
            "isRecording": self.is_recording,
            "deviceId": self.device_id,
            "timestamp": time.time()
        }
        
        # Add native recorder state if available
        if self.native_recorder:
            recorder_state = self.native_recorder.get_state()
            status_response["recorderState"] = recorder_state['state']
            status_response["timing"] = recorder_state.get('timing', {})
        
        return status_response
    
    def get_video_info(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Get video file info for GET_VIDEO"""
        if file_id not in self.file_map:
            return None
        
        file_path = self.file_map[file_id]
        if not file_path.exists():
            return None
        
        return {
            "fileId": file_id,
            "fileName": file_path.name,
            "fileSize": file_path.stat().st_size,
            "filePath": str(file_path)
        }


