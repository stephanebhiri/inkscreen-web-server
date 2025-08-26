#!/usr/bin/env python3
# Inkscreen Web - E-Paper Display Manager

import os

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # Use system environment variables if dotenv not available
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from PIL import Image, ImageOps
from flask import Flask, request, jsonify, render_template, send_file, session, Response
from flask_httpauth import HTTPBasicAuth
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
import uuid

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'change-this-secret-key')
auth = HTTPBasicAuth()

# Configuration
BASE_FOLDER = os.getenv('BASE_FOLDER', './playlists')
THUMBNAILS_FOLDER = os.getenv('THUMBNAILS_FOLDER', './thumbnails')
CONFIG_FILE = os.getenv('CONFIG_FILE', './config.json')
SLIDESHOW_STATUS_FILE = os.getenv('SLIDESHOW_STATUS_FILE', './slideshow_status.json')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
THUMBNAIL_SIZE = (150, 150)
MAX_IMAGE_SIZE_MB = 5
MAX_STORAGE_MB = 8000

# Load credentials from environment
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')
users = {ADMIN_USERNAME: generate_password_hash(ADMIN_PASSWORD)}

# Global push status tracking
push_jobs = {}

# HTTP Polling globals
IMAGE_PATH = BASE_FOLDER
current_folder = ""
current_image = ""
manual_override = False  # When True, use manual selection instead of playlist
# ESP32 Device Stats
esp32_stats = {
    "battery": -1,
    "rssi": 0,
    "heap": 0,
    "uptime": 0,
    "last_seen": None
}

class PushJob:
    def __init__(self, job_id, image_name, image_path):
        self.job_id = job_id
        self.image_name = image_name
        self.image_path = image_path
        self.status = 'starting'  # starting, dithering, sending, completed, failed
        self.progress = 0
        self.message = 'Initializing...'
        self.start_time = time.time()
        self.error = None
        
    def update(self, status, progress=None, message=None):
        self.status = status
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
            
    def to_dict(self):
        return {
            'job_id': self.job_id,
            'image_name': self.image_name,
            'status': self.status,
            'progress': self.progress,
            'message': self.message,
            'elapsed': time.time() - self.start_time,
            'error': self.error
        }

# Slideshow scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Slideshow state (managed by scheduler)
slideshow_state = {
    'job_id': None,
    'folder_path': '',
    'current_image_name': None,  # Track by name instead of index
    'loop_count': 0,
    'images': [],
    'settings': {}
}

def async_push_with_feedback(job_id, image_path):
    """Push image asynchronously with real-time feedback"""
    job = push_jobs.get(job_id)
    if not job:
        return
    
    try:
        # Phase 1: Starting
        job.update('dithering', 10, 'Loading and dithering image...')
        
        # Execute push script with real-time monitoring
        process = subprocess.Popen([
            os.getenv('PUSH_SCRIPT', './push_epaper_sierra_sorbet_fast.py'),
            image_path,
            '--host', os.getenv('ESP32_HOST', '192.168.1.100')
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        
        # Monitor output for progress
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
        
        # Wait for process to complete
        process.wait()
        
        # Check final result
        if process.returncode == 0:
            job.update('completed', 100, 'Successfully sent to display!')
        else:
            error_output = process.stderr.read()
            job.update('failed', 0, f'Push failed: {error_output}')
            job.error = error_output
            
    except Exception as e:
        job.update('failed', 0, f'Error: {str(e)}')
        job.error = str(e)
    
    # Clean up job after 30 seconds
    def cleanup():
        time.sleep(30)
        if job_id in push_jobs:
            del push_jobs[job_id]
    
    threading.Thread(target=cleanup, daemon=True).start()

class PlaylistManager:
    @staticmethod
    def get_playlist_file(folder_path):
        return os.path.join(folder_path, '.playlist.json')
    
    @staticmethod
    def load_playlist(folder_path):
        playlist_file = PlaylistManager.get_playlist_file(folder_path)
        if os.path.exists(playlist_file):
            with open(playlist_file, 'r') as f:
                return json.load(f)
        return {
            'name': os.path.basename(folder_path),
            'created': datetime.now().isoformat(),
            'modified': datetime.now().isoformat(),
            'order': [],
            'settings': {
                'interval': 300,
                'shuffle': False,
                'loop': True,
                'active': False
            },
            'tags': [],
            'description': '',
            'stats': {
                'play_count': 0,
                'last_played': None,
                'total_duration': 0
            }
        }
    
    @staticmethod
    def save_playlist(folder_path, playlist_data):
        playlist_data['modified'] = datetime.now().isoformat()
        playlist_file = PlaylistManager.get_playlist_file(folder_path)
        with open(playlist_file, 'w') as f:
            json.dump(playlist_data, f, indent=2)
    
    @staticmethod
    def update_order(folder_path, new_order=None):
        """Update playlist order based on folder contents"""
        playlist = PlaylistManager.load_playlist(folder_path)
        
        # Get current images in folder
        current_images = []
        for f in os.listdir(folder_path):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                current_images.append(f)
        
        if new_order:
            # Use provided order, but only for existing images
            playlist['order'] = [img for img in new_order if img in current_images]
            # Add any new images not in order to the end
            for img in current_images:
                if img not in playlist['order']:
                    playlist['order'].append(img)
        else:
            # Auto-update: keep existing order, add new images, remove deleted
            existing_order = playlist.get('order', [])
            new_order = []
            
            # Keep existing images in their order
            for img in existing_order:
                if img in current_images:
                    new_order.append(img)
            
            # Add new images to the end
            for img in current_images:
                if img not in new_order:
                    new_order.append(img)
            
            playlist['order'] = new_order
        
        PlaylistManager.save_playlist(folder_path, playlist)
        return playlist

class FolderManager:
    @staticmethod
    def ensure_base_folder():
        os.makedirs(BASE_FOLDER, exist_ok=True)
        os.makedirs(THUMBNAILS_FOLDER, exist_ok=True)
    
    @staticmethod
    def create_folder(path):
        """Create a new folder"""
        full_path = os.path.join(BASE_FOLDER, path.strip('/'))
        os.makedirs(full_path, exist_ok=True)
        # Initialize playlist for new folder
        PlaylistManager.update_order(full_path)
        return True
    
    @staticmethod
    def get_folder_tree():
        """Get complete folder tree structure"""
        FolderManager.ensure_base_folder()
        
        # Add root folder first
        root_images = [f for f in os.listdir(BASE_FOLDER) 
                      if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        root_playlist = PlaylistManager.load_playlist(BASE_FOLDER)
        
        tree = [{
            'name': '📁 Root',
            'path': '',
            'type': 'folder',
            'children': [],
            'image_count': len(root_images),
            'active': root_playlist.get('settings', {}).get('active', False)
        }]
        
        def walk_dir(path, rel_path=''):
            items = []
            try:
                for item in sorted(os.listdir(path)):
                    if item.startswith('.'):
                        continue
                    
                    item_path = os.path.join(path, item)
                    item_rel_path = os.path.join(rel_path, item)
                    
                    if os.path.isdir(item_path):
                        children = walk_dir(item_path, item_rel_path)
                        playlist = PlaylistManager.load_playlist(item_path)
                        items.append({
                            'name': item,
                            'path': item_rel_path,
                            'type': 'folder',
                            'children': children,
                            'image_count': len([f for f in os.listdir(item_path) 
                                              if f.lower().endswith(('.jpg', '.jpeg', '.png'))]),
                            'active': playlist.get('settings', {}).get('active', False)
                        })
            except PermissionError:
                pass
            
            return items
        
        # Add subfolders to root
        tree[0]['children'] = walk_dir(BASE_FOLDER)
        return tree
    
    @staticmethod
    def move_image(image_path, from_folder, to_folder):
        """Move image from one folder to another"""
        from_path = os.path.join(BASE_FOLDER, from_folder.strip('/'), image_path)
        to_path = os.path.join(BASE_FOLDER, to_folder.strip('/'), image_path)
        
        if os.path.exists(from_path):
            os.makedirs(os.path.dirname(to_path), exist_ok=True)
            shutil.move(from_path, to_path)
            
            # Update playlists
            PlaylistManager.update_order(os.path.dirname(from_path))
            PlaylistManager.update_order(os.path.dirname(to_path))
            
            # Move thumbnail
            thumb_from = os.path.join(THUMBNAILS_FOLDER, f"{os.path.splitext(image_path)[0]}_thumb.jpg")
            thumb_to = os.path.join(THUMBNAILS_FOLDER, f"{to_folder}_{os.path.splitext(image_path)[0]}_thumb.jpg")
            if os.path.exists(thumb_from):
                shutil.move(thumb_from, thumb_to)
            
            return True
        return False

class SlideshowManager:
    @staticmethod
    def get_status():
        """Get current slideshow status"""
        global slideshow_state
        
        # Check if slideshow is actually running
        job_id = slideshow_state.get('job_id')
        job = scheduler.get_job(job_id) if job_id else None
        
        if job and job_id:
            # Use image name tracking for reliable status
            images = slideshow_state['images']
            current_image_name = slideshow_state['current_image_name']
            settings = slideshow_state['settings']
            
            # Current image is the one identified by name
            current_image = current_image_name or ''
            
            # Find next image
            if current_image_name and current_image_name in images and images:
                current_index = images.index(current_image_name)
                next_index = current_index + 1
                
                # Handle wraparound
                if next_index >= len(images):
                    if settings.get('loop', True):
                        next_image = images[0]
                        displayed_index = current_index
                    else:
                        next_image = ''
                        displayed_index = current_index
                else:
                    next_image = images[next_index]
                    displayed_index = current_index
            else:
                # No current image or not found - show first image as next
                next_image = images[0] if images else ''
                displayed_index = 0
            
            return {
                'running': True,
                'current_folder': slideshow_state['folder_path'].replace(BASE_FOLDER, '').strip('/'),
                'current_image': current_image,
                'next_image': next_image,
                'current_index': displayed_index + 1,
                'total_images': len(images),
                'loop_count': slideshow_state['loop_count'],
                'loop_enabled': settings.get('loop', True),
                'shuffle_enabled': settings.get('shuffle', False),
                'interval': settings.get('interval', 300),
                'start_time': datetime.now().isoformat(),
                'next_change': job.next_run_time.timestamp() if job.next_run_time else None
            }
        
        # Not running
        return {
            'running': False,
            'current_folder': '',
            'current_image': '',
            'next_image': '',
            'current_index': 0,
            'total_images': 0,
            'loop_count': 0,
            'start_time': None,
            'next_change': None
        }
    
    @staticmethod
    def push_next_image():
        """Push next image in slideshow (called by scheduler)"""
        global slideshow_state
        
        try:
            images = slideshow_state['images']
            current_image_name = slideshow_state['current_image_name']
            settings = slideshow_state['settings']
            folder_path = slideshow_state['folder_path']
            
            # Check if we have images
            if not images:
                SlideshowManager.stop()
                return
            
            # Find current image index (or start from beginning)
            if current_image_name and current_image_name in images:
                current_index = images.index(current_image_name)
                # Get next image
                next_index = current_index + 1
            else:
                # First image or image not found
                next_index = 0
            
            # Handle end of list
            if next_index >= len(images):
                if settings.get('loop', True):
                    next_index = 0
                    slideshow_state['loop_count'] += 1
                    
                    # Reshuffle if needed (but keep current_image_name stable)
                    if settings.get('shuffle', False):
                        import random
                        random.shuffle(slideshow_state['images'])
                        images = slideshow_state['images']
                        next_index = 0  # Start from new shuffled beginning
                else:
                    # End of slideshow
                    SlideshowManager.stop()
                    return
            
            # Get the image to push
            image_file = images[next_index]
            image_path = os.path.join(folder_path, image_file)
            
            # Push image to display
            subprocess.run([
                os.getenv('PUSH_SCRIPT', './push_epaper_sierra_sorbet_fast.py'),
                image_path,
                '--host', os.getenv('ESP32_HOST', '192.168.1.100')
            ], check=False, timeout=30)
            
            # Update current image name (this is now our reference)
            slideshow_state['current_image_name'] = image_file
            
            # Clear manual override when playlist advances automatically
            global manual_override
            manual_override = False
            
        except Exception as e:
            print(f"Error in push_next_image: {e}")
            # Don't stop slideshow on error, just continue
    
    @staticmethod
    def start(folder_path):
        """Start slideshow for a folder"""
        global slideshow_state
        
        # Stop existing slideshow
        SlideshowManager.stop()
        
        # Load playlist
        playlist = PlaylistManager.load_playlist(folder_path)
        settings = playlist.get('settings', {})
        interval = settings.get('interval', 300)
        
        # Get images
        images = playlist.get('order', [])
        if not images:
            images = [f for f in os.listdir(folder_path) 
                     if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        
        if not images:
            return False
        
        # Shuffle if needed
        if settings.get('shuffle', False):
            import random
            images = images.copy()
            random.shuffle(images)
        
        # Update state
        slideshow_state = {
            'job_id': None,
            'folder_path': folder_path,
            'current_image_name': None,  # Will be set by first push
            'loop_count': 0,
            'images': images,
            'settings': settings
        }
        
        # Schedule first image immediately, then recurring
        try:
            SlideshowManager.push_next_image()  # Push first image now
            print(f"[DEBUG] First image pushed successfully")
        except Exception as e:
            print(f"[DEBUG] Error pushing first image: {e}")
        
        # Schedule recurring job
        try:
            job = scheduler.add_job(
                SlideshowManager.push_next_image,
                'interval',
                seconds=interval,
                id='slideshow_job',
                replace_existing=True,
                max_instances=1
            )
            slideshow_state['job_id'] = job.id
            print(f"[DEBUG] Job created successfully: {job.id}, next run: {job.next_run_time}")
        except Exception as e:
            print(f"[DEBUG] Error creating job: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        # Update playlist stats
        playlist['stats']['play_count'] = playlist['stats'].get('play_count', 0) + 1
        playlist['stats']['last_played'] = datetime.now().isoformat()
        PlaylistManager.save_playlist(folder_path, playlist)
        
        return True
    
    @staticmethod
    def stop():
        """Stop current slideshow"""
        global slideshow_state
        
        # Remove scheduled job
        if slideshow_state.get('job_id'):
            try:
                scheduler.remove_job(slideshow_state['job_id'])
            except:
                pass
        
        # Clear state
        slideshow_state = {
            'job_id': None,
            'folder_path': '',
            'current_image_name': None,
            'loop_count': 0,
            'images': [],
            'settings': {}
        }

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        session['username'] = username
        return username

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def detect_optimal_format(accept_header):
    """Detect best image format based on Accept header (Next.js style)"""
    if not accept_header:
        return 'jpeg'
    
    # Check Pillow format support
    try:
        from PIL import Image
        available_formats = Image.registered_extensions().values()
    except:
        return 'jpeg'
    
    # Modern browsers with AVIF support (Chrome 85+, Firefox 93+)
    if 'image/avif' in accept_header and 'AVIF' in available_formats:
        return 'avif'
    # WebP support (Chrome, Firefox, Safari 14+)  
    elif 'image/webp' in accept_header and 'WEBP' in available_formats:
        return 'webp'
    else:
        return 'jpeg'

def create_optimized_thumbnail(image_path, thumb_path, format='jpeg', quality=85, size=THUMBNAIL_SIZE):
    """Create optimized thumbnail with modern formats (Next.js inspired)"""
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        
        # Convert RGBA to RGB if necessary (for PNG with alpha channel)
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode == 'P':
            img = img.convert('RGB')
        
        img.thumbnail(size, Image.Resampling.LANCZOS)
        
        # Save in optimal format
        if format == 'avif':
            img.save(thumb_path, 'AVIF', quality=quality)
        elif format == 'webp':
            img.save(thumb_path, 'WebP', quality=quality, method=6)
        else:
            img.save(thumb_path, 'JPEG', quality=quality, optimize=True, progressive=True)
        
        return True
    except Exception as e:
        print(f"Error creating optimized thumbnail: {e}")
        # Fallback to JPEG if modern format fails
        if format != 'jpeg':
            return create_optimized_thumbnail(image_path, thumb_path, 'jpeg', quality, size)
        return False

def create_thumbnail(image_path, thumb_path):
    """Legacy function - keep for compatibility"""
    return create_optimized_thumbnail(image_path, thumb_path, 'jpeg', 85)

def resize_large_image(image_path, max_size_mb=MAX_IMAGE_SIZE_MB):
    """Resize image if too large"""
    file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
    if file_size_mb <= max_size_mb:
        return False
    
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        
        # Calculate new size to get under max_size_mb
        reduction_factor = (max_size_mb / file_size_mb) ** 0.5
        new_size = (int(img.width * reduction_factor), int(img.height * reduction_factor))
        
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        img.save(image_path, quality=85, optimize=True)
        return True
    except Exception as e:
        print(f"Error resizing image: {e}")
        return False

def check_storage_and_cleanup():
    """Check storage usage and cleanup if needed"""
    total_size = 0
    image_files = []
    
    # Walk through all folders
    for root, dirs, files in os.walk(BASE_FOLDER):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                file_path = os.path.join(root, file)
                file_size = os.path.getsize(file_path)
                file_mtime = os.path.getmtime(file_path)
                total_size += file_size
                image_files.append((file_path, file_size, file_mtime))
    
    total_size_mb = total_size / (1024 * 1024)
    
    if total_size_mb > MAX_STORAGE_MB * 0.9:  # 90% threshold
        # Sort by modification time (oldest first)
        image_files.sort(key=lambda x: x[2])
        
        # Remove oldest files until under 80% of max
        target_size = MAX_STORAGE_MB * 0.8 * 1024 * 1024
        for file_path, file_size, _ in image_files:
            if total_size <= target_size:
                break
            
            try:
                os.remove(file_path)
                # Remove thumbnail
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
    tree = FolderManager.get_folder_tree()
    return jsonify({'tree': tree})

@app.route('/api/folder', methods=['POST'])
@auth.login_required
def api_create_folder():
    data = request.json
    path = data.get('path', '').strip('/')
    
    if FolderManager.create_folder(path):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to create folder'}), 400

@app.route('/api/set_current', methods=['POST'])
@auth.login_required
def api_set_current_image():
    data = request.json
    image_path = data.get('image_path')
    
    if not image_path:
        return jsonify({'error': 'No image path provided'}), 400
    
    full_path = os.path.join(IMAGE_PATH, image_path)
    if not os.path.exists(full_path):
        return jsonify({'error': 'Image not found'}), 404
    
    # Update current image globals and enable manual override
    global current_folder, current_image, manual_override
    folder_part = os.path.dirname(image_path)
    image_name = os.path.basename(image_path)
    
    current_folder = folder_part
    current_image = image_name
    manual_override = True  # Override playlist with manual selection
    
    return jsonify({
        'success': True,
        'current_folder': current_folder,
        'current_image': current_image,
        'message': f'Current image set to {image_name}'
    })

@app.route('/api/playlist/')
@app.route('/api/playlist/<path:folder_path>')
@auth.login_required
def api_get_playlist(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    if not os.path.exists(full_path):
        os.makedirs(full_path, exist_ok=True)
    
    playlist = PlaylistManager.load_playlist(full_path)
    
    # Get images
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
    
    playlist = PlaylistManager.update_order(full_path, new_order)
    return jsonify({'success': True, 'playlist': playlist})

@app.route('/api/playlist//settings', methods=['POST'])
@app.route('/api/playlist/<path:folder_path>/settings', methods=['POST'])
@auth.login_required
def api_update_settings(folder_path=''):
    folder_path = folder_path or ''
    full_path = os.path.join(BASE_FOLDER, folder_path)
    
    data = request.json
    playlist = PlaylistManager.load_playlist(full_path)
    
    if 'settings' in data:
        playlist['settings'].update(data['settings'])
    if 'description' in data:
        playlist['description'] = data['description']
    
    PlaylistManager.save_playlist(full_path, playlist)
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
            # Add timestamp to avoid conflicts
            name, ext = os.path.splitext(filename)
            filename = f"{name}_{int(time.time())}{ext}"
            
            file_path = os.path.join(full_path, filename)
            file.save(file_path)
            
            # Fix orientation
            try:
                img = Image.open(file_path)
                img = ImageOps.exif_transpose(img)
                img.save(file_path)
            except:
                pass
            
            # Resize if needed
            resize_large_image(file_path)
            
            # Create thumbnail
            thumb_name = f"{folder_path.replace('/', '_')}_{os.path.splitext(filename)[0]}_thumb.jpg"
            thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
            create_thumbnail(file_path, thumb_path)
            
            uploaded.append(filename)
    
    # Update playlist
    PlaylistManager.update_order(full_path)
    
    return jsonify({'success': True, 'uploaded': uploaded})

# Track thumbnail API calls
thumbnail_call_count = 0

@app.route('/api/thumbnail/<path:image_path>')
def api_get_thumbnail(image_path):
    """Next.js inspired optimized thumbnail endpoint"""
    global thumbnail_call_count
    thumbnail_call_count += 1
    
    # Get client preferences
    accept_header = request.headers.get('Accept', '')
    quality = int(request.args.get('q', '85'))  # ?q=75 for quality
    width = int(request.args.get('w', '300'))   # ?w=150 for width
    
    # Determine optimal format based on browser support
    optimal_format = detect_optimal_format(accept_header)
    
    # Generate format-specific thumbnail name
    folder_parts = os.path.dirname(image_path).replace('/', '_')
    filename = os.path.basename(image_path)
    base_name = os.path.splitext(filename)[0]
    
    # Create unique name with format and params: image_w300_q85.webp
    format_ext = 'jpg' if optimal_format == 'jpeg' else optimal_format
    thumb_name = f"{folder_parts}_{base_name}_w{width}_q{quality}.{format_ext}" if folder_parts else f"{base_name}_w{width}_q{quality}.{format_ext}"
    thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
    full_image_path = os.path.join(BASE_FOLDER, image_path)
    
    if not os.path.exists(full_image_path):
        return '', 404
    
    # Check if optimized thumbnail needs generation
    need_regenerate = False
    
    if not os.path.exists(thumb_path):
        need_regenerate = True
    else:
        # Check if source is newer than thumbnail
        image_mtime = os.path.getmtime(full_image_path)
        thumb_mtime = os.path.getmtime(thumb_path)
        if image_mtime > thumb_mtime:
            need_regenerate = True
    
    # Generate optimized thumbnail
    if need_regenerate:
        thumbnail_size = (width, width)  # Square thumbnails
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
        
        # Next.js style headers for aggressive caching
        response.headers.update({
            'Cache-Control': 'public, max-age=31536000, immutable',  # 1 year cache
            'Vary': 'Accept',  # Important for format negotiation
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
        
        # Remove thumbnail
        folder_parts = os.path.dirname(image_path).replace('/', '_')
        filename = os.path.basename(image_path)
        thumb_name = f"{folder_parts}_{os.path.splitext(filename)[0]}_thumb.jpg" if folder_parts else f"{os.path.splitext(filename)[0]}_thumb.jpg"
        thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        
        # Update playlist
        folder_path = os.path.dirname(full_path)
        PlaylistManager.update_order(folder_path)
        
        return jsonify({'success': True})
    
    return jsonify({'error': 'Image not found'}), 404

@app.route('/api/folder/<path:folder_path>', methods=['DELETE'])
@auth.login_required
def api_delete_folder(folder_path):
    """Delete a folder and all its contents"""
    full_folder_path = os.path.join(BASE_FOLDER, folder_path)
    
    if not os.path.exists(full_folder_path):
        return jsonify({'error': 'Folder not found'}), 404
    
    if not os.path.isdir(full_folder_path):
        return jsonify({'error': 'Path is not a folder'}), 400
    
    # Don't allow deleting root folder
    if full_folder_path == BASE_FOLDER:
        return jsonify({'error': 'Cannot delete root folder'}), 400
    
    try:
        # Remove all images and thumbnails
        for root, dirs, files in os.walk(full_folder_path, topdown=False):
            for file in files:
                file_path = os.path.join(root, file)
                
                # Remove thumbnail if it's an image
                if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                    rel_path = os.path.relpath(file_path, BASE_FOLDER)
                    folder_parts = os.path.dirname(rel_path).replace('/', '_')
                    filename = os.path.splitext(file)[0]
                    thumb_name = f"{folder_parts}_{filename}_thumb.jpg" if folder_parts else f"{filename}_thumb.jpg"
                    thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                
                # Remove the file
                os.remove(file_path)
            
            # Remove empty directories
            for dir in dirs:
                dir_path = os.path.join(root, dir)
                try:
                    os.rmdir(dir_path)
                except OSError:
                    pass  # Directory not empty, will be handled in next iteration
        
        # Remove the main folder
        os.rmdir(full_folder_path)
        
        return jsonify({'success': True, 'message': f'Folder "{folder_path}" deleted successfully'})
    
    except Exception as e:
        return jsonify({'error': f'Failed to delete folder: {str(e)}'}), 500

@app.route('/api/folder/<path:folder_path>/rename', methods=['POST'])
@auth.login_required
def api_rename_folder(folder_path):
    """Rename a folder"""
    print(f"Rename request for: '{folder_path}'")
    data = request.json
    new_name = data.get('new_name', '').strip()
    print(f"New name: '{new_name}'")
    
    if not new_name:
        return jsonify({'error': 'New name cannot be empty'}), 400
    
    # Validate new name doesn't contain invalid characters
    if any(char in new_name for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        return jsonify({'error': 'Invalid characters in folder name'}), 400
    
    full_folder_path = os.path.join(BASE_FOLDER, folder_path)
    print(f"Full path: '{full_folder_path}'")
    print(f"Exists: {os.path.exists(full_folder_path)}")
    
    if not os.path.exists(full_folder_path):
        return jsonify({'error': 'Folder not found'}), 404
    
    if not os.path.isdir(full_folder_path):
        return jsonify({'error': 'Path is not a folder'}), 400
    
    # Calculate new path
    parent_dir = os.path.dirname(full_folder_path)
    new_full_path = os.path.join(parent_dir, new_name)
    
    # Check if target already exists
    if os.path.exists(new_full_path):
        return jsonify({'error': 'A folder with that name already exists'}), 400
    
    try:
        # Rename the folder
        os.rename(full_folder_path, new_full_path)
        
        # Update any playlists that reference this folder
        old_rel_path = os.path.relpath(full_folder_path, BASE_FOLDER).replace('\\', '/')
        new_rel_path = os.path.relpath(new_full_path, BASE_FOLDER).replace('\\', '/')
        
        return jsonify({'success': True, 'message': f'Folder renamed to "{new_name}"', 'new_path': new_rel_path})
    
    except Exception as e:
        return jsonify({'error': f'Failed to rename folder: {str(e)}'}), 500

@app.route('/api/folder/move', methods=['POST'])
@auth.login_required
def api_move_folder():
    """Move a folder to a new location"""
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
    
    # Ensure target directory exists
    target_parent = os.path.dirname(target_full)
    os.makedirs(target_parent, exist_ok=True)
    
    try:
        # Move the folder
        shutil.move(source_full, target_full)
        
        # Update playlist files if they exist
        old_playlist = os.path.join(source_full, '.playlist.json')
        new_playlist = os.path.join(target_full, '.playlist.json')
        
        if os.path.exists(new_playlist):
            # Update playlist paths if needed
            pass
        
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
    
    if FolderManager.move_image(image, from_folder, to_folder):
        return jsonify({'success': True})
    
    return jsonify({'error': 'Failed to move image'}), 400

@app.route('/api/push/<path:image_path>', methods=['POST'])
@auth.login_required
def api_push_image(image_path):
    full_path = os.path.join(BASE_FOLDER, image_path)
    
    if not os.path.exists(full_path):
        return jsonify({'error': 'Image not found'}), 404
    
    # Create push job
    job_id = str(uuid.uuid4())
    image_name = os.path.basename(image_path)
    job = PushJob(job_id, image_name, full_path)
    push_jobs[job_id] = job
    
    # Start async push
    thread = threading.Thread(target=async_push_with_feedback, args=(job_id, full_path))
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
    """Get push job status"""
    job = push_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(job.to_dict())

@app.route('/api/image/info')
def api_image_info():
    """Get current image hash and metadata for change detection"""
    global current_folder, current_image, slideshow_state, manual_override

    # Capture ESP32 device stats if provided
    global esp32_stats
    if request.args.get("battery"):
        esp32_stats["battery"] = int(request.args.get("battery", -1))
        esp32_stats["rssi"] = int(request.args.get("rssi", 0))
        esp32_stats["heap"] = int(request.args.get("heap", 0))
        esp32_stats["uptime"] = int(request.args.get("uptime", 0))
        esp32_stats["last_seen"] = datetime.now().strftime("%H:%M:%S")
        print("[ESP32] Battery:", esp32_stats["battery"], "% | RSSI:", esp32_stats["rssi"], "dBm | Heap:", esp32_stats["heap"], "B | Uptime:", esp32_stats["uptime"], "s")
    try:
        # Priority 1: Manual override - use Set Current selection even if playlist is running
        if manual_override and current_image:
            pass  # Keep current manual selection
        # Priority 2: If slideshow is running and no manual override, use current slideshow image
        elif slideshow_state.get('job_id') and slideshow_state.get('current_image_name'):
            # slideshow_state.folder_path is already the full path from BASE_FOLDER
            # We need to extract just the folder name relative to BASE_FOLDER
            folder_full_path = slideshow_state.get('folder_path', '')
            if folder_full_path.startswith(BASE_FOLDER):
                current_folder = os.path.relpath(folder_full_path, BASE_FOLDER)
            else:
                current_folder = folder_full_path
            current_image = slideshow_state.get('current_image_name')
        # Priority 2: If no image set, pick first available image from playlists directory
        elif not current_image:
            try:
                for root, dirs, files in os.walk(BASE_FOLDER):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.png', '.jpeg')):
                            # Get relative path from BASE_FOLDER
                            rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                            if '/' in rel_path:
                                current_folder, current_image = rel_path.split('/', 1)
                            else:
                                current_image = rel_path
                            break
                    if current_image:
                        break
                if not current_image:
                    return jsonify({'error': 'No images available'}), 404
            except Exception as e:
                return jsonify({'error': f'No current image: {e}'}), 404
            
        # Construct correct path: folder/image or just image if no folder
        if current_folder:
            full_path = os.path.join(BASE_FOLDER, current_folder, current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        # Calculate MD5 hash of current image file
        import hashlib
        hash_md5 = hashlib.md5()
        with open(full_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        
        file_hash = hash_md5.hexdigest()[:12]  # Short hash
        
        return jsonify({
            'hash': file_hash,
            'image_name': os.path.basename(current_image),
            'timestamp': int(os.path.getmtime(full_path))
        })
        
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/api/image')
def api_get_current_image():
    """Serve current image in e-paper format for HTTP polling"""
    try:
        slideshow_data = SlideshowManager.get_status()
        current_folder = slideshow_data.get('current_folder')
        current_image = slideshow_data.get('current_image')
        
        if not current_image:
            return jsonify({'error': 'No current image'}), 404
            
        # Construct correct path: folder/image or just image if no folder
        if current_folder:
            full_path = os.path.join(BASE_FOLDER, current_folder, current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        # Convert image to e-paper format
        epaper_data = convert_image_to_epaper_format(full_path)
        
        if not epaper_data:
            return jsonify({'error': 'Failed to convert image'}), 500
            
        # Return binary image data
        from flask import Response
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
    """Convert image to e-paper binary format using Sierra SORBET dithering"""
    try:
        from dither_sierra_sorbet import sierra_sorbet_dither
        import numpy as np
        from PIL import Image
        
        # Constants
        EPD_W, EPD_H = 1200, 1600
        PALETTE_RGB = [(0,0,0), (255,255,255), (255,255,0), (255,0,0), (0,0,255), (0,255,0)]
        CODE_MAP = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]
        
        # Load and process image
        im = Image.open(image_path).convert("RGB")
        im = crop_center_zoom(im)
        im = im.resize((EPD_W, EPD_H), Image.Resampling.LANCZOS)
        im = enhance_image(im)
        
        # Sierra SORBET dithering
        img_array = np.array(im, dtype=np.float32)
        palette_np = np.array(PALETTE_RGB, dtype=np.float32)
        indices_2d = sierra_sorbet_dither(img_array, palette_np)
        indices = indices_2d.flatten()
        
        # Pack to binary format (same as push script)
        left_data = pack_half(indices, 0, 600)
        right_data = pack_half(indices, 600, 1200)
        
        # Combine Master + Slave data
        binary_data = left_data + right_data
        return binary_data
        
    except Exception as e:
        print(f"Error converting image with Sierra SORBET: {e}")
        import traceback
        traceback.print_exc()
        return None

def convert_image_simple(image_path):
    """Fallback simple conversion if Sierra SORBET fails"""
    try:
        from PIL import ImageEnhance
        import numpy as np
        
        # Same logic as your push script but simplified
        img = Image.open(image_path).convert('RGB')
        
        # Crop center zoom like your script
        img = crop_center_zoom(img)
        img = img.resize((1200, 1600), Image.Resampling.LANCZOS)
        img = enhance_image(img)
        
        # Try to import Sierra SORBET if available
        try:
            from dither_sierra_sorbet import sierra_sorbet_dither
            img_array = np.array(img, dtype=np.float32)
            palette_np = np.array([
                [0,0,0], [255,255,255], [255,255,0], 
                [255,0,0], [0,0,255], [0,255,0]
            ], dtype=np.float32)
            indices_2d = sierra_sorbet_dither(img_array, palette_np)
            idx = indices_2d.flatten()
        except ImportError:
            # Basic fallback
            img = img.quantize(colors=6, method=Image.Quantize.MEDIANCUT)
            idx = list(img.getdata())
        
        # Pack into binary format like your script
        left = pack_half(idx, 0, 600)
        right = pack_half(idx, 600, 1200)
        
        return left + right
        
    except Exception as e:
        print(f"Simple conversion failed: {e}")
        return None

def crop_center_zoom(im, target_ratio=12/16):
    """Same crop logic as push script"""
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
    """Same enhancement as push script"""
    from PIL import ImageEnhance
    enhancer = ImageEnhance.Contrast(im)
    im = enhancer.enhance(1.2)
    enhancer = ImageEnhance.Color(im)
    im = enhancer.enhance(1.3)
    enhancer = ImageEnhance.Brightness(im)
    im = enhancer.enhance(1.05)
    return im

def pack_half(indices, x0, x1):
    """Same packing logic as push script"""
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
    """Stream current image data line by line for ESP32 HTTP polling"""
    global current_folder, current_image
    try:
        # Use same logic as /api/image/info
        if not current_image:
            try:
                for root, dirs, files in os.walk(BASE_FOLDER):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.png', '.jpeg')):
                            # Get relative path from BASE_FOLDER
                            rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                            if '/' in rel_path:
                                current_folder, current_image = rel_path.split('/', 1)
                            else:
                                current_image = rel_path
                            break
                    if current_image:
                        break
                if not current_image:
                    return jsonify({'error': 'No images available'}), 404
            except Exception as e:
                return jsonify({'error': f'No current image: {e}'}), 404
            
        # Construct correct path: folder/image or just image if no folder
        if current_folder:
            full_path = os.path.join(BASE_FOLDER, current_folder, current_image)
        else:
            full_path = os.path.join(BASE_FOLDER, current_image)
        if not os.path.exists(full_path):
            return jsonify({'error': 'Current image file not found'}), 404
        
        print(f"[HTTP] Streaming {current_image}")
        
        # Convert image to binary data using existing function
        binary_data = convert_image_to_epaper_format(full_path)
        if not binary_data:
            return jsonify({'error': 'Failed to convert image'}), 500
            
        def generate_stream():
            # Stream 300 bytes at a time (line by line)
            BYTES_PER_LINE_HALF = 300
            for i in range(0, len(binary_data), BYTES_PER_LINE_HALF):
                chunk = binary_data[i:i + BYTES_PER_LINE_HALF]
                if len(chunk) == BYTES_PER_LINE_HALF:
                    yield chunk
                else:
                    # Pad last chunk if needed
                    yield chunk + b'\x00' * (BYTES_PER_LINE_HALF - len(chunk)) # Corrected escape sequence
        
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
    
    # Start slideshow with scheduler
    success = SlideshowManager.start(full_path)
    
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'No images found in folder'}), 400

@app.route('/api/slideshow/stop', methods=['POST'])
@auth.login_required
def api_stop_slideshow():
    SlideshowManager.stop()
    return jsonify({'success': True})

@app.route('/api/slideshow/next', methods=['POST'])
@auth.login_required
def api_slideshow_next():
    """Skip to next image in slideshow"""
    status = SlideshowManager.get_status()
    if status.get('running'):
        try:
            SlideshowManager.push_next_image()
            return jsonify({'success': True, 'message': 'Advanced to next image'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Slideshow not running'}), 400

@app.route('/api/thumbnails/refresh', methods=['POST'], defaults={'folder_path': ''})
@app.route('/api/thumbnails/refresh/<path:folder_path>', methods=['POST'])
@auth.login_required
def api_refresh_thumbnails(folder_path=''):
    """Force regenerate all thumbnails for a folder"""
    full_folder = os.path.join(BASE_FOLDER, folder_path) if folder_path else BASE_FOLDER
    
    if not os.path.exists(full_folder):
        return jsonify({'error': 'Folder not found'}), 404
    
    regenerated = 0
    errors = 0
    
    # Walk through all images in the folder
    for root, dirs, files in os.walk(full_folder):
        for file in files:
            if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                image_path = os.path.join(root, file)
                rel_path = os.path.relpath(image_path, BASE_FOLDER)
                
                # Generate thumbnail name
                folder_parts = os.path.dirname(rel_path).replace('/', '_')
                filename = os.path.splitext(file)[0]
                thumb_name = f"{folder_parts}_{filename}_thumb.jpg" if folder_parts else f"{filename}_thumb.jpg"
                thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
                
                # Force recreate thumbnail
                try:
                    # Remove old thumbnail if exists
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
                    
                    # Create new thumbnail
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
    return jsonify(SlideshowManager.get_status())

@app.route('/api/esp32/stats')
@auth.login_required
def api_esp32_stats():
    """Get ESP32 device stats"""
    global esp32_stats
    return jsonify(esp32_stats)

@auth.login_required
def api_scheduler_jobs():
    """Get list of scheduled jobs"""
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
    """Get thumbnail API call statistics"""
    return jsonify({
        'total_calls': thumbnail_call_count,
        'message': f'Thumbnail API called {thumbnail_call_count} times since server start'
    })

def cleanup_orphaned_thumbnails():
    """Remove thumbnails for images that no longer exist"""
    try:
        if not os.path.exists(THUMBNAILS_FOLDER):
            return
        
        cleaned_count = 0
        
        # Get all existing images
        existing_images = set()
        for root, dirs, files in os.walk(BASE_FOLDER):
            for file in files:
                if file.lower().endswith(('.jpg', '.jpeg', '.png')):
                    rel_path = os.path.relpath(os.path.join(root, file), BASE_FOLDER)
                    folder_parts = os.path.dirname(rel_path).replace('/', '_')
                    filename = os.path.splitext(file)[0]
                    thumb_name = f"{folder_parts}_{filename}_thumb.jpg" if folder_parts else f"{filename}_thumb.jpg"
                    existing_images.add(thumb_name)
        
        # Check all thumbnails
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

# Schedule cleanup job to run every hour
scheduler.add_job(
    func=cleanup_orphaned_thumbnails,
    trigger="interval",
    hours=1,
    id='thumbnail_cleanup',
    name='Cleanup orphaned thumbnails',
    replace_existing=True
)

if __name__ == '__main__':
    FolderManager.ensure_base_folder()
    # Run initial cleanup
    cleanup_orphaned_thumbnails()
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)
