#!/usr/bin/env python3

import asyncio
import logging
import socket
import time
import uuid
import zipfile
import aiohttp
from pathlib import Path
from typing import Dict, Optional, Any, List, Union

from zeroconf import ServiceInfo, Zeroconf
from multicam_common.status import DeviceStatus
from multicam_common.commands import (
    CommandType, CommandMessage, StatusResponse,
    StopRecordingResponse, ErrorResponse, FileResponse,
    ListFilesResponse, FileMetadata, UploadItem, UploadStatus
)
from multicam_common.constants import TCP_PORT

from .native_recorder import NativeOAKRecorder, RecorderState
from .post_process import StereoPostProcess


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
        self.current_file_name: Optional[str] = None
        self.native_recorder: Optional[NativeOAKRecorder] = None
        self.status = DeviceStatus.READY.value

        # Upload queue infrastructure
        self.upload_queue: List[UploadItem] = []
        self.failed_upload_queue: List[UploadItem] = []
        self.upload_tasks: Dict[str, asyncio.Task] = {}  # fileName -> task
        self._upload_lock = asyncio.Lock()  # Thread-safe queue operations
        self.upload_iam_credentials: Dict[str, Dict[str, str]] = {}  # fileName -> IAM credentials

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

    def _get_battery_level(self) -> Optional[float]:
        """Get battery level (platform-specific, returns None for RPi)"""
        # TODO: Implement platform-specific battery reading
        # For RPi with UPS, read from I2C/GPIO
        # For now, return None
        return None

    async def start_recording(self, scheduled_time: Optional[float] = None) -> StatusResponse:
        """Start recording, optionally at scheduled time"""
        logger.info(f"START_RECORDING request received. Current recording state: {self.is_recording}")

        if self.is_recording:
            logger.warning("Recording already in progress, rejecting new start request")
            return StatusResponse(
                deviceId=self.device_id,
                status=DeviceStatus.RECORDING.value,
                timestamp=time.time(),
                batteryLevel=self._get_battery_level(),
                uploadQueue=self.upload_queue,
                failedUploadQueue=self.failed_upload_queue
            )
        
        current_time = time.time()
        logger.info(f"Current time: {current_time}, Scheduled time: {scheduled_time}")
        
        if scheduled_time and scheduled_time >= current_time + 0.01:  # 10ms threshold
            # Schedule recording with camera warmup during delay
            delay = scheduled_time - current_time
            logger.info(f"Scheduling recording to start in {delay:.3f} seconds with camera warmup")

            # Generate file name and setup output directory now
            self.current_file_name = f"video_{int(time.time())}.zip"
            output_dir = self.videos_dir / Path(self.current_file_name).stem
            logger.info(f"Pre-generated file name: {self.current_file_name}")

            asyncio.create_task(self._delayed_start_recording_with_warmup(delay, output_dir))
            return StatusResponse(
                deviceId=self.device_id,
                status=DeviceStatus.SCHEDULED_RECORDING_ACCEPTED.value,
                timestamp=time.time(),
                batteryLevel=self._get_battery_level(),
                uploadQueue=self.upload_queue,
                failedUploadQueue=self.failed_upload_queue
            )
        else:
            # Start immediately
            logger.info("Starting recording immediately")
            await self._start_recording_now()
            return StatusResponse(
                deviceId=self.device_id,
                status=DeviceStatus.COMMAND_RECEIVED.value,
                timestamp=time.time(),
                batteryLevel=self._get_battery_level(),
                uploadQueue=self.upload_queue,
                failedUploadQueue=self.failed_upload_queue
            )
    
    async def _delayed_start_recording_with_warmup(self, delay: float, output_dir: Path):
        """Start recording after delay, using the delay time to warm up cameras"""
        logger.info("=== SCHEDULED RECORDING WITH CAMERA WARMUP ===")
        logger.debug(f"File name: {self.current_file_name}")
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
                    logger.info(f"Native recording started successfully: {self.current_file_name}")
                else:
                    error_msg = "Failed to start native recording"
                    logger.error(error_msg)
                    self.status = DeviceStatus.ERROR.value
                    self.is_recording = False
            else:
                # No pre-warmed recorder, initialize from scratch
                logger.info("Initializing cameras from scratch (no warmup)")
                self.current_file_name = f"video_{int(time.time())}.zip"
                output_dir = self.videos_dir / Path(self.current_file_name).stem
                logger.info(f"Generated file name: {self.current_file_name}")
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
                    logger.info(f"Native recording started successfully: {self.current_file_name}")
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
    
    async def stop_recording(self) -> Union[StopRecordingResponse, ErrorResponse]:
        """Stop active recording using native recorder"""
        logger.info("=== STOPPING NATIVE RECORDING PROCESS ===")
        logger.info(f"Current recording state: is_recording={self.is_recording}, recorder={self.native_recorder is not None}")
        
        if not self.is_recording or not self.native_recorder:
            logger.warning("Stop recording requested but not currently recording")
            return ErrorResponse(
                deviceId=self.device_id,
                status=DeviceStatus.ERROR.value,
                timestamp=time.time(),
                message="Not currently recording"
            )
        
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
            logger.info(f"Recording stopped successfully. File name: {self.current_file_name}")

            # Get file size
            file_path = self.videos_dir / self.current_file_name
            file_size = file_path.stat().st_size if file_path.exists() else 0

            response = StopRecordingResponse(
                deviceId=self.device_id,
                status=DeviceStatus.RECORDING_STOPPED.value,
                timestamp=time.time(),
                fileName=self.current_file_name,
                fileSize=file_size
            )

            temp_file_name = self.current_file_name
            self.current_file_name = None
            
            # Clean up the recorder instance
            if self.native_recorder:
                self.native_recorder.cleanup()
                self.native_recorder = None
            
            logger.info(f"Stop recording response: {response}")
            return response
            
        except Exception as e:
            error_msg = f"Error stopping native recording: {e}"
            self.status = DeviceStatus.ERROR.value
            logger.error(error_msg)
            logger.exception("Full exception details:")
            return ErrorResponse(
                deviceId=self.device_id,
                status=DeviceStatus.ERROR.value,
                timestamp=time.time(),
                message=error_msg
            )
    
    async def _finalize_recording(self):
        """Run stereo post-processing (MP4 + stats) and zip results."""
        if not self.current_file_name:
            logger.warning("No current file name for finalization")
            return

        output_dir = self.videos_dir / Path(self.current_file_name).stem
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

        # Log produced ZIP if present
        zip_path = None
        if isinstance(result, dict):
            zp = result.get("zip_path")
            if zp:
                zip_path = Path(zp)
        if zip_path and zip_path.exists():
            try:
                sz_mb = zip_path.stat().st_size / 1024 / 1024
            except Exception:
                sz_mb = 0.0
            logger.info(f"Finalized recording. ZIP: {zip_path} ({sz_mb:.1f} MB)")
        else:
            logger.error("Finalization did not produce a ZIP archive")
    
    def get_device_status(self) -> StatusResponse:
        """Get current device status"""
        return StatusResponse(
            deviceId=self.device_id,
            status=self.status,
            timestamp=time.time(),
            batteryLevel=self._get_battery_level(),
            uploadQueue=self.upload_queue,
            failedUploadQueue=self.failed_upload_queue
        )
    
    def get_video_info(self, file_name: str) -> Optional[FileMetadata]:
        """Get video file metadata for GET_VIDEO"""
        file_path = self.videos_dir / file_name
        if not file_path.exists():
            return None

        stat = file_path.stat()
        return FileMetadata(
            fileName=file_name,
            fileSize=stat.st_size,
            creationDate=stat.st_ctime,
            modificationDate=stat.st_mtime
        )

    async def upload_to_cloud(
        self,
        file_name: str,
        upload_url: Optional[str] = None,
        s3_bucket: Optional[str] = None,
        s3_key: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        region: Optional[str] = None
    ) -> Union[StatusResponse, ErrorResponse]:
        """Queue file upload to cloud using presigned S3 URL or IAM credentials"""
        logger.info(f"=== UPLOAD TO CLOUD REQUEST ===")

        # Determine authentication method
        using_iam = all([s3_bucket, s3_key, access_key_id, secret_access_key, session_token, region])

        if using_iam:
            logger.info(f"File: {file_name}, Bucket: {s3_bucket}, Key: {s3_key}, Method: IAM")
        elif upload_url:
            logger.info(f"File: {file_name}, URL: {upload_url[:50]}..., Method: Presigned URL")
        else:
            logger.error("No authentication credentials provided")
            return ErrorResponse(
                deviceId=self.device_id,
                status=DeviceStatus.ERROR.value,
                timestamp=time.time(),
                message="Missing authentication credentials (uploadUrl or IAM credentials)"
            )

        # Validate file exists
        file_path = self.videos_dir / file_name
        if not file_path.exists():
            logger.error(f"File not found: {file_name}")
            return ErrorResponse(
                deviceId=self.device_id,
                status=DeviceStatus.FILE_NOT_FOUND.value,
                timestamp=time.time(),
                message=f"File not found: {file_name}"
            )

        # Check if already uploading
        if file_name in self.upload_tasks:
            logger.warning(f"Upload already in progress for: {file_name}")
            return ErrorResponse(
                deviceId=self.device_id,
                status=DeviceStatus.ERROR.value,
                timestamp=time.time(),
                message=f"Upload already in progress for {file_name}"
            )

        # Check if already in queue
        async with self._upload_lock:
            for item in self.upload_queue:
                if item.fileName == file_name:
                    logger.warning(f"File already in upload queue: {file_name}")
                    return ErrorResponse(
                        deviceId=self.device_id,
                        status=DeviceStatus.ERROR.value,
                        timestamp=time.time(),
                        message=f"File already in upload queue: {file_name}"
                    )

        # Get file size
        file_size = file_path.stat().st_size
        logger.info(f"File size: {file_size} bytes ({file_size / 1024 / 1024:.2f} MB)")

        # Store IAM credentials if provided
        if using_iam:
            self.upload_iam_credentials[file_name] = {
                'bucket': s3_bucket,
                'key': s3_key,
                'access_key_id': access_key_id,
                'secret_access_key': secret_access_key,
                'session_token': session_token,
                'region': region
            }
            display_url = f"s3://{s3_bucket}/{s3_key}"
        else:
            display_url = upload_url

        # Create UploadItem
        upload_item = UploadItem(
            fileName=file_name,
            fileSize=file_size,
            bytesUploaded=0,
            uploadProgress=0.0,
            uploadSpeed=0,
            status=UploadStatus.QUEUED.value,
            uploadUrl=display_url,
            error=None
        )

        # Add to queue
        async with self._upload_lock:
            self.upload_queue.append(upload_item)

        # Start background upload task
        task = asyncio.create_task(self._upload_file_task(file_name))
        self.upload_tasks[file_name] = task

        logger.info(f"Upload queued successfully: {file_name}")

        return StatusResponse(
            deviceId=self.device_id,
            status=DeviceStatus.UPLOAD_QUEUED.value,
            timestamp=time.time(),
            batteryLevel=self._get_battery_level(),
            uploadQueue=self.upload_queue,
            failedUploadQueue=self.failed_upload_queue
        )

    async def _upload_file_task(self, file_name: str) -> None:
        """Background task that performs the actual upload"""
        logger.info(f"=== UPLOAD TASK STARTED: {file_name} ===")

        upload_item = None

        try:
            # Find upload item in queue
            async with self._upload_lock:
                for item in self.upload_queue:
                    if item.fileName == file_name:
                        upload_item = item
                        break

            if not upload_item:
                logger.error(f"Upload item not found in queue: {file_name}")
                return

            # Update status to UPLOADING
            async with self._upload_lock:
                upload_item.status = UploadStatus.UPLOADING.value
                self.status = DeviceStatus.UPLOADING.value

            logger.info(f"Starting upload: {file_name}")

            # Perform the upload - route to appropriate method
            file_path = self.videos_dir / file_name

            # Check if using IAM credentials
            if file_name in self.upload_iam_credentials:
                creds = self.upload_iam_credentials[file_name]
                await self._upload_to_s3_with_iam(file_path, upload_item, creds)
            else:
                await self._upload_to_s3(file_path, upload_item)

            # Success - delete file and remove from queue
            logger.info(f"Upload completed successfully: {file_name}")
            await self._delete_uploaded_file(file_name)

            async with self._upload_lock:
                self.upload_queue.remove(upload_item)
                self.status = DeviceStatus.READY.value

            # Cleanup IAM credentials
            if file_name in self.upload_iam_credentials:
                del self.upload_iam_credentials[file_name]

            logger.info(f"File deleted and removed from queue: {file_name}")

        except Exception as e:
            error_msg = f"Upload failed: {str(e)}"
            logger.error(f"{error_msg} for file: {file_name}")
            logger.exception("Full exception details:")

            # Move to failed queue
            if upload_item:
                async with self._upload_lock:
                    upload_item.status = UploadStatus.FAILED.value
                    upload_item.error = error_msg

                    # Remove from upload queue
                    if upload_item in self.upload_queue:
                        self.upload_queue.remove(upload_item)

                    # Add to failed queue
                    self.failed_upload_queue.append(upload_item)

                    self.status = DeviceStatus.UPLOAD_FAILED.value

                logger.warning(f"Upload moved to failed queue: {file_name}")

        finally:
            # Cleanup task reference and IAM credentials
            if file_name in self.upload_tasks:
                del self.upload_tasks[file_name]

            # Cleanup IAM credentials (in case of failure)
            if file_name in self.upload_iam_credentials:
                del self.upload_iam_credentials[file_name]

            logger.info(f"=== UPLOAD TASK ENDED: {file_name} ===")

    async def _upload_to_s3(self, file_path: Path, upload_item: UploadItem) -> None:
        """Upload file to S3 with progress tracking"""
        import io

        start_time = time.time()

        logger.info(f"Starting S3 upload: {file_path.name}, size: {upload_item.fileSize} bytes")

        try:
            # Read file into memory (S3 doesn't support chunked transfer encoding)
            logger.debug(f"Reading file into memory: {file_path}")
            with open(file_path, 'rb') as f:
                file_data = f.read()

            logger.debug(f"File read complete: {len(file_data)} bytes")

            # Create a custom file-like object that tracks progress as it's read
            class ProgressBytesIO(io.BytesIO):
                """BytesIO that calls progress callback as data is read"""
                def __init__(self, data, progress_callback):
                    super().__init__(data)
                    self.progress_callback = progress_callback
                    self.bytes_read = 0
                    self.total_size = len(data)
                    self.last_update = 0

                def read(self, size=-1):
                    chunk = super().read(size)
                    if chunk:
                        self.bytes_read += len(chunk)
                        # Update every 640KB to avoid excessive updates
                        if self.bytes_read - self.last_update >= 655360 or self.bytes_read == self.total_size:
                            self.last_update = self.bytes_read
                            if self.progress_callback:
                                self.progress_callback(self.bytes_read, self.total_size)
                    return chunk

            # Progress callback function
            def progress_callback(bytes_uploaded, total_bytes):
                elapsed = time.time() - start_time
                # Schedule async update in event loop
                asyncio.create_task(self._update_upload_progress(
                    upload_item.fileName,
                    bytes_uploaded,
                    elapsed
                ))

            # Create progress-tracking file object
            progress_file = ProgressBytesIO(file_data, progress_callback)

            logger.debug(f"Starting PUT request to S3: {len(file_data)} bytes")

            # Perform HTTP PUT request
            # S3 requires Content-Length and doesn't support chunked transfer encoding
            timeout = aiohttp.ClientTimeout(total=600)  # 10 minute timeout

            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Note: Skip auto headers that might interfere with signature
                # Pass file-like object that will be read by aiohttp
                async with session.put(
                    upload_item.uploadUrl,
                    data=progress_file,
                    skip_auto_headers=['content-type']
                ) as response:
                    if response.status != 200:
                        error_msg = f"HTTP {response.status}: {await response.text()}"
                        logger.error(f"S3 upload failed: {error_msg}")
                        raise Exception(error_msg)

                    logger.info(f"S3 upload successful: {file_path.name}")

            # Final progress update
            elapsed = time.time() - start_time
            await self._update_upload_progress(
                upload_item.fileName,
                len(file_data),
                elapsed
            )

            # Log final statistics
            avg_speed = len(file_data) / elapsed if elapsed > 0 else 0
            logger.info(f"Upload stats: {len(file_data)} bytes in {elapsed:.2f}s "
                       f"(avg speed: {avg_speed / 1024 / 1024:.2f} MB/s)")

        except aiohttp.ClientError as e:
            error_msg = f"Network error during upload: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
        except FileNotFoundError:
            error_msg = f"File deleted during upload: {file_path}"
            logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            logger.error(f"Unexpected error during upload: {e}")
            raise

    async def _upload_to_s3_with_iam(self, file_path: Path, upload_item: UploadItem, credentials: Dict[str, str]) -> None:
        """Upload file to S3 using IAM credentials with multipart support via boto3"""
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import ClientError

        start_time = time.time()
        bucket = credentials['bucket']
        key = credentials['key']
        region = credentials['region']

        logger.info(f"Starting S3 IAM upload: {file_path.name}, size: {upload_item.fileSize} bytes")
        logger.info(f"Bucket: {bucket}, Key: {key}, Region: {region}")

        # Configure multipart upload settings
        config = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,  # 8MB - files larger trigger multipart
            multipart_chunksize=8 * 1024 * 1024,  # 8MB per part
            max_concurrency=10,  # Up to 10 concurrent uploads
            use_threads=True  # Use threading for multipart uploads
        )

        # Get event loop reference for use in callback
        loop = asyncio.get_running_loop()

        # Progress callback for boto3
        bytes_uploaded_total = [0]  # Use list to allow modification in nested function

        def progress_callback(bytes_uploaded_chunk):
            """Called by boto3 for each chunk uploaded"""
            bytes_uploaded_total[0] += bytes_uploaded_chunk
            elapsed = time.time() - start_time

            # Schedule async update in event loop from thread
            asyncio.run_coroutine_threadsafe(
                self._update_upload_progress(
                    upload_item.fileName,
                    bytes_uploaded_total[0],
                    elapsed
                ),
                loop
            )

        try:
            # Create S3 client with temporary credentials
            logger.debug(f"Creating S3 client with IAM credentials")
            s3_client = boto3.client(
                's3',
                aws_access_key_id=credentials['access_key_id'],
                aws_secret_access_key=credentials['secret_access_key'],
                aws_session_token=credentials['session_token'],
                region_name=region
            )

            # Execute upload in thread pool to avoid blocking async event loop
            # boto3 is synchronous, so we run it in an executor
            logger.debug(f"Starting boto3 upload (multipart threshold: 8MB)")
            await loop.run_in_executor(
                None,  # Use default thread pool
                lambda: s3_client.upload_file(
                    str(file_path),
                    bucket,
                    key,
                    Config=config,
                    Callback=progress_callback
                )
            )

            logger.info(f"S3 IAM upload successful: {file_path.name}")

            # Final progress update
            elapsed = time.time() - start_time
            await self._update_upload_progress(
                upload_item.fileName,
                upload_item.fileSize,
                elapsed
            )

            # Log final statistics
            avg_speed = upload_item.fileSize / elapsed if elapsed > 0 else 0
            logger.info(f"Upload stats: {upload_item.fileSize} bytes in {elapsed:.2f}s "
                       f"(avg speed: {avg_speed / 1024 / 1024:.2f} MB/s)")

            # Check if multipart was used
            if upload_item.fileSize > config.multipart_threshold:
                num_parts = (upload_item.fileSize + config.multipart_chunksize - 1) // config.multipart_chunksize
                logger.info(f"Multipart upload used: {num_parts} parts of {config.multipart_chunksize / 1024 / 1024:.1f} MB each")

        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = f"AWS S3 error ({error_code}): {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
        except FileNotFoundError:
            error_msg = f"File deleted during upload: {file_path}"
            logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            logger.error(f"Unexpected error during IAM upload: {e}")
            raise

    async def _update_upload_progress(self, file_name: str, bytes_uploaded: int, elapsed: float) -> None:
        """Update UploadItem progress fields (thread-safe)"""
        async with self._upload_lock:
            for item in self.upload_queue:
                if item.fileName == file_name:
                    item.bytesUploaded = bytes_uploaded
                    item.uploadProgress = (bytes_uploaded / item.fileSize) * 100.0 if item.fileSize > 0 else 0.0
                    item.uploadSpeed = int(bytes_uploaded / elapsed) if elapsed > 0 else 0

                    # Log progress at 25%, 50%, 75% milestones
                    progress = item.uploadProgress
                    if (progress >= 25 and progress < 26) or \
                       (progress >= 50 and progress < 51) or \
                       (progress >= 75 and progress < 76):
                        logger.info(f"Upload progress: {file_name} - {progress:.1f}% "
                                   f"({bytes_uploaded / 1024 / 1024:.2f} MB, "
                                   f"speed: {item.uploadSpeed / 1024 / 1024:.2f} MB/s)")
                    break

    async def _delete_uploaded_file(self, file_name: str) -> None:
        """Delete ZIP and source directory after successful upload"""
        import shutil

        try:
            # Delete ZIP file
            zip_path = self.videos_dir / file_name
            if zip_path.exists():
                zip_path.unlink()
                logger.info(f"Deleted uploaded file: {zip_path}")
            else:
                logger.warning(f"ZIP file not found for deletion: {zip_path}")

            # Delete source directory (e.g., video_123/ for video_123.zip)
            source_dir = self.videos_dir / Path(file_name).stem
            if source_dir.exists() and source_dir.is_dir():
                shutil.rmtree(source_dir)
                logger.info(f"Deleted source directory: {source_dir}")

        except Exception as e:
            # Log but don't raise - file deletion failure shouldn't fail the upload
            logger.warning(f"Error deleting files for {file_name}: {e}")


