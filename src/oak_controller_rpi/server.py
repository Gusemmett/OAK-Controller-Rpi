#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import struct
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any
from zeroconf import ServiceInfo, Zeroconf
import socket

from .native_recorder import NativeOAKRecorder, RecorderState

logger = logging.getLogger(__name__)

class MultiCamDevice:
    def __init__(self, port: int = 8080, videos_dir: str = "/home/pi/videos"):
        self.port = port
        self.videos_dir = Path(videos_dir)
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate persistent device ID
        self.device_id = self._get_or_create_device_id()
        
        # State
        self.is_recording = False
        self.current_file_id: Optional[str] = None
        self.native_recorder: Optional[NativeOAKRecorder] = None
        self.status = "ready"  # ready, recording, error
        
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
        service_name = f"multiCam-{self.device_id}._multicam._tcp.local."
        
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
            return {"status": "Already recording", "isRecording": True}
        
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
            return {"status": "Scheduled recording accepted", "isRecording": False}
        else:
            # Start immediately
            logger.info("Starting recording immediately")
            await self._start_recording_now()
            return {"status": "Command received", "isRecording": self.is_recording}
    
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
                self.status = error_msg
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
                    self.status = error_msg
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
            self.status = error_msg
    
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
                    self.status = "recording"
                    logger.info(f"Native recording started successfully: {self.current_file_id}")
                else:
                    error_msg = "Failed to start native recording"
                    logger.error(error_msg)
                    self.status = error_msg
                    self.is_recording = False
            else:
                # No pre-warmed recorder, initialize from scratch
                logger.info("Initializing cameras from scratch (no warmup)")
                self.current_file_id = f"video_{int(time.time())}"
                output_dir = self.videos_dir / self.current_file_id
                logger.info(f"Generated file ID: {self.current_file_id}")
                logger.info(f"Output directory: {output_dir}")
                
                # Create native recorder
                self.native_recorder = NativeOAKRecorder()
                
                # Initialize cameras
                logger.info("Initializing cameras...")
                init_success = await self.native_recorder.initialize_cameras(output_dir)
                if not init_success:
                    error_msg = "Failed to initialize cameras"
                    logger.error(error_msg)
                    self.status = error_msg
                    self.is_recording = False
                    return
                
                # Quick warmup (minimal delay for immediate recording)
                logger.info("Quick camera warmup...")
                warmup_success = await self.native_recorder.warmup_cameras(warmup_duration=1.0)
                if not warmup_success:
                    error_msg = "Failed to warm up cameras"
                    logger.error(error_msg)
                    self.status = error_msg
                    self.is_recording = False
                    return
                
                # Start recording
                success = self.native_recorder.start_recording()
                if success:
                    self.is_recording = True
                    self.status = "recording"
                    logger.info(f"Native recording started successfully: {self.current_file_id}")
                else:
                    error_msg = "Failed to start native recording after initialization"
                    logger.error(error_msg)
                    self.status = error_msg
                    self.is_recording = False
            
        except Exception as e:
            error_msg = f"Failed to start native recording: {e}"
            logger.error(error_msg)
            logger.exception("Full exception details:")
            self.status = error_msg
            self.is_recording = False
    
    async def stop_recording(self) -> Dict[str, Any]:
        """Stop active recording using native recorder"""
        logger.info("=== STOPPING NATIVE RECORDING PROCESS ===")
        logger.info(f"Current recording state: is_recording={self.is_recording}, recorder={self.native_recorder is not None}")
        
        if not self.is_recording or not self.native_recorder:
            logger.warning("Stop recording requested but not currently recording")
            return {"status": "Not recording", "isRecording": False}
        
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
            self.status = "ready"
            logger.info(f"Recording stopped successfully. File ID: {self.current_file_id}")
            
            response = {
                "status": "Recording stopped",
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
            return {"status": error_msg, "isRecording": self.is_recording}
    
    async def _finalize_recording(self):
        """Create ZIP archive of all recorded data and update file_map"""
        if not self.current_file_id:
            logger.warning("No current file ID for finalization")
            return
        
        output_dir = self.videos_dir / self.current_file_id
        zip_file = self.videos_dir / f"{self.current_file_id}.zip"
        
        logger.info(f"Creating ZIP archive: {zip_file}")
        logger.debug(f"Source directory: {output_dir}")
        
        if not output_dir.exists():
            logger.error(f"Output directory does not exist: {output_dir}")
            return
        
        try:
            # Create ZIP archive in a separate thread to avoid blocking
            def create_zip():
                import zipfile
                
                with zipfile.ZipFile(zip_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    # Add all files from the output directory
                    for file_path in output_dir.rglob('*'):
                        if file_path.is_file():
                            # Use relative path in ZIP archive
                            arcname = file_path.relative_to(output_dir)
                            zipf.write(file_path, arcname)
                            logger.debug(f"Added to ZIP: {arcname}")
                
                return zip_file.stat().st_size if zip_file.exists() else 0
            
            # Run ZIP creation in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            zip_size = await loop.run_in_executor(None, create_zip)
            
            if zip_file.exists() and zip_size > 0:
                self.file_map[self.current_file_id] = zip_file
                logger.info(f"Created ZIP archive: {zip_file} ({zip_size / 1024 / 1024:.1f} MB)")
                
                # Log contents for debugging
                file_count = len(list(output_dir.rglob('*')))
                logger.debug(f"ZIP contains {file_count} files from {output_dir}")
            else:
                logger.error(f"Failed to create ZIP archive: {zip_file}")
                
        except Exception as e:
            logger.error(f"Failed to create ZIP archive: {e}")
            logger.exception("Full ZIP creation error details:")
    
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

class MultiCamServer:
    def __init__(self, device: MultiCamDevice):
        self.device = device
        self.clients = set()
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle client connection"""
        client_addr = writer.get_extra_info('peername')
        logger.info(f"Client connected: {client_addr}")
        self.clients.add(writer)
        
        try:
            while True:
                # Read data - handle both raw JSON and length-prefixed protocols
                logger.debug(f"Waiting for message from {client_addr}")
                
                # Peek at first few bytes to determine protocol
                first_bytes = await reader.read(4)
                if not first_bytes:
                    break
                
                # Check if it looks like length prefix (starts with reasonable binary length)
                # or JSON (starts with { which is 0x7b)
                if first_bytes[0] == ord('{'):
                    # Raw JSON protocol (like iOS)
                    logger.debug(f"Using raw JSON protocol from {client_addr}")
                    
                    # Read rest of JSON message
                    remaining_data = await reader.read(1024)  # Read up to 1KB more
                    full_data = first_bytes + remaining_data
                    
                    # Find the end of JSON message
                    try:
                        json_str = full_data.decode('utf-8').rstrip('\x00\n\r')
                        message = json.loads(json_str)
                        logger.info(f"Received command from {client_addr}: {message}")
                        
                        # Process command
                        response = await self._process_command(message)
                        logger.debug(f"Response to {client_addr}: {response if isinstance(response, dict) else 'binary_data'}")
                        
                        # Send raw JSON response (no length prefix)
                        if isinstance(response, dict):
                            response_json = json.dumps(response).encode('utf-8')
                            logger.debug(f"Sending raw JSON response to {client_addr}: {len(response_json)} bytes")
                            writer.write(response_json)
                            await writer.drain()
                        elif isinstance(response, tuple):  # Binary file transfer
                            header, file_data = response
                            header_json = json.dumps(header).encode('utf-8')
                            header_length = struct.pack('>I', len(header_json))
                            logger.info(f"Sending binary file to {client_addr}: {len(file_data)} bytes")
                            
                            # Send header length + header + file data (keep this format for files)
                            writer.write(header_length + header_json + file_data)
                            await writer.drain()
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON from {client_addr}: {e}")
                        break
                else:
                    # Length-prefixed protocol (like test client)
                    logger.debug(f"Using length-prefixed protocol from {client_addr}")
                    message_length = struct.unpack('>I', first_bytes)[0]
                    logger.debug(f"Message length: {message_length} bytes from {client_addr}")
                    
                    # Sanity check message length
                    if message_length > 1000000:  # 1MB limit
                        logger.error(f"Message length too large ({message_length}), disconnecting {client_addr}")
                        break
                    
                    # Read the JSON message
                    message_data = await reader.readexactly(message_length)
                    logger.debug(f"Raw message data: {message_data[:100]}... from {client_addr}")
                    message = json.loads(message_data.decode('utf-8'))
                    logger.info(f"Received command from {client_addr}: {message}")
                    
                    # Process command
                    response = await self._process_command(message)
                    logger.debug(f"Response to {client_addr}: {response if isinstance(response, dict) else 'binary_data'}")
                    
                    # Send length-prefixed response
                    if isinstance(response, dict):
                        response_json = json.dumps(response).encode('utf-8')
                        response_length = struct.pack('>I', len(response_json))
                        logger.debug(f"Sending length-prefixed JSON response to {client_addr}: {len(response_json)} bytes")
                        writer.write(response_length + response_json)
                        await writer.drain()
                    elif isinstance(response, tuple):  # Binary file transfer
                        header, file_data = response
                        header_json = json.dumps(header).encode('utf-8')
                        header_length = struct.pack('>I', len(header_json))
                        logger.info(f"Sending binary file to {client_addr}: {len(file_data)} bytes")
                        
                        # Send header length + header + file data
                        writer.write(header_length + header_json + file_data)
                        await writer.drain()
        
        except asyncio.IncompleteReadError:
            logger.info(f"Client disconnected: {client_addr}")
        except Exception as e:
            logger.error(f"Error handling client {client_addr}: {e}")
        finally:
            self.clients.discard(writer)
            writer.close()
            await writer.wait_closed()
    
    async def _process_command(self, message: Dict[str, Any]) -> Any:
        """Process incoming command message"""
        command = message.get('command')
        logger.info(f"Processing command: {command}")
        
        if command == 'START_RECORDING':
            timestamp = message.get('timestamp')
            logger.info(f"START_RECORDING command with timestamp: {timestamp}")
            return await self.device.start_recording(timestamp)
        
        elif command == 'STOP_RECORDING':
            logger.info("STOP_RECORDING command received")
            return await self.device.stop_recording()
        
        elif command == 'DEVICE_STATUS':
            logger.debug("DEVICE_STATUS command received")
            return self.device.get_device_status()
        
        elif command == 'HEARTBEAT':
            logger.debug("HEARTBEAT command received")
            return {"status": "Heartbeat acknowledged"}
        
        elif command == 'GET_VIDEO':
            file_id = message.get('fileId')
            logger.info(f"GET_VIDEO command for fileId: {file_id}")
            if not file_id:
                return {"status": "Missing fileId parameter"}
            
            video_info = self.device.get_video_info(file_id)
            if not video_info:
                logger.warning(f"Video file not found: {file_id}")
                return {"status": "File not found"}
            
            # Read file data
            try:
                with open(video_info['filePath'], 'rb') as f:
                    file_data = f.read()
                
                header = {
                    "fileId": video_info['fileId'],
                    "fileName": video_info['fileName'], 
                    "fileSize": len(file_data)
                }
                
                return (header, file_data)  # Tuple for binary transfer
                
            except Exception as e:
                return {"status": f"Error reading file: {e}"}
        
        else:
            logger.warning(f"Unknown command received: {command}")
            return {"status": f"Unknown command: {command}"}
    
    async def start(self):
        """Start the TCP server"""
        logger.info(f"Starting TCP server on 0.0.0.0:{self.device.port}")
        server = await asyncio.start_server(
            self.handle_client,
            '0.0.0.0',
            self.device.port
        )
        
        logger.info(f"MultiCam server listening on port {self.device.port}")
        return server

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Create device and server
    device = MultiCamDevice()
    server_instance = MultiCamServer(device)
    
    # Start mDNS
    await device.start_mdns()
    
    try:
        # Start TCP server
        server = await server_instance.start()
        
        # Run forever
        async with server:
            await server.serve_forever()
    
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await device.stop_mdns()

if __name__ == "__main__":
    asyncio.run(main())