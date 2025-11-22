#!/usr/bin/env python3

import asyncio
import json
import logging
import struct
import time
from typing import Any, Dict

from multicam_common.status import DeviceStatus, DeviceType
from multicam_common.commands import (
    CommandMessage, CommandType, StatusResponse,
    StopRecordingResponse, ErrorResponse, FileResponse,
    ListFilesResponse, FileMetadata
)

logger = logging.getLogger(__name__)


class MultiCamServer:
    def __init__(self, device):
        self.device = device
        self.clients = set()
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle client connection"""
        client_addr = writer.get_extra_info('peername')
        logger.debug(f"Client connected: {client_addr}")
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
                    # Increased buffer size to handle large IAM credentials (session tokens can be long)
                    remaining_data = await reader.read(8192)  # Read up to 8KB more
                    full_data = first_bytes + remaining_data

                    # Find the end of JSON message
                    try:
                        json_str = full_data.decode('utf-8').rstrip('\x00\n\r')
                        message = json.loads(json_str)
                        logger.debug(f"Received command from {client_addr}: {message}")
                        
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

                            # Clean up file after successful GET_VIDEO
                            if message.get('command') == 'GET_VIDEO':
                                file_name = message.get('fileName')
                                if file_name:
                                    logger.info(f"Deleting file after successful GET_VIDEO: {file_name}")
                                    await self.device._delete_uploaded_file(file_name)
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
                    logger.debug(f"Received command from {client_addr}: {message}")
                    
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

                        # Clean up file after successful GET_VIDEO
                        if message.get('command') == 'GET_VIDEO':
                            file_name = message.get('fileName')
                            if file_name:
                                logger.info(f"Deleting file after successful GET_VIDEO: {file_name}")
                                await self.device._delete_uploaded_file(file_name)
        
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
        try:
            # Parse command using CommandMessage
            cmd = CommandMessage.from_json(json.dumps(message))
            logger.debug(f"Processing command: {cmd.command}")

            if cmd.command == CommandType.START_RECORDING:
                logger.info(f"START_RECORDING command with timestamp: {cmd.timestamp}")
                response = await self.device.start_recording(cmd.timestamp)
                return json.loads(response.to_json())

            elif cmd.command == CommandType.STOP_RECORDING:
                logger.info("STOP_RECORDING command received")
                response = await self.device.stop_recording()
                return json.loads(response.to_json())

            elif cmd.command == CommandType.DEVICE_STATUS:
                logger.debug("DEVICE_STATUS command received")
                response = self.device.get_device_status()
                return json.loads(response.to_json())

            elif cmd.command == CommandType.HEARTBEAT:
                logger.debug("HEARTBEAT command received")
                response = StatusResponse(
                    deviceId=self.device.device_id,
                    status=DeviceStatus.COMMAND_RECEIVED.value,
                    timestamp=time.time(),
                    deviceType=DeviceType.OAK.value
                )
                return json.loads(response.to_json())

            elif cmd.command == CommandType.GET_VIDEO:
                logger.info(f"GET_VIDEO command for fileName: {cmd.fileName}")
                if not cmd.fileName:
                    error = ErrorResponse(
                        deviceId=self.device.device_id,
                        status=DeviceStatus.ERROR.value,
                        timestamp=time.time(),
                        message="fileName required for GET_VIDEO"
                    )
                    return json.loads(error.to_json())

                # Ensure video is finalized (creates ZIP if needed)
                logger.info(f"Ensuring video is finalized before GET_VIDEO: {cmd.fileName}")
                finalized = await self.device._ensure_video_finalized(cmd.fileName)
                if not finalized:
                    logger.error(f"Failed to finalize video for GET_VIDEO: {cmd.fileName}")
                    error = ErrorResponse(
                        deviceId=self.device.device_id,
                        status=DeviceStatus.ERROR.value,
                        timestamp=time.time(),
                        message=f"Failed to finalize video: {cmd.fileName}"
                    )
                    return json.loads(error.to_json())

                file_metadata = self.device.get_video_info(cmd.fileName)
                if not file_metadata:
                    logger.warning(f"Video file not found: {cmd.fileName}")
                    error = ErrorResponse(
                        deviceId=self.device.device_id,
                        status=DeviceStatus.FILE_NOT_FOUND.value,
                        timestamp=time.time(),
                        message=f"File not found: {cmd.fileName}"
                    )
                    return json.loads(error.to_json())

                # Read file data
                file_path = self.device.videos_dir / cmd.fileName
                with open(file_path, 'rb') as f:
                    file_data = f.read()

                header = FileResponse(
                    deviceId=self.device.device_id,
                    fileName=file_metadata.fileName,
                    fileSize=file_metadata.fileSize,
                    status=DeviceStatus.READY.value
                )

                return (json.loads(header.to_json()), file_data)

            elif cmd.command == CommandType.LIST_FILES:
                logger.info("LIST_FILES command received")
                files = []
                for file_path in self.device.videos_dir.glob("*.zip"):
                    stat = file_path.stat()
                    files.append(FileMetadata(
                        fileName=file_path.name,
                        fileSize=stat.st_size,
                        creationDate=stat.st_ctime,
                        modificationDate=stat.st_mtime
                    ))

                response = ListFilesResponse(
                    deviceId=self.device.device_id,
                    status=DeviceStatus.READY.value,
                    timestamp=time.time(),
                    files=files
                )
                return json.loads(response.to_json())

            elif cmd.command == CommandType.UPLOAD_TO_CLOUD:
                logger.info(f"UPLOAD_TO_CLOUD command for fileName: {cmd.fileName}")
                if not cmd.fileName:
                    error = ErrorResponse(
                        deviceId=self.device.device_id,
                        status=DeviceStatus.ERROR.value,
                        timestamp=time.time(),
                        message="fileName required for UPLOAD_TO_CLOUD"
                    )
                    return json.loads(error.to_json())

                # Check if using IAM credentials or presigned URL
                if (cmd.s3Bucket and cmd.s3Key and cmd.awsAccessKeyId and
                    cmd.awsSecretAccessKey and cmd.awsSessionToken and cmd.awsRegion):
                    # Use IAM credentials authentication
                    logger.info(f"Using IAM credentials for upload: {cmd.fileName}")
                    response = await self.device.upload_to_cloud(
                        file_name=cmd.fileName,
                        upload_url=None,
                        s3_bucket=cmd.s3Bucket,
                        s3_key=cmd.s3Key,
                        access_key_id=cmd.awsAccessKeyId,
                        secret_access_key=cmd.awsSecretAccessKey,
                        session_token=cmd.awsSessionToken,
                        region=cmd.awsRegion
                    )
                elif cmd.uploadUrl:
                    # Use presigned URL authentication
                    logger.info(f"Using presigned URL for upload: {cmd.fileName}")
                    response = await self.device.upload_to_cloud(
                        file_name=cmd.fileName,
                        upload_url=cmd.uploadUrl
                    )
                else:
                    error = ErrorResponse(
                        deviceId=self.device.device_id,
                        status=DeviceStatus.ERROR.value,
                        timestamp=time.time(),
                        message="Missing authentication credentials (uploadUrl or IAM credentials) for UPLOAD_TO_CLOUD"
                    )
                    return json.loads(error.to_json())

                return json.loads(response.to_json())

            else:
                logger.warning(f"Unknown command received: {cmd.command}")
                error = ErrorResponse(
                    deviceId=self.device.device_id,
                    status=DeviceStatus.ERROR.value,
                    timestamp=time.time(),
                    message=f"Unknown command: {cmd.command}"
                )
                return json.loads(error.to_json())

        except Exception as e:
            logger.error(f"Error processing command: {e}")
            logger.exception("Full exception details:")
            error = ErrorResponse(
                deviceId=self.device.device_id,
                status=DeviceStatus.ERROR.value,
                timestamp=time.time(),
                message=str(e)
            )
            return json.loads(error.to_json())
    
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


