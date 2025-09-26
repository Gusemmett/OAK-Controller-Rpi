#!/usr/bin/env python3

import asyncio
import json
import logging
import struct
from typing import Any, Dict

from .multicam_device import DeviceStatus

logger = logging.getLogger(__name__)


class MultiCamServer:
    def __init__(self, device):
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
            return {"status": DeviceStatus.COMMAND_RECEIVED.value}
        
        elif command == 'GET_VIDEO':
            file_id = message.get('fileId')
            logger.info(f"GET_VIDEO command for fileId: {file_id}")
            if not file_id:
                return {"status": DeviceStatus.ERROR.value}
            
            video_info = self.device.get_video_info(file_id)
            if not video_info:
                logger.warning(f"Video file not found: {file_id}")
                return {"status": DeviceStatus.FILE_NOT_FOUND.value}
            
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
                return {"status": DeviceStatus.ERROR.value}
        
        else:
            logger.warning(f"Unknown command received: {command}")
            return {"status": DeviceStatus.ERROR.value}
    
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


