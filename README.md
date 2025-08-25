# Inkscreen Web

Flask web app for managing e-paper displays via ESP32 with **HTTP polling architecture**.

## 🔄 Architecture Migration

**NEW**: HTTP Polling architecture provides better network compatibility and 10x battery life improvement for ESP32 devices.

### Before (TCP Push)
- Web server pushes images to ESP32 on port 3333
- ESP32 runs TCP server (always-on WiFi)
- NAT/firewall connection issues
- ~80mA continuous power consumption

### After (HTTP Polling) 
- ESP32 polls web server via HTTP (port 5001)
- Hash-based change detection prevents unnecessary updates  
- Manual override system allows "Set Current" during active playlists
- ~1-10mA average power consumption with Light Sleep

## Features

- **Authentication**: HTTP Basic Auth with .env configuration
- **Image Management**: Upload, organize, and manage image collections
- **HTTP Polling**: New endpoints for ESP32 HTTP polling architecture
- **Legacy TCP Support**: Still supports original TCP push for older ESP32 code
- **Slideshow System**: Automated playlists with scheduling
- **Manual Override**: "Set Current" works even during active playlists
- **Image Processing**: Sierra SORBET dithering for 6-color e-ink displays
- **Thumbnail Generation**: Automatic WebP thumbnails for web interface

## Setup

```bash
# Clone and setup
git clone https://github.com/stephanebhiri/inkscreen-web.git
cd inkscreen-web

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env  # Edit configuration

# Run server (HTTP polling on port 5001)
python app_ultimate_enhanced.py
```

## Configuration

Create `.env` file with:
```bash
FLASK_SECRET_KEY=your-unique-secret-key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password

# Legacy TCP Push settings (optional)
ESP32_IP=192.168.1.49
ESP32_PORT=3333
PUSH_SCRIPT=./push_epaper_sierra_sorbet_fast.py
```

## HTTP Polling Endpoints

For ESP32 HTTP polling architecture:

- `GET /api/image/info` - Returns current image hash and metadata
- `GET /api/image/stream` - Streams binary image data (300 bytes per line)
- `POST /api/set_current` - Set current image (manual override)

## ESP32 Compatibility

### HTTP Polling (Recommended)
Works with: https://github.com/stephanebhiri/esp32-eink-frame/tree/feature/http-polling
- HTTP client polling on port 5001
- Hash-based change detection
- 10x better battery life
- Better network compatibility

### Legacy TCP Push
Works with: https://github.com/stephanebhiri/esp32-eink-frame/tree/main  
- TCP server on ESP32 port 3333
- Push-based updates
- Original architecture

## Requirements

- Python 3.8+
- Flask
- Pillow
- See requirements.txt for full list
