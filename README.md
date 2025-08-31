# OAK Controller RPi - MultiCam API

This project implements a MultiCam API server for OAK (OpenCV AI Kit) devices on Raspberry Pi, providing network-discoverable camera recording functionality compatible with other multiCam device implementations.

## Features

- **mDNS Service Discovery**: Advertises as `_multicam._tcp.local.` service
- **TCP Command Interface**: Handles recording commands on port 8080
- **Synchronized Recording**: Support for scheduled recording starts
- **Binary File Transfer**: Efficient video file downloads
- **OAK Integration**: Uses existing DepthAI camera recording pipeline

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure you have `ffmpeg` installed for video format conversion:
```bash
# On Raspberry Pi OS
sudo apt update
sudo apt install ffmpeg
```

## Usage

### Start the MultiCam Server

```bash
python3 run_multicam_server.py
```

Options:
- `--port PORT`: TCP server port (default: 8080)  
- `--videos-dir DIR`: Video storage directory (default: ./videos)
- `--log-level LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

### Test the Server

```bash
python3 test_client.py
```

### Verify mDNS Advertisement

```bash
dns-sd -B _multicam._tcp local.
```

## API Commands

The server accepts JSON commands over TCP with the following format:

### Command Structure
```json
{
  "command": "COMMAND_NAME",
  "parameter": "value"
}
```

### Supported Commands

#### START_RECORDING
Start recording immediately or at scheduled time:
```json
{"command": "START_RECORDING"}
{"command": "START_RECORDING", "timestamp": 1640995200.0}
```

Response:
- Immediate: `{"status": "Command received", "isRecording": true}`
- Scheduled: `{"status": "Scheduled recording accepted", "isRecording": false}`

#### STOP_RECORDING  
Stop active recording:
```json
{"command": "STOP_RECORDING"}
```

Response:
```json
{
  "status": "Recording stopped", 
  "isRecording": false,
  "fileId": "video_1640995200"
}
```

#### DEVICE_STATUS
Get current device state:
```json
{"command": "DEVICE_STATUS"}
```

Response:
```json
{
  "status": "ready",
  "isRecording": false,
  "deviceId": "550e8400-e29b-41d4-a716-446655440000"
}
```

#### HEARTBEAT
Keep-alive ping:
```json
{"command": "HEARTBEAT"}
```

Response:
```json
{"status": "Heartbeat acknowledged"}
```

#### GET_VIDEO
Download recorded video file:
```json
{"command": "GET_VIDEO", "fileId": "video_1640995200"}
```

Response: Binary file transfer protocol
1. 4-byte header length (big-endian uint32)
2. JSON header with file info
3. Raw file bytes

## File Structure

```
OAK-Controller-Rpi/
├── src/oak_controller_rpi/
│   ├── __init__.py
│   ├── __main__.py
│   └── server.py              # Main server implementation
├── scripts/
│   └── simpleRecorder.py      # OAK recording script
├── run_multicam_server.py     # Server startup script
├── test_client.py             # Test client
├── requirements.txt           # Dependencies
└── README.md                  # This file
```

## Technical Details

### mDNS Service
- Service Type: `_multicam._tcp.local.`
- Service Name: `multiCam-{deviceId}`  
- Port: 8080
- Persistent device ID stored in `~/.multicam_device_id`

### Recording Pipeline
1. Uses `simpleRecorder.py` for OAK camera control
2. Records H.265 video streams from stereo cameras
3. Converts to .mov format using ffmpeg
4. Stores files in configurable videos directory
5. Maintains file mapping for GET_VIDEO requests

### Protocol Details
- TCP connections on port 8080
- UTF-8 JSON for commands
- Big-endian length prefixes for message framing
- Binary transfer for video files
- Graceful error handling and connection management

## Testing Checklist

- ☑ Device discoverable via `dns-sd -B _multicam._tcp local.`
- ☑ Controller connects to TCP port 8080  
- ☑ DEVICE_STATUS returns proper JSON response
- ☑ START_RECORDING/STOP_RECORDING work correctly
- ☑ GET_VIDEO delivers files with correct binary protocol
- ☑ Multiple controllers can connect simultaneously
- ☑ Scheduled recording respects timing constraints

## Compatibility

This implementation provides a drop-in replacement for iPhone multiCam nodes, maintaining API compatibility while leveraging OAK hardware capabilities.