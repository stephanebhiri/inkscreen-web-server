#!/usr/bin/env python3
# Inkscreen Web - E-Paper Display Manager

import os
import json
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)
from flask_httpauth import HTTPBasicAuth
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from managers import (
    FolderManager,
    PlaylistManager,
    PushJob,
    SlideshowManager,
)
from state import AppState

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'change-this-secret-key')
auth = HTTPBasicAuth()

# Configuration
BASE_FOLDER = os.getenv('BASE_FOLDER', './playlists')
THUMBNAILS_FOLDER = os.getenv('THUMBNAILS_FOLDER', './thumbnails')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
MAX_IMAGE_SIZE_MB = 5
MAX_STORAGE_MB = 8000

# Load credentials from environment
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')
users = {ADMIN_USERNAME: generate_password_hash(ADMIN_PASSWORD)}

# Application State
app_state = AppState()

# Slideshow scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Managers
playlist_manager = PlaylistManager(BASE_FOLDER)
folder_manager = FolderManager(BASE_FOLDER, THUMBNAILS_FOLDER)
slideshow_manager = SlideshowManager(scheduler, BASE_FOLDER, app_state)


def async_push_with_feedback(job_id, image_path, app_state):
    """Push image asynchronously with real-time feedback"""
    job = app_state.push_jobs.get(job_id)
    if not job:
        return
    
    try:
        job.update('dithering', 10, 'Loading and dithering image...')
        
        process = subprocess.Popen([
            os.getenv('PUSH_SCRIPT', './push_epaper_sierra_sorbet_fast.py'),
            image_path,
            '--host', os.getenv('ESP32_HOST', '192.168.1.100')
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            
            if output:
                output = output.strip()
                if '[TIME] Load & resize:' in output:
                    job.update('dithering', 30, 'Image processed, applying dithering...')
                elif '[TIME] Dithering:' in output:
                    job.update('sending', 60, 'Dithering complete, sending to display...')
                elif '[TIME] Packing:' in output:
                    job.update('sending', 80, 'Packaging data for transmission...')
                elif '[TIME] Network send:' in output:
                    job.update('sending', 90, 'Transmitting to Ink Screen...')
                elif 'OK sent.' in output:
                    job.update('completed', 100, 'Successfully sent to display!')
                    break
        
        process.wait()
        
        if process.returncode == 0:
            job.update('completed', 100, 'Successfully sent to display!')
        else:
            error_output = process.stderr.read()
            job.update('failed', 0, f'Push failed: {error_output}')
            job.error = error_output
            
    except Exception as e:
        job.update('failed', 0, f'Error: {str(e)}')
        job.error = str(e)
    
    def cleanup():
        time.sleep(30)
        if job_id in app_state.push_jobs:
            del app_state.push_jobs[job_id]
    
    threading.Thread(target=cleanup, daemon=True).start()


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        session['username'] = username
        return username

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def detect_optimal_format(accept_header):
    if not accept_header:
        return 'jpeg'
    
    try:
        from PIL import Image
        available_formats = Image.registered_extensions().values()
    except:
        return 'jpeg'
    
    if 'image/avif' in accept_header and 'AVIF' in available_formats:
        return 'avif'
    elif 'image/webp' in accept_header and 'WEBP' in available_formats:
        return 'webp'
    else:
        return 'jpeg'

def create_optimized_thumbnail(image_path, thumb_path, format='jpeg', quality=85, size=(150, 150)):
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode == 'P':
            img = img.convert('RGB')
        
        img.thumbnail(size, Image.Resampling.LANCZOS)
        
        if format == 'avif':
            img.save(thumb_path, 'AVIF', quality=quality)
        elif format == 'webp':
            img.save(thumb_path, 'WebP', quality=quality, method=6)
        else:
            img.save(thumb_path, 'JPEG', quality=quality, optimize=True, progressive=True)
        
        return True
    except Exception as e:
        print(f"Error creating optimized thumbnail: {e}")
        if format != 'jpeg':
            return create_optimized_thumbnail(image_path, thumb_path, 'jpeg', quality, size)
        return False

def create_thumbnail(image_path, thumb_path):
    return create_optimized_thumbnail(image_path, thumb_path, 'jpeg', 85)

def resize_large_image(image_path, max_size_mb=MAX_IMAGE_SIZE_MB):
    file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return False
    
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        
        reduction_factor = (max_size_mb / file_size_mb) ** 0.5
        new_size = (int(img.width * reduction_factor), int(img.height * reduction_factor))
        
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        img.save(image_path, quality=85, optimize=True)
        return True
    except Exception as e:
        print(f"Error resizing image: {e}")
        return False

def check_storage_and_cleanup():
    total_size = 0
    image_files = []
    
    for root, dirs, files in os.walk(BASE_FOLDER):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                file_path = os.path.join(root, file)
                file_size = os.path.getsize(file_path)
                file_mtime = os.path.getmtime(file_path)
                total_size += file_size
                image_files.append((file_path, file_size, file_mtime))
    
    total_size_mb = total_size / (1024 * 1024)
    
    if total_size_mb > MAX_STORAGE_MB * 0.9:
        image_files.sort(key=lambda x: x[2])
        
        target_size = MAX_STORAGE_MB * 0.8 * 1024 * 1024
        for file_path, file_size, _ in image_files:
            if total_size <= target_size:
                break
            
            try:
                os.remove(file_path)
                thumb_name = f"{os.path.splitext(os.path.basename(file_path))[0]}_thumb.jpg"
                thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
                
                total_size -= file_size
                print(f"Removed old file: {file_path}")
            except Exception as e:
                print(f"Error removing file: {e}")

@app.route('/')
@auth.login_required
def index():
    return render_template('index.html')

@app.route('/api/folders')
@auth.login_required
def api_get_folders():
    tree = folder_manager.get_folder_tree()
    return jsonify({'tree': tree})

@app.route('/api/folder', methods=['POST'])
@auth.login_required
def api_create_folder():
    data = request.json
    path = data.get('path', '').strip('/')
    
    if folder_manager.create_folder(path):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to create folder'}), 400

@app.route('/api/set_current', methods=['POST'])
@auth.login_required
def api_set_current_image():
    data = request.json
    image_path = data.get('image_path')
    
    if not image_path:
        return jsonify({'error': 'No image path provided'}), 400
    
    full_path = os.path.join(BASE_FOLDER, image_path)
    if not os.path.exists(full_path):
        return jsonify({'error': 'Image not found'}), 404
    
    app_state.current_folder = os.path.dirname(image_path)
    app_state.current_image = os.path.basename(image_path)
    app_state.manual_override = True
    
    return jsonify({
        'success': True,
        'current_folder': app_state.current_folder,
        'current_image': app_state.current_image,
        'message': f'Current image set to {app_state.current_image}'
    })

@app.route('/api/playlist/')
@app.route('/api/playlist/<path:folder_path>')
@auth.login_required
def api_get_playlist(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    if not os.path.exists(full_path):
        os.makedirs(full_path, exist_ok=True)
    
    playlist = playlist_manager.load_playlist(full_path)
    
    images = []
    for f in os.listdir(full_path):
        if f.lower().endswith(('.jpg', '.jpeg', '.png')):
            file_path = os.path.join(full_path, f)
            images.append({
                'name': f,
                'size': os.path.getsize(file_path),
                'modified': datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
            })
    
    return jsonify({'playlist': playlist, 'images': images})

@app.route('/api/playlist//order', methods=['POST'])
@app.route('/api/playlist/<path:folder_path>/order', methods=['POST'])
@auth.login_required
def api_update_order(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    data = request.json
    new_order = data.get('order', [])
    
    playlist = playlist_manager.update_order(full_path, new_order)
    return jsonify({'success': True, 'playlist': playlist})

@app.route('/api/playlist//settings', methods=['POST'])
@app.route('/api/playlist/<path:folder_path>/settings', methods=['POST'])
@auth.login_required
def api_update_settings(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    data = request.json
    playlist = playlist_manager.load_playlist(full_path)
    
    if 'settings' in data:
        playlist['settings'].update(data['settings'])
    if 'description' in data:
        playlist['description'] = data['description']
    
    playlist_manager.save_playlist(full_path, playlist)
    return jsonify({'success': True})

@app.route('/api/upload/', methods=['POST'])
@app.route('/api/upload/<path:folder_path>', methods=['POST'])
@auth.login_required
def api_upload(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    os.makedirs(full_path, exist_ok=True)
    
    check_storage_and_cleanup()
    
    files = request.files.getlist('files')
    uploaded = []
    
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{int(time.time())}{ext}"
            
            file_path = os.path.join(full_path, filename)
            file.save(file_path)
            
            try:
                img = Image.open(file_path)
                img = ImageOps.exif_transpose(img)
                img.save(file_path)
            except:
                pass
            
            resize_large_image(file_path)
            
            thumb_name = f"{folder_path.replace('/', '_')}_{os.path.splitext(filename)[0]}_thumb.jpg"
            thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
            create_thumbnail(file_path, thumb_path)
            
            uploaded.append(filename)
    
    playlist_manager.update_order(full_path)
    
    return jsonify({'success': True, 'uploaded': uploaded})

thumbnail_call_count = 0

@app.route('/api/thumbnail/<path:image_path>')
def api_get_thumbnail(image_path):
    global thumbnail_call_count
    thumbnail_call_count += 1
    
    accept_header = request.headers.get('Accept', '')
    quality = int(request.args.get('q', '85'))
    width = int(request.args.get('w', '300'))
    
    optimal_format = detect_optimal_format(accept_header)
    
    folder_parts = os.path.dirname(image_path).replace('/', '_')
    filename = os.path.basename(image_path)
    base_name = os.path.splitext(filename)[0]
    
    format_ext = 'jpg' if optimal_format == 'jpeg' else optimal_format
    thumb_name = f"{folder_parts}_{base_name}_w{width}_q{quality}.{format_ext}" if folder_parts else f"{base_name}_w{width}_q{quality}.{format_ext}"
    thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
    full_image_path = os.path.join(BASE_FOLDER, image_path)
    
    if not os.path.exists(full_image_path):
        return '', 404
    
    need_regenerate = False
    
    if not os.path.exists(thumb_path):
        need_regenerate = True
    else:
        image_mtime = os.path.getmtime(full_image_path)
        thumb_mtime = os.path.getmtime(thumb_path)
        if image_mtime > thumb_mtime:
            need_regenerate = True
    
    if need_regenerate:
        thumbnail_size = (width, width)
        success = create_optimized_thumbnail(
            full_image_path, 
            thumb_path, 
            format=optimal_format,
            quality=quality, 
            size=thumbnail_size
        )
        if not success:
            return '', 500
    
    if os.path.exists(thumb_path):
        response = send_file(thumb_path)
        
        response.headers.update({
            'Cache-Control': 'public, max-age=31536000, immutable',
            'Vary': 'Accept',
            'X-Content-Type-Options': 'nosniff',
            'Content-Type': f'image/{optimal_format}' if optimal_format != 'jpeg' else 'image/jpeg'
        })
        
        return response
    
    return '', 404

@app.route('/api/image/<path:image_path>', methods=['DELETE'])
@auth.login_required
def api_delete_image(image_path):
    full_path = os.path.join(BASE_FOLDER, image_path)
    
    if os.path.exists(full_path):
        os.remove(full_path)
        
        folder_parts = os.path.dirname(image_path).replace('/', '_')
        filename = os.path.basename(image_path)
        thumb_name = f"{folder_parts}_{os.path.splitext(filename)[0]}_thumb.jpg" if folder_parts else f"{os.path.splitext(filename)[0]}_thumb.jpg"
        thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        
        folder_path = os.path.dirname(full_path)
        playlist_manager.update_order(folder_path)
        
        return jsonify({'success': True})
    
    return jsonify({'error': 'Image not found'}), 404

@app.route('/api/folder/<path:folder_path>', methods=['DELETE'])
@auth.login_required
def api_delete_folder(folder_path):
    full_folder_path = os.path.join(BASE_FOLDER, folder_path)
    
    if not os.path.exists(full_folder_path):
        return jsonify({'error': 'Folder not found'}), 404
    
    if not os.path.isdir(full_folder_path):
        return jsonify({'error': 'Path is not a folder'}), 400
    
    if full_folder_path == BASE_FOLDER:
        return jsonify({'error': 'Cannot delete root folder'}), 400
    
    try:
        shutil.rmtree(full_folder_path)
        return jsonify({'success': True, 'message': f'Folder "{folder_path}" deleted successfully'})
    
    except Exception as e:
        return jsonify({'error': f'Failed to delete folder: {str(e)}'}), 500

@app.route('/api/folder/<path:folder_path>/rename', methods=['POST'])
@auth.login_required
def api_rename_folder(folder_path):
    data = request.json
    new_name = data.get('new_name', '').strip()
    
    if not new_name:
        return jsonify({'error': 'New name cannot be empty'}), 400
    
    if any(char in new_name for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        return jsonify({'error': 'Invalid characters in folder name'}), 400
    
    full_folder_path = os.path.join(BASE_FOLDER, folder_path)
    
    if not os.path.exists(full_folder_path):
        return jsonify({'error': 'Folder not found'}), 404
    
    if not os.path.isdir(full_folder_path):
        return jsonify({'error': 'Path is not a folder'}), 400
    
    parent_dir = os.path.dirname(full_folder_path)
    new_full_path = os.path.join(parent_dir, new_name)
    
    if os.path.exists(new_full_path):
        return jsonify({'error': 'A folder with that name already exists'}), 400
    
    try:
        os.rename(full_folder_path, new_full_path)
        return jsonify({'success': True, 'message': f'Folder renamed to "{new_name}"'}) # Corrected escape sequence
    
    except Exception as e:
        return jsonify({'error': f'Failed to rename folder: {str(e)}'}), 500

@app.route('/api/folder/move', methods=['POST'])
@auth.login_required
def api_move_folder():
    data = request.get_json()
    source_path = data.get('source')
    target_path = data.get('target')
    
    if not source_path or not target_path:
        return jsonify({'error': 'Missing source or target path'}), 400
    
    source_full = os.path.join(BASE_FOLDER, source_path)
    target_full = os.path.join(BASE_FOLDER, target_path)
    
    if not os.path.exists(source_full):
        return jsonify({'error': 'Source folder not found'}), 404
    
    if not os.path.isdir(source_full):
        return jsonify({'error': 'Source is not a folder'}), 400
    
    if os.path.exists(target_full):
        return jsonify({'error': 'Target folder already exists'}), 409
    
    target_parent = os.path.dirname(target_full)
    os.makedirs(target_parent, exist_ok=True)
    
    try:
        shutil.move(source_full, target_full)
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/move', methods=['POST'])
@auth.login_required
def api_move_image():
    data = request.json
    image = data.get('image')
    from_folder = data.get('from', '')
    to_folder = data.get('to', '')
    
    if folder_manager.move_image(image, from_folder, to_folder):
        return jsonify({'success': True})
    
    return jsonify({'error': 'Failed to move image'}), 400

@app.route('/api/push/<path:image_path>', methods=['POST'])
@auth.login_required
def api_push_image(image_path):
    full_path = os.path.join(BASE_FOLDER, image_path)
    
    if not os.path.exists(full_path):
        return jsonify({'error': 'Image not found'}), 404
    
    job_id = str(uuid.uuid4())
    image_name = os.path.basename(image_path)
    job = PushJob(job_id, image_name, full_path)
    app_state.push_jobs[job_id] = job
    
    thread = threading.Thread(target=async_push_with_feedback, args=(job_id, full_path, app_state))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True, 
        'job_id': job_id,
        'status': 'started',
        'message': 'Push started, use job_id to track progress'
    })

@app.route('/api/push/status/<job_id>')
@auth.login_required
def api_push_status(job_id):
    job = app_state.push_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(job.to_dict())

@app.route('/api/image/info')
def api_image_info():
    if request.args.get("battery"):
        app_state.esp32_stats["battery"] = int(request.args.get("battery", -1))
        app_state.esp32_stats["rssi"] = int(request.args.get("rssi", 0))
        app_state.esp32_stats["heap"] = int(request.args.get("heap", 0))
        app_state.esp32_stats["uptime"] = int(request.args.get("uptime", 0))
        app_state.esp32_stats["last_seen"] = datetime.now().strftime("%H:%M:%S")
        print(f"[ESP32] Battery: {app_state.esp32_stats['battery']}% | RSSI: {app_state.esp32_stats['rssi']}dBm | Heap: {app_state.esp32_stats['heap']}B | Uptime: {app_state.esp32_stats['uptime']}s")
    
    try:
        if app_state.manual_override and app_state.current_image:
            pass
        elif slideshow_manager.app_state.slideshow_state.get('job_id') and slideshow_manager.app_state.slideshow_state.get('current_image_name'):
            folder_full_path = slideshow_manager.app_state.slideshow_state.get('folder_path', '')
            if folder_full_path.startswith(BASE_FOLDER):
                app_state.current_folder = os.path.relpath(folder_full_path, BASE_FOLDER)
            else:
                app_state.current_folder = folder_full_path
            app_state.current_image = slideshow_manager.app_state.slideshow_state.get('current_image_name')
        elif not app_state.current_image:
            for root, dirs, files in os.walk(BASE_FOLDER):
                for file in files:
                    if file.lower().endswith(('.jpg', '.png', '.jpeg')):
                        rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                        if '/' in rel_path:
                            app_state.current_folder, app_state.current_image = rel_path.split('/', 1)
                        else:
                            app_state.current_image = rel_path
                        break
                if app_state.current_image:
                    break
            if not app_state.current_image:
                return jsonify({'error': 'No images available'}), 404
            
        if app_state.current_folder:
            full_path = os.path.join(BASE_FOLDER, app_state.current_folder, app_state.current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, app_state.current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        import hashlib
        hash_md5 = hashlib.md5()
        with open(full_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        
        file_hash = hash_md5.hexdigest()[:12]
        
        return jsonify({
            'hash': file_hash,
            'image_name': os.path.basename(app_state.current_image),
            'timestamp': int(os.path.getmtime(full_path))
        })
        
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/image')
def api_get_current_image():
    try:
        slideshow_data = slideshow_manager.get_status()
        current_folder = slideshow_data.get('current_folder')
        current_image = slideshow_data.get('current_image')
        
        if not current_image:
            return jsonify({'error': 'No current image'}), 404
            
        if current_folder:
            full_path = os.path.join(BASE_FOLDER, current_folder, current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        epaper_data = convert_image_to_epaper_format(full_path)
        
        if not epaper_data:
            return jsonify({'error': 'Failed to convert image'}), 500
            
        return Response(
            epaper_data,
            mimetype='application/octet-stream',
            headers={
                'Content-Length': str(len(epaper_data)),
                'Cache-Control': 'no-cache'
            }
        )
        
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

def convert_image_to_epaper_format(image_path):
    try:
        from dither_sierra_sorbet import sierra_sorbet_dither
        import numpy as np
        
        EPD_W, EPD_H = 1200, 1600
        PALETTE_RGB = [(0,0,0), (255,255,255), (255,255,0), (255,0,0), (0,0,255), (0,255,0)]
        CODE_MAP = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]
        
        im = Image.open(image_path).convert("RGB")
        im = crop_center_zoom(im)
        im = im.resize((EPD_W, EPD_H), Image.Resampling.LANCZOS)
        im = enhance_image(im)
        
        img_array = np.array(im, dtype=np.float32)
        palette_np = np.array(PALETTE_RGB, dtype=np.float32)
        indices_2d = sierra_sorbet_dither(img_array, palette_np)
        indices = indices_2d.flatten()
        
        left_data = pack_half(indices, 0, 600)
        right_data = pack_half(indices, 600, 1200)
        
        return left_data + right_data
        
    except Exception as e:
        print(f"Error converting image with Sierra SORBET: {e}")
        import traceback
        traceback.print_exc()
        return None

def crop_center_zoom(im, target_ratio=12/16):
    original_width, original_height = im.size
    original_ratio = original_width / original_height
    
    if original_ratio > target_ratio:
        new_width = int(original_height * target_ratio)
        left = (original_width - new_width) // 2
        right = left + new_width
        crop_box = (left, 0, right, original_height)
    else:
        new_height = int(original_width / target_ratio)
        top = (original_height - new_height) // 2
        bottom = top + new_height
        crop_box = (0, top, original_width, bottom)
    
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

def pack_half(indices, x0, x1):
    EPD_W, EPD_H = 1200, 1600
    CODE_MAP = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]
    
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

@app.route('/api/image/stream')
def api_image_stream():
    try:
        if not app_state.current_image:
            for root, dirs, files in os.walk(BASE_FOLDER):
                for file in files:
                    if file.lower().endswith(('.jpg', '.png', '.jpeg')):
                        rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                        if '/' in rel_path:
                            app_state.current_folder, app_state.current_image = rel_path.split('/', 1)
                        else:
                            app_state.current_image = rel_path
                        break
                if app_state.current_image:
                    break
            if not app_state.current_image:
                return jsonify({'error': 'No images available'}), 404
            
        if app_state.current_folder:
            full_path = os.path.join(BASE_FOLDER, app_state.current_folder, app_state.current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, app_state.current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        print(f"[HTTP] Streaming {app_state.current_image}")
        
        binary_data = convert_image_to_epaper_format(full_path)
        if not binary_data:
            return jsonify({'error': 'Failed to convert image'}), 500
            
        def generate_stream():
            BYTES_PER_LINE_HALF = 300
            for i in range(0, len(binary_data), BYTES_PER_LINE_HALF):
                chunk = binary_data[i:i + BYTES_PER_LINE_HALF]
                if len(chunk) == BYTES_PER_LINE_HALF:
                    yield chunk
                else:
                    yield chunk + b'\x00' * (BYTES_PER_LINE_HALF - len(chunk))
        
        return Response(generate_stream(), 
                       mimetype='application/octet-stream',
                       headers={'Content-Length': str(len(binary_data))})
                       
    except Exception as e:
        print(f"Error in image streaming: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/slideshow/start/', methods=['POST'])
@app.route('/api/slideshow/start/<path:folder_path>', methods=['POST'])
@auth.login_required
def api_start_slideshow(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    success = slideshow_manager.start(full_path)
    
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'No images found in folder'}), 400

@app.route('/api/slideshow/stop', methods=['POST'])
@auth.login_required
def api_stop_slideshow():
    slideshow_manager.stop()
    return jsonify({'success': True})

@app.route('/api/slideshow/next', methods=['POST'])
@auth.login_required
def api_slideshow_next():
    status = slideshow_manager.get_status()
    if status.get('running'):
        try:
            slideshow_manager.push_next_image()
            return jsonify({'success': True, 'message': 'Advanced to next image'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Slideshow not running'}), 400

@app.route('/api/thumbnails/refresh', methods=['POST'], defaults={'folder_path': ''})
@app.route('/api/thumbnails/refresh/<path:folder_path>', methods=['POST'])
@auth.login_required
def api_refresh_thumbnails(folder_path=''):
    full_folder = os.path.join(BASE_FOLDER, folder_path) if folder_path else BASE_FOLDER
    
    if not os.path.exists(full_folder):
        return jsonify({'error': 'Folder not found'}), 404
    
    regenerated = 0
    errors = 0
    
    for root, dirs, files in os.walk(full_folder):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                image_path = os.path.join(root, file)
                rel_path = os.path.relpath(image_path, BASE_FOLDER)
                
                folder_parts = os.path.dirname(rel_path).replace('/', '_')
                filename = os.path.splitext(file)[0]
                thumb_name = f"{folder_parts}_{filename}_thumb.jpg" if folder_parts else f"{filename}_thumb.jpg"
                thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
                
                try:
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                    
                    if create_thumbnail(image_path, thumb_path):
                        regenerated += 1
                    else:
                        errors += 1
                except Exception as e:
                    print(f"Error regenerating thumbnail for {file}: {e}")
                    errors += 1
    
    return jsonify({
        'success': True,
        'regenerated': regenerated,
        'errors': errors,
        'message': f'Regenerated {regenerated} thumbnails'
    })

@app.route('/api/slideshow/status')
@auth.login_required
def api_slideshow_status():
    return jsonify(slideshow_manager.get_status())

@app.route('/api/esp32/stats')
@auth.login_required
def api_esp32_stats():
    return jsonify(app_state.esp32_stats)

@auth.login_required
def api_scheduler_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run': str(job.next_run_time) if job.next_run_time else None,
            'trigger': str(job.trigger)
        })
    return jsonify({'jobs': jobs})

@app.route('/api/thumbnail/stats')
def api_thumbnail_stats():
    global thumbnail_call_count
    return jsonify({
        'total_calls': thumbnail_call_count,
        'message': f'Thumbnail API called {thumbnail_call_count} times since server start'
    })

def cleanup_orphaned_thumbnails():
    try:
        if not os.path.exists(THUMBNAILS_FOLDER):
            return
        
        cleaned_count = 0
        
        existing_images = set()
        for root, dirs, files in os.walk(BASE_FOLDER):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                    rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                    folder_parts = os.path.dirname(rel_path).replace('/', '_')
                    filename = os.path.splitext(file)[0]
                    thumb_name = f"{folder_parts}_{filename}_thumb.jpg" if folder_parts else f"{filename}_thumb.jpg"
                    existing_images.add(thumb_name)
        
        for thumb_file in os.listdir(THUMBNAILS_FOLDER):
            if thumb_file.endswith('_thumb.jpg'):
                if thumb_file not in existing_images:
                    thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_file)
                    try:
                        os.remove(thumb_path)
                        cleaned_count += 1
                        print(f"Removed orphaned thumbnail: {thumb_file}")
                    except Exception as e:
                        print(f"Error removing thumbnail {thumb_file}: {e}")
        
        if cleaned_count > 0:
            print(f"Cleaned {cleaned_count} orphaned thumbnails")
    
    except Exception as e:
        print(f"Error during thumbnail cleanup: {e}")

scheduler.add_job(
    func=cleanup_orphaned_thumbnails,
    trigger="interval",
    hours=1,
    id='thumbnail_cleanup',
    name='Cleanup orphaned thumbnails',
    replace_existing=True
)

if __name__ == '__main__':
    folder_manager.ensure_base_folder()
    cleanup_orphaned_thumbnails()
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)