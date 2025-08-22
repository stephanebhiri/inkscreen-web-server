# Inkscreen Web

Flask web app for managing e-paper displays via ESP32.

## Features

- HTTP Basic Auth with .env configuration
- Upload and manage images
- Stream to ESP32 displays over TCP
- Web interface with authentication
- Automated slideshow mode
- Thumbnail generation

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

# Run server
python app_ultimate_enhanced.py
```

## Configuration

Create `.env` file with:
```bash
FLASK_SECRET_KEY=your-unique-secret-key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-secure-password
ESP32_IP=192.168.1.49
ESP32_PORT=3333
```

## ESP32 Compatibility

Works with: https://github.com/stephanebhiri/esp32-eink-frame
- Waveshare 13.3" displays
- TCP streaming on port 3333
- Real-time image optimization

## Requirements

- Python 3.8+
- Flask
- Pillow
- See requirements.txt for full list
