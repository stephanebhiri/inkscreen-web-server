#!/usr/bin/env python3
import os
import socket
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime

def get_server_info():
    """Get dynamic server information"""
    hostname = socket.gethostname()
    
    # Get local IP (try multiple methods)
    try:
        # Connect to external host to find local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"
    
    return {
        'hostname': hostname,
        'ip': local_ip,
        'port': 5001,
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'url': f"http://{local_ip}:5001"
    }

def create_dynamic_welcome_image(server_info, output_path):
    """Create welcome image with dynamic server info"""
    WIDTH = 1200
    HEIGHT = 1600
    
    # 6-color E-ink palette
    COLORS = {
        'BLACK': (0, 0, 0),
        'WHITE': (255, 255, 255),
        'RED': (255, 0, 0),
        'YELLOW': (255, 255, 0),
        'BLUE': (0, 0, 255),
        'GREEN': (0, 255, 0)
    }
    
    # Create base image
    img = Image.new('RGB', (WIDTH, HEIGHT), COLORS['WHITE'])
    draw = ImageDraw.Draw(img)
    
    # Try to load fonts
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 100)
        subtitle_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 55)
        body_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
        info_font = ImageFont.truetype("/System/Library/Fonts/Monaco.ttc", 35)
    except:
        # Fallback fonts for Linux
        try:
            title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 100)
            subtitle_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 55)
            body_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
            info_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 35)
        except:
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default()
            body_font = ImageFont.load_default()
            info_font = ImageFont.load_default()
    
    # Title
    title = "INKSCREEN READY"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    title_width = title_bbox[2] - title_bbox[0]
    title_x = (WIDTH - title_width) // 2
    draw.text((title_x, 120), title, font=title_font, fill=COLORS['BLACK'])
    
    # Subtitle
    subtitle = "E-Paper Display Server"
    subtitle_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    subtitle_width = subtitle_bbox[2] - subtitle_bbox[0]
    subtitle_x = (WIDTH - subtitle_width) // 2
    draw.text((subtitle_x, 250), subtitle, font=subtitle_font, fill=COLORS['BLUE'])
    
    # Decorative line
    draw.rectangle([200, 350, WIDTH-200, 360], fill=COLORS['RED'])
    
    # Server info section
    y_pos = 450
    info_items = [
        f"🌐 Server: {server_info['hostname']}",
        f"📍 IP Address: {server_info['ip']}",
        f"🔗 Port: {server_info['port']}",
        f"🌍 Web Interface:",
        f"   {server_info['url']}",
        "",
        f"⏰ Started: {server_info['timestamp']}",
        "",
        "🔋 ESP32 Features:",
        "• HTTP Polling Architecture",
        "• Real-time Battery Monitoring", 
        "• WiFi Signal Strength",
        "• Memory Usage Tracking",
        "• Light Sleep Power Management",
        "",
        "🎨 Display Features:",
        "• 1200×1600 Resolution",
        "• 6-Color E-Ink Display",
        "• Sierra SORBET Dithering",
        "• Automatic Image Processing",
        "",
        "📱 Access this interface from:",
        "• Desktop Browser",
        "• Mobile Device", 
        "• Any device on your network"
    ]
    
    for item in info_items:
        if item == "":
            y_pos += 30
            continue
            
        # Choose color based on content
        if item.startswith('🌐') or item.startswith('📍') or item.startswith('🔗'):
            color = COLORS['RED']
            font = info_font
        elif item.startswith('🌍') or item.startswith('📱'):
            color = COLORS['BLUE'] 
            font = body_font
        elif item.startswith('   http://'):
            color = COLORS['GREEN']
            font = info_font
        elif item.startswith('⏰'):
            color = COLORS['BLACK']
            font = info_font
        elif item.startswith('🔋') or item.startswith('🎨'):
            color = COLORS['BLUE']
            font = body_font
        elif item.startswith('•'):
            color = COLORS['BLACK']
            font = body_font
        else:
            color = COLORS['BLACK']
            font = body_font
        
        # Center align main headers, left align details
        if item.startswith('🌍') or item.startswith('📱') or item.startswith('🔋') or item.startswith('🎨'):
            bbox = draw.textbbox((0, 0), item, font=font)
            width = bbox[2] - bbox[0]
            x = (WIDTH - width) // 2
        else:
            x = 150
            
        draw.text((x, y_pos), item, font=font, fill=color)
        y_pos += 50
    
    # Bottom decorative elements
    pattern_y = HEIGHT - 150
    for i, x in enumerate(range(100, WIDTH-100, 200)):
        color = [COLORS['RED'], COLORS['BLUE'], COLORS['GREEN']][i % 3]
        draw.rectangle([x, pattern_y, x + 150, pattern_y + 20], fill=color)
    
    # Save the image
    img.save(output_path, "JPEG", quality=95)
    return output_path

if __name__ == "__main__":
    server_info = get_server_info()
    output_path = "/home/debian/inkscreen_web/playlists/inkscreen_welcome.jpg"
    
    print("🎨 Generating dynamic welcome image...")
    print(f"   Server: {server_info['hostname']}")
    print(f"   IP: {server_info['ip']}:{server_info['port']}")
    print(f"   URL: {server_info['url']}")
    
    create_dynamic_welcome_image(server_info, output_path)
    print(f"✅ Welcome image generated: {output_path}")
    print(f"   Resolution: 1200×1600")
    print(f"   Timestamp: {server_info['timestamp']}")