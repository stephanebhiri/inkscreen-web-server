#!/usr/bin/env python3
from flask import Flask, jsonify, Response
import hashlib
import os
import time

app = Flask(__name__)

# Test image fixe - use real image for testing  
TEST_IMAGE = "./test_image.jpg"
CURRENT_IMAGE_FILE = "./current_image.txt"  # File to store current image path

def get_current_image():
    """Get current image path from file or default"""
    if os.path.exists(CURRENT_IMAGE_FILE):
        with open(CURRENT_IMAGE_FILE, 'r') as f:
            return f.read().strip()
    return TEST_IMAGE

@app.route('/api/image/info')
def image_info():
    """Info sur l'image courante avec hash"""
    current_image = get_current_image()
    if not os.path.exists(current_image):
        return jsonify({"error": "No image"}), 404
    
    # Hash MD5
    hash_md5 = hashlib.md5()
    with open(current_image, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    file_hash = hash_md5.hexdigest()[:12]
    
    # Return JSON with spaces to match Flask format expected by ESP32
    from flask import Response
    import json
    data = {
        'hash': file_hash,
        'image_name': os.path.basename(current_image),
        'timestamp': int(os.path.getmtime(current_image))
    }
    return Response(json.dumps(data, separators=(', ', ': ')), 
                   mimetype='application/json')

@app.route('/api/set_current', methods=['POST'])
def set_current():
    """Set current image"""
    from flask import request
    data = request.get_json()
    if not data or 'image_path' not in data:
        return jsonify({'error': 'No image path provided'}), 400
    
    image_path = data['image_path']
    if not os.path.exists(image_path):
        return jsonify({'error': 'Image not found'}), 404
    
    # Write current image to file
    with open(CURRENT_IMAGE_FILE, 'w') as f:
        f.write(image_path)
    
    return jsonify({
        'success': True,
        'current_image': os.path.basename(image_path),
        'message': f'Current image set to {os.path.basename(image_path)}'
    })

@app.route('/api/image/stream')
def image_stream():
    """Stream image convertie ligne par ligne"""
    current_image = get_current_image()
    if not os.path.exists(current_image):
        return "No image", 404
    
    print(f"[HTTP] Streaming {current_image}")
    
    # Import Sierra SORBET
    try:
        from dither_sierra_sorbet import sierra_sorbet_dither
        import numpy as np
        from PIL import Image, ImageEnhance
        SORBET_AVAILABLE = True
    except ImportError:
        print("[ERROR] Sierra SORBET not available")
        return "Dithering not available", 500
    
    # Conversion Sierra SORBET
    EPD_W, EPD_H = 1200, 1600
    PALETTE_RGB = [(0,0,0), (255,255,255), (255,255,0), (255,0,0), (0,0,255), (0,255,0)]
    CODE_MAP = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]
    
    def crop_center_zoom(im, target_ratio=12/16):
        original_width, original_height = im.size
        original_ratio = original_width / original_height
        
        if original_ratio > target_ratio:
            new_width = int(original_height * target_ratio)
            left = (original_width - new_width) // 2
            crop_box = (left, 0, left + new_width, original_height)
        else:
            new_height = int(original_width / target_ratio)
            top = (original_height - new_height) // 2
            crop_box = (0, top, original_width, top + new_height)
        return im.crop(crop_box)
    
    def enhance_image(im):
        from PIL import ImageEnhance
        enhancer = ImageEnhance.Contrast(im)
        im = enhancer.enhance(1.2)
        enhancer = ImageEnhance.Color(im)
        im = enhancer.enhance(1.3)
        enhancer = ImageEnhance.Brightness(im)
        im = enhancer.enhance(1.05)
        return im
    
    # Load et convert
    im = Image.open(current_image).convert("RGB")
    im = crop_center_zoom(im)
    im = im.resize((EPD_W, EPD_H), Image.Resampling.LANCZOS)
    im = enhance_image(im)
    
    # Sierra SORBET dithering
    img_array = np.array(im, dtype=np.float32)
    palette_np = np.array(PALETTE_RGB, dtype=np.float32)
    indices_2d = sierra_sorbet_dither(img_array, palette_np)
    indices = indices_2d.flatten()
    
    def generate_stream():
        BYTES_PER_LINE_HALF = 300  # 600 pixels / 2
        
        # Master (left 600px) - 1600 lines
        for y in range(EPD_H):
            line_data = bytearray(BYTES_PER_LINE_HALF)
            for x in range(0, 600, 2):
                pixel_idx = y * EPD_W + x
                a = CODE_MAP[indices[pixel_idx]]
                b = CODE_MAP[indices[pixel_idx + 1]]
                line_data[x//2] = (a << 4) | (b & 0x0F)
            yield bytes(line_data)
        
        # Slave (right 600px) - 1600 lines  
        for y in range(EPD_H):
            line_data = bytearray(BYTES_PER_LINE_HALF)
            for x in range(0, 600, 2):
                pixel_idx = y * EPD_W + (600 + x)
                a = CODE_MAP[indices[pixel_idx]]
                b = CODE_MAP[indices[pixel_idx + 1]]
                line_data[x//2] = (a << 4) | (b & 0x0F)
            yield bytes(line_data)
    
    return Response(generate_stream(), 
                   mimetype='application/octet-stream',
                   headers={'Content-Length': str(300 * 1600 * 2)})  # 960KB total

if __name__ == '__main__':
    print(f"Test HTTP server - serving: {TEST_IMAGE}")
    app.run(host='0.0.0.0', port=5001, debug=True)