#!/usr/bin/env python3

import asyncio
import logging

from .multicam_device import MultiCamDevice
from .tcp_server import MultiCamServer

__all__ = ["MultiCamDevice", "MultiCamServer"]

logger = logging.getLogger(__name__)


async def main():
    logging.basicConfig(level=logging.INFO)
    
    device = MultiCamDevice()
    server_instance = MultiCamServer(device)
    
    await device.start_mdns()
    
    try:
        server = await server_instance.start()
        async with server:
            await server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await device.stop_mdns()


if __name__ == "__main__":
    asyncio.run(main())