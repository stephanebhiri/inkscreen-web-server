#!/usr/bin/env python3
import sys
import socket
from PIL import Image, ImageEnhance
import numpy as np
import time

# Importer la version SORBET compilée
try:
    from dither_sierra_sorbet import sierra_sorbet_dither
    SORBET_AVAILABLE = True
    print("[INFO] Using compiled Sierra SORBET dithering (FAST + BALANCED)")
except ImportError:
    SORBET_AVAILABLE = False
    print("[ERROR] Sierra SORBET module not found - recompile dither_sierra_sorbet.pyx")

EPD_W, EPD_H = 1200, 1600
PORT = 3333

PALETTE_RGB = [
    (0,0,0),         # 0 BLACK
    (255,255,255),   # 1 WHITE  
    (255,255,0),     # 2 YELLOW
    (255,0,0),       # 3 RED
    (0,0,255),       # 4 BLUE
    (0,255,0),       # 5 GREEN
]
CODE_MAP = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]

def make_palette_image():
    pal = []
    for rgb in PALETTE_RGB:
        pal.extend(rgb)
    pal.extend([0,0,0] * (256 - len(PALETTE_RGB)))
    p = Image.new("P", (1,1))
    p.putpalette(pal)
    return p

def enhance_image(im):
    enhancer = ImageEnhance.Contrast(im)
    im = enhancer.enhance(1.2)
    enhancer = ImageEnhance.Color(im)
    im = enhancer.enhance(1.3)
    enhancer = ImageEnhance.Brightness(im)
    im = enhancer.enhance(1.05)
    return im

def pack_half(indices, x0, x1):
    out = bytearray((x1-x0)//2 * EPD_H)
    off = 0
    for y in range(EPD_H):
        row = indices[y*EPD_W + x0 : y*EPD_W + x1]
        for i in range(0, 600, 2):
            a = CODE_MAP[row[i]]
            b = CODE_MAP[row[i+1]]
            out[off] = (a << 4) | (b & 0x0F)
            off += 1
    return bytes(out)

def crop_center_zoom(im, target_ratio=12/16):
    """Crop l'image en mode zoom pour ratio 12/16 (1200/1600)"""
    original_width, original_height = im.size
    original_ratio = original_width / original_height
    
    print(f"[INFO] Image originale: {original_width}x{original_height} (ratio {original_ratio:.3f})")
    print(f"[INFO] Ratio cible: {target_ratio:.3f} (12/16)")
    
    if original_ratio > target_ratio:
        # Image trop large - crop horizontalement (garder la hauteur)
        new_width = int(original_height * target_ratio)
        left = (original_width - new_width) // 2
        right = left + new_width
        crop_box = (left, 0, right, original_height)
        print(f"[INFO] Crop horizontal: {new_width}x{original_height}")
    else:
        # Image trop haute - crop verticalement (garder la largeur)
        new_height = int(original_width / target_ratio)
        top = (original_height - new_height) // 2
        bottom = top + new_height
        crop_box = (0, top, original_width, bottom)
        print(f"[INFO] Crop vertical: {original_width}x{new_height}")
    
    return im.crop(crop_box)

def build_frame(img_path):
    start_time = time.time()
    
    im = Image.open(img_path).convert("RGB")
    
    # NOUVEAU: Crop en mode zoom 12/16
    im = crop_center_zoom(im)
    
    # Puis resize à la taille finale
    im = im.resize((EPD_W, EPD_H), Image.Resampling.LANCZOS)
    im = enhance_image(im)
    
    print(f"[TIME] Load & resize: {time.time() - start_time:.2f}s")
    dither_time = time.time()
    
    if SORBET_AVAILABLE:
        # Utiliser Sierra SORBET compilé (ultra-rapide)
        img_array = np.array(im, dtype=np.float32)
        palette_np = np.array(PALETTE_RGB, dtype=np.float32)
        indices_2d = sierra_sorbet_dither(img_array, palette_np)
        idx = indices_2d.flatten()
    else:
        # Fallback si compilation échouée
        pal_img = make_palette_image()
        im_p = im.quantize(palette=pal_img, dither=Image.FLOYDSTEINBERG)
        idx = list(im_p.getdata())
    
    print(f"[TIME] Dithering: {time.time() - dither_time:.2f}s")
    pack_time = time.time()
    
    left = pack_half(idx, 0, 600)
    right = pack_half(idx, 600, 1200)
    
    print(f"[TIME] Packing: {time.time() - pack_time:.2f}s")
    print(f"[TIME] Total frame: {time.time() - start_time:.2f}s")
    
    return left, right

def send(img_path, host):
    left, right = build_frame(img_path)
    print(f"[INFO] left={len(left)} right={len(right)} (expect 480000 each)")
    hdr = b'E6' + EPD_W.to_bytes(2,'little') + EPD_H.to_bytes(2,'little') + b'\x00'
    
    send_time = time.time()
    with socket.create_connection((host, PORT), timeout=10) as s:
        s.sendall(hdr)
        s.sendall(left)
        s.sendall(right)
    print(f"[TIME] Network send: {time.time() - send_time:.2f}s")
    print("OK sent.")

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[2] != "--host":
        print("Usage: push_epaper_sierra_sorbet_fast.py IMAGE --host IP")
        sys.exit(1)
    
    img_path = sys.argv[1]
    host_ip = sys.argv[3]
    
    total_time = time.time()
    send(img_path, host_ip)
    print(f"[TIME] TOTAL: {time.time() - total_time:.2f}s")