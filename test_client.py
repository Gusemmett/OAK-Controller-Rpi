#!/usr/bin/env python3

"""
Test client for MultiCam OAK controller server

This script tests the basic functionality of the MultiCam server by sending
various commands and checking responses.
"""

import asyncio
import json
import socket
import struct
import time

class MultiCamTestClient:
    def __init__(self, host='127.0.0.1', port=8080):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
    
    async def connect(self):
        """Connect to the MultiCam server"""
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        print(f"Connected to {self.host}:{self.port}")
    
    async def disconnect(self):
        """Disconnect from the server"""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        print("Disconnected")
    
    async def send_command(self, command_dict):
        """Send a command and return the response"""
        # Send command
        message = json.dumps(command_dict).encode('utf-8')
        length = struct.pack('>I', len(message))
        self.writer.write(length + message)
        await self.writer.drain()
        
        # Read response
        response_length_data = await self.reader.readexactly(4)
        response_length = struct.unpack('>I', response_length_data)[0]
        response_data = await self.reader.readexactly(response_length)
        
        return json.loads(response_data.decode('utf-8'))
    
    async def test_device_status(self):
        """Test DEVICE_STATUS command"""
        print("\n--- Testing DEVICE_STATUS ---")
        response = await self.send_command({"command": "DEVICE_STATUS"})
        print(f"Response: {json.dumps(response, indent=2)}")
        return response
    
    async def test_heartbeat(self):
        """Test HEARTBEAT command"""
        print("\n--- Testing HEARTBEAT ---")
        response = await self.send_command({"command": "HEARTBEAT"})
        print(f"Response: {json.dumps(response, indent=2)}")
        return response
    
    async def test_start_recording(self):
        """Test START_RECORDING command"""
        print("\n--- Testing START_RECORDING (immediate) ---")
        response = await self.send_command({"command": "START_RECORDING"})
        print(f"Response: {json.dumps(response, indent=2)}")
        return response
    
    async def test_scheduled_recording(self):
        """Test START_RECORDING with scheduled time"""
        print("\n--- Testing START_RECORDING (scheduled) ---")
        future_time = time.time() + 2  # 2 seconds from now
        response = await self.send_command({
            "command": "START_RECORDING",
            "timestamp": future_time
        })
        print(f"Response: {json.dumps(response, indent=2)}")
        return response
    
    async def test_stop_recording(self):
        """Test STOP_RECORDING command"""
        print("\n--- Testing STOP_RECORDING ---")
        response = await self.send_command({"command": "STOP_RECORDING"})
        print(f"Response: {json.dumps(response, indent=2)}")
        return response
    
    async def test_get_video(self, file_id):
        """Test GET_VIDEO command"""
        print(f"\n--- Testing GET_VIDEO for {file_id} ---")
        
        # Send GET_VIDEO command
        message = json.dumps({"command": "GET_VIDEO", "fileId": file_id}).encode('utf-8')
        length = struct.pack('>I', len(message))
        self.writer.write(length + message)
        await self.writer.drain()
        
        # Read header length
        header_length_data = await self.reader.readexactly(4)
        header_length = struct.unpack('>I', header_length_data)[0]
        
        # Read header
        header_data = await self.reader.readexactly(header_length)
        header = json.loads(header_data.decode('utf-8'))
        print(f"File header: {json.dumps(header, indent=2)}")
        
        if 'fileSize' in header:
            # Read file data (but don't save it for test)
            file_size = header['fileSize']
            print(f"Reading {file_size} bytes of file data...")
            file_data = await self.reader.readexactly(file_size)
            print(f"Successfully received {len(file_data)} bytes")
        
        return header
    
    async def run_tests(self):
        """Run all tests"""
        try:
            await self.connect()
            
            # Basic tests
            await self.test_device_status()
            await self.test_heartbeat()
            
            # Recording tests
            start_response = await self.test_start_recording()
            
            if start_response.get('isRecording'):
                # Wait a bit for recording
                print("\nWaiting 5 seconds for recording...")
                await asyncio.sleep(5)
                
                # Stop recording
                stop_response = await self.test_stop_recording()
                
                # Test video retrieval if we got a file ID
                if 'fileId' in stop_response:
                    file_id = stop_response['fileId']
                    await self.test_get_video(file_id)
                else:
                    await self.test_get_video("nonexistent_file")  # Test error case
            
            # Test scheduled recording
            await self.test_scheduled_recording()
            
            print("\n--- All tests completed ---")
            
        except Exception as e:
            print(f"Test error: {e}")
        finally:
            await self.disconnect()

async def main():
    client = MultiCamTestClient()
    await client.run_tests()

if __name__ == "__main__":
    asyncio.run(main())