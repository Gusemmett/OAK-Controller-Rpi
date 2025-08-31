#!/usr/bin/env python3

"""
MultiCam Server Startup Script

This script starts the MultiCam OAK controller server that provides:
- mDNS service discovery (_multicam._tcp.local.)
- TCP server on port 8080 for command handling
- Integration with OAK camera recording via simpleRecorder.py

Commands supported:
- START_RECORDING: Start recording (immediate or scheduled)
- STOP_RECORDING: Stop recording and finalize video file
- DEVICE_STATUS: Get current device state
- HEARTBEAT: Keep-alive ping
- GET_VIDEO: Download recorded video files

Usage:
    python3 run_multicam_server.py [--port PORT] [--videos-dir DIR]
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add src to path so we can import our module
sys.path.insert(0, str(Path(__file__).parent / "src"))

from oak_controller_rpi.server import MultiCamDevice, MultiCamServer


async def main():
    parser = argparse.ArgumentParser(
        description="Start MultiCam OAK controller server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--port", 
        type=int, 
        default=8080,
        help="TCP server port (default: 8080)"
    )
    parser.add_argument(
        "--videos-dir",
        type=str,
        default="./videos",
        help="Directory to store recorded videos (default: ./videos)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    logger = logging.getLogger(__name__)
    
    # Create videos directory if it doesn't exist
    videos_path = Path(args.videos_dir)
    videos_path.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Starting MultiCam server on port {args.port}")
    logger.info(f"Videos will be stored in: {videos_path.absolute()}")
    
    # Create device and server
    device = MultiCamDevice(port=args.port, videos_dir=str(videos_path))
    server_instance = MultiCamServer(device)
    
    # Start mDNS
    try:
        await device.start_mdns()
        logger.info(f"mDNS service advertised: multiCam-{device.device_id}")
    except Exception as e:
        logger.error(f"Failed to start mDNS: {e}")
        return 1
    
    try:
        # Start TCP server
        server = await server_instance.start()
        
        logger.info("MultiCam server is ready! Press Ctrl+C to stop.")
        logger.info(f"Device ID: {device.device_id}")
        logger.info("Test with: dns-sd -B _multicam._tcp local.")
        
        # Run forever
        async with server:
            await server.serve_forever()
    
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Server error: {e}")
        return 1
    finally:
        logger.info("Stopping mDNS service...")
        await device.stop_mdns()
        logger.info("Server stopped.")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))