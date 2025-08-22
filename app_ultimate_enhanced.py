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
from flask import Flask, request, jsonify, render_template_string, send_file, session
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
    return render_template_string('''<!DOCTYPE html>
<html>
<head>
    <title>Ink Screen Manager - Ultimate</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <!-- Cache buster timestamp: 2025-08-19-14-45 -->
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #f8fafc;
            min-height: 100vh;
            display: flex;
        }
        
        /* Notifications */
        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 20px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            z-index: 10000;
            display: none;
            animation: slideIn 0.3s ease-out;
            max-width: 300px;
        }
        
        .notification.show {
            display: block;
        }
        
        .notification.success {
            border-left: 5px solid #00d2d3;
        }
        
        .notification.error {
            border-left: 5px solid #ff4757;
        }
        
        .notification.info {
            border-left: 5px solid #667eea;
        }
        
        .notification-content {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .notification-icon {
            font-size: 24px;
        }
        
        .notification-text {
            flex: 1;
        }
        
        .notification-title {
            font-weight: 600;
            margin-bottom: 5px;
        }
        
        .notification-message {
            font-size: 14px;
            color: #666;
        }
        
        @keyframes slideIn {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        
        /* Hamburger Menu */
        .hamburger {
            display: none;
            position: fixed;
            top: 20px;
            left: 20px;
            z-index: 1001;
            background: rgba(255, 255, 255, 0.95);
            border: none;
            border-radius: 8px;
            padding: 10px;
            cursor: pointer;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
        }
        
        .hamburger span {
            display: block;
            width: 25px;
            height: 3px;
            background: #667eea;
            margin: 5px 0;
            transition: all 0.3s;
            border-radius: 2px;
        }
        
        .hamburger.active span:nth-child(1) {
            transform: rotate(45deg) translate(8px, 8px);
        }
        
        .hamburger.active span:nth-child(2) {
            opacity: 0;
        }
        
        .hamburger.active span:nth-child(3) {
            transform: rotate(-45deg) translate(6px, -6px);
        }

        /* Modern Sidebar - Figma Style */
        .sidebar {
            width: 300px;
            background: #ffffff;
            border-right: 1px solid #e2e8f0;
            overflow-y: auto;
            padding: 0;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
        }
        
        .sidebar-header {
            padding: 24px 20px 20px 20px;
            border-bottom: 1px solid #f1f5f9;
            background: #fafbfc;
        }
        
        .sidebar h2 {
            font-size: 18px;
            font-weight: 600;
            color: #0f172a;
            margin: 0 0 4px 0;
            letter-spacing: -0.025em;
        }
        
        .sidebar-subtitle {
            font-size: 13px;
            color: #64748b;
            margin: 0;
        }
        
        .new-folder-btn {
            width: 100%;
            margin-top: 16px;
            padding: 10px 16px;
            background: #6366f1;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        
        .new-folder-btn:hover {
            background: #5855eb;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
        }
        
        .folders-container {
            flex: 1;
            padding: 16px 0;
            overflow-y: auto;
        }
        
        .folder-tree {
            list-style: none;
            margin: 0;
            padding: 0;
        }
        
        .folder-item {
            margin: 2px 0;
            transition: all 0.2s ease;
            position: relative;
        }
        
        .folder-item-content {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 16px;
            margin: 0 8px;
            cursor: pointer;
            border-radius: 8px;
            transition: all 0.2s ease;
            min-height: 40px;
        }
        
        .folder-item:hover .folder-item-content {
            background: #f8fafc;
            transform: translateX(2px);
        }
        
        .folder-item.active .folder-item-content {
            background: #3b82f6;
            color: white;
        }
        
        .folder-icon {
            font-size: 16px;
            margin-right: 10px;
            transition: all 0.2s ease;
        }
        
        .folder-left {
            flex: 1;
            display: flex;
            align-items: center;
            min-width: 0;
        }
        
        .folder-name {
            font-size: 14px;
            font-weight: 500;
            color: #374151;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            transition: color 0.2s ease;
        }
        
        .folder-item.active .folder-name {
            color: white;
        }
        
        .folder-actions {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        
        .folder-badge {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            font-size: 10px;
            font-weight: 600;
            padding: 3px 7px;
            border-radius: 12px;
            min-width: 20px;
            text-align: center;
            margin-right: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        
        .folder-item.active .folder-badge {
            background: rgba(255, 255, 255, 0.2);
            color: white;
        }
        
        .folder-action-btn {
            width: 20px;
            height: 20px;
            border: none;
            background: transparent;
            border-radius: 4px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            transition: all 0.2s ease;
            color: #9ca3af;
            opacity: 0;
        }
        
        .folder-item:hover .folder-action-btn {
            opacity: 1;
        }
        
        .folder-item.active .folder-action-btn {
            opacity: 1;
            color: rgba(255, 255, 255, 0.7);
        }
        
        .folder-action-btn:hover {
            background: rgba(0, 0, 0, 0.1);
            color: #374151;
        }
        
        .folder-item.active .folder-action-btn:hover {
            background: rgba(255, 255, 255, 0.2);
            color: white;
        }
        
        .folder-action-btn.delete:hover {
            background: #ef4444;
            color: white;
        }
        
        .folder-children {
            padding-left: 32px;
            border-left: 1px solid #f1f5f9;
            margin-left: 32px;
        }
        
        /* Main Content */
        .main-content {
            flex: 1;
            padding: 20px;
            overflow-y: auto;
        }
        
        .header {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            padding: 20px;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        
        .breadcrumb {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }
        
        .breadcrumb-item {
            color: #667eea;
            text-decoration: none;
            padding: 5px 10px;
            background: rgba(102, 126, 234, 0.1);
            border-radius: 5px;
            transition: all 0.3s;
        }
        
        .breadcrumb-item:hover {
            background: rgba(102, 126, 234, 0.2);
        }
        
        .controls {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }
        
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.3s;
            font-weight: 500;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }
        
        .btn-secondary {
            background: white;
            color: #667eea;
            border: 2px solid #667eea;
        }
        
        .btn-danger {
            background: #ff4757;
            color: white;
        }
        
        .btn-folder-delete {
            background: #ff4757;
            color: white;
            border: none;
            border-radius: 5px;
            padding: 5px 8px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 10px;
            transition: all 0.3s;
        }
        
        .btn-folder-delete:hover {
            background: #ff3838;
            transform: scale(1.1);
        }
        
        .btn-success {
            background: #00d2d3;
            color: white;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(0,0,0,0.2);
        }
        
        /* Status Bar */
        .status-bar {
            background: rgba(255, 255, 255, 0.95);
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        
        .status-content {
            display: flex;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        
        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .status-indicator {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #ddd;
        }
        
        .status-indicator.active {
            background: #00d2d3;
            animation: pulse 2s infinite;
        }
        
        /* Now Playing Section */
        .now-playing {
            display: none;
            background: linear-gradient(135deg, rgba(102, 126, 234, 0.1), rgba(118, 75, 162, 0.1));
            padding: 15px;
            border-radius: 10px;
            margin-top: 15px;
        }
        
        .now-playing.active {
            display: block;
        }
        
        .playing-content {
            display: flex;
            gap: 30px;
            align-items: center;
        }
        
        .playing-item {
            text-align: center;
        }
        
        .playing-item h4 {
            margin-bottom: 10px;
            color: #667eea;
        }
        
        .playing-thumbnail {
            width: 120px;
            height: 90px;
            object-fit: cover;
            border-radius: 8px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .playing-thumbnail:hover {
            transform: scale(1.05);
            box-shadow: 0 8px 20px rgba(0,0,0,0.3);
        }
        
        .playing-name {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
            max-width: 120px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .next-arrow {
            font-size: 24px;
            color: #667eea;
            animation: slide 1s ease-in-out infinite;
        }
        
        @keyframes slide {
            0%, 100% { transform: translateX(0); }
            50% { transform: translateX(10px); }
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        /* Upload Zone */
        .upload-zone {
            background: rgba(255, 255, 255, 0.95);
            border: 3px dashed #667eea;
            border-radius: 15px;
            padding: 40px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
            margin-bottom: 20px;
        }
        
        .upload-zone:hover, .upload-zone.dragover {
            background: rgba(102, 126, 234, 0.1);
            border-color: #764ba2;
        }
        
        /* Image Grid */
        .image-grid {
            display: grid !important;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)) !important;
            gap: 15px !important;
            padding: 20px;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 15px;
        }
        
        .image-item {
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            transition: all 0.3s;
            cursor: grab;
            position: relative;
        }
        
        .image-item.dragging {
            opacity: 0.5;
            cursor: grabbing;
        }
        
        .image-item.drag-over {
            border: 3px solid #667eea;
        }
        
        .image-item.drag-over-left {
            border-left: 6px solid #667eea;
            border-top: 2px solid #667eea;
            border-bottom: 2px solid #667eea;
        }
        
        .image-item.drag-over-right {
            border-right: 6px solid #667eea;
            border-top: 2px solid #667eea;
            border-bottom: 2px solid #667eea;
        }
        
        .image-item:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
        }
        
        .image-item img {
            width: 100%;
            height: 150px;
            object-fit: cover;
        }
        
        .image-info {
            padding: 10px;
        }
        
        .image-name {
            font-size: 12px;
            color: #666;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .image-actions {
            display: flex;
            gap: 5px;
            margin-top: 10px;
        }
        
        .image-actions button {
            flex: 1;
            padding: 5px;
            font-size: 12px;
        }
        
        .order-badge {
            position: absolute;
            top: 10px;
            left: 10px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
            padding: 5px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }
        
        /* Modal */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            backdrop-filter: blur(5px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        
        .modal.show {
            display: flex;
        }
        
        .modal-content {
            background: white;
            border-radius: 15px;
            padding: 30px;
            max-width: 500px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }
        
        .modal-header {
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #667eea;
        }
        
        .modal-body {
            margin: 20px 0;
        }
        
        .form-group {
            margin-bottom: 15px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 5px;
            color: #333;
            font-weight: 500;
        }
        
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            border-color: #667eea;
            outline: none;
        }
        
        .toggle-switch {
            position: relative;
            width: 50px;
            height: 25px;
            background: #ddd;
            border-radius: 25px;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .toggle-switch.active {
            background: linear-gradient(135deg, #667eea, #764ba2);
        }
        
        .toggle-switch::after {
            content: '';
            position: absolute;
            width: 21px;
            height: 21px;
            background: white;
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: all 0.3s;
        }
        
        .toggle-switch.active::after {
            left: 27px;
        }
        
        /* Progress Bar */
        .progress-bar {
            width: 100%;
            height: 20px;
            background: #f0f0f0;
            border-radius: 10px;
            overflow: hidden;
            margin: 10px 0;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(135deg, #667eea, #764ba2);
            transition: width 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 12px;
        }
        
        /* Push Progress Notification */
        .notification.progress {
            border-left: 5px solid #667eea;
            min-width: 280px;
        }
        
        .notification-progress {
            margin-top: 10px;
        }
        
        .notification-progress .progress-bar {
            height: 6px;
            margin: 5px 0 0 0;
        }
        
        .notification-progress .progress-fill {
            font-size: 10px;
            height: 100%;
        }
        
        /* Mobile Responsive */
        @media (max-width: 768px) {
            .hamburger {
                display: block;
            }
            
            .sidebar {
                position: fixed;
                top: 0;
                left: 0;
                height: 100vh;
                z-index: 1000;
                transform: translateX(-100%);
                width: 300px;
            }
            
            .sidebar.open {
                transform: translateX(0);
            }
            
            .main-content {
                flex: 1;
                padding: 70px 10px 10px 10px;
            }
            
            .header {
                padding: 15px;
                margin-bottom: 15px;
            }
            
            .controls {
                flex-direction: column;
                gap: 8px;
            }
            
            .btn {
                padding: 12px 16px;
                font-size: 16px;
                width: 100%;
            }
            
            .status-bar {
                padding: 10px;
                margin-bottom: 15px;
            }
            
            .status-content {
                flex-direction: column;
                gap: 10px;
                align-items: flex-start;
            }
            
            .playing-content {
                flex-direction: column;
                gap: 15px;
                text-align: center;
            }
            
            .next-arrow {
                transform: rotate(90deg);
                font-size: 20px;
            }
            
            .image-grid {
                grid-template-columns: repeat(2, 1fr);
                gap: 15px;
                padding: 15px;
            }
            
            .image-item img {
                height: 120px;
            }
            
            .breadcrumb {
                flex-wrap: wrap;
                gap: 5px;
            }
            
            .breadcrumb-item {
                padding: 3px 6px;
                font-size: 14px;
            }
            
            .upload-zone {
                padding: 30px 15px;
                margin-bottom: 15px;
            }
            
            .modal-content {
                width: 95%;
                margin: 10px;
                padding: 20px;
            }
            
            .notification {
                top: 10px;
                right: 10px;
                left: 10px;
                max-width: none;
            }
        }
        
        /* Tablet */
        @media (min-width: 769px) and (max-width: 1024px) {
            .image-grid {
                grid-template-columns: repeat(3, 1fr);
            }
            
            .sidebar {
                width: 250px;
            }
        }
    </style>
</head>
<body>
    <button class="hamburger" id="hamburger">
        <span></span>
        <span></span>
        <span></span>
    </button>
    
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <h2>Folders</h2>
            <p class="sidebar-subtitle">Manage your image collections</p>
            <button class="new-folder-btn" onclick="showNewFolderModal()">
                <span>+</span>
                New Folder
            </button>
        </div>
        <div class="folders-container">
            <div id="folderTree" class="folder-tree"></div>
        </div>
    </div>
    
    <div class="main-content">
        <div class="header">
            <div class="breadcrumb" id="breadcrumb">
                <a href="#" class="breadcrumb-item" onclick="loadFolder('')">Home</a>
            </div>
            <div class="controls">
                <button class="btn btn-primary" onclick="document.getElementById('fileInput').click()">
                    📤 Upload Images
                </button>
                <button class="btn btn-secondary" onclick="showPlaylistSettings()">
                    ⚙️ Settings
                </button>
                <button class="btn btn-success" id="playBtn" onclick="toggleSlideshow()">
                    ▶️ Play
                </button>
                <button class="btn btn-danger" id="stopBtn" onclick="stopSlideshow()" style="display: none;">
                    ⏹️ Stop
                </button>
                <button class="btn btn-secondary" onclick="refreshStatus()">
                    🔄 Refresh
                </button>
            </div>
        </div>
        
        <div class="status-bar" id="statusBar">
            <div class="status-content">
                <div class="status-item">
                    <div class="status-indicator" id="statusIndicator"></div>
                    <span id="statusText">Idle</span>
                </div>
                <div class="status-item">
                    <span>🔁 Loop: <span id="loopStatus">Off</span></span>
                </div>
                <div class="status-item">
                    <span>🔀 Shuffle: <span id="shuffleStatus">Off</span></span>
                </div>
                <div class="status-item">
                    <span>🔢 <span id="playlistProgress">-</span></span>
                </div>
                <div class="status-item">
                    <span>⏱️ Next in: <span id="nextChange">-</span></span>
                </div>
            </div>
            <div class="now-playing" id="nowPlaying">
                <div style="border-bottom: 1px solid rgba(102, 126, 234, 0.3); padding-bottom: 10px; margin-bottom: 15px;">
                    <strong>Playing from:</strong> <span id="playingFolder" style="color: #667eea;">-</span>
                </div>
                <div class="playing-content">
                    <div class="playing-item">
                        <h4>Now Playing</h4>
                        <img id="currentThumbnail" class="playing-thumbnail" src="" alt="Current">
                        <div class="playing-name" id="currentImageName">-</div>
                    </div>
                    <div class="next-arrow">→</div>
                    <div class="playing-item">
                        <h4>Next</h4>
                        <img id="nextThumbnail" class="playing-thumbnail" src="" alt="Next">
                        <div class="playing-name" id="nextImageName">-</div>
                        <button class="btn btn-primary" style="margin-top: 10px;" onclick="pushNextNow()">Push Next Now</button>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="upload-zone" id="uploadZone">
            <h3>📸 Drop images here or click to upload</h3>
            <p>Supported formats: JPG, PNG</p>
            <div class="progress-bar" id="uploadProgress" style="display: none;">
                <div class="progress-fill" id="uploadProgressFill">0%</div>
            </div>
        </div>
        
        <div class="image-grid" id="imageGrid"></div>
    </div>
    
    <input type="file" id="fileInput" multiple accept="image/*" style="display: none;">
    
    <!-- Notification -->
    <div class="notification" id="notification">
        <div class="notification-content">
            <span class="notification-icon" id="notificationIcon">✓</span>
            <div class="notification-text">
                <div class="notification-title" id="notificationTitle">Success</div>
                <div class="notification-message" id="notificationMessage">Action completed</div>
                <div class="notification-progress" id="notificationProgress" style="display: none;">
                    <div class="progress-bar">
                        <div class="progress-fill" id="notificationProgressFill">0%</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Modals -->
    <div class="modal" id="newFolderModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Create New Folder</h3>
            </div>
            <div class="modal-body">
                <div class="form-group">
                    <label>Folder Name:</label>
                    <input type="text" id="newFolderName" placeholder="Enter folder name">
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-primary" onclick="createNewFolder()">Create</button>
                <button class="btn btn-secondary" onclick="closeModal('newFolderModal')">Cancel</button>
            </div>
        </div>
    </div>
    
    <div class="modal" id="settingsModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Playlist Settings</h3>
            </div>
            <div class="modal-body">
                <div class="form-group">
                    <label>Interval (seconds):</label>
                    <input type="number" id="intervalInput" min="1" value="300">
                </div>
                <div class="form-group">
                    <label>Loop:</label>
                    <div class="toggle-switch" id="loopToggle" onclick="toggleSwitch(this)"></div>
                </div>
                <div class="form-group">
                    <label>Shuffle:</label>
                    <div class="toggle-switch" id="shuffleToggle" onclick="toggleSwitch(this)"></div>
                </div>
                <div class="form-group">
                    <label>Description:</label>
                    <textarea id="descriptionInput" rows="3"></textarea>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-primary" onclick="savePlaylistSettings()">Save</button>
                <button class="btn btn-secondary" onclick="closeModal('settingsModal')">Cancel</button>
            </div>
        </div>
    </div>
    
    <div class="modal" id="moveModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Move Image</h3>
            </div>
            <div class="modal-body">
                <div class="form-group">
                    <label>Select Destination Folder:</label>
                    <select id="destinationFolder"></select>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-primary" onclick="confirmMove()">Move</button>
                <button class="btn btn-secondary" onclick="closeModal('moveModal')">Cancel</button>
            </div>
        </div>
    </div>
    
    <script>
        let currentFolder = '';
        let selectedImage = null;
        let draggedElement = null;
        let statusTimer = null;
        let countdownTimer = null;
        let notificationTimeout = null;
        
        // Notification system
        function showNotification(title, message, type = 'success', progress = null) {
            const notification = document.getElementById('notification');
            const icon = document.getElementById('notificationIcon');
            const titleEl = document.getElementById('notificationTitle');
            const messageEl = document.getElementById('notificationMessage');
            const progressEl = document.getElementById('notificationProgress');
            const progressFill = document.getElementById('notificationProgressFill');
            
            // Clear existing timeout
            if (notificationTimeout) {
                clearTimeout(notificationTimeout);
            }
            
            // Set content
            titleEl.textContent = title;
            messageEl.textContent = message;
            
            // Handle progress bar
            if (progress !== null) {
                progressEl.style.display = 'block';
                progressFill.style.width = progress + '%';
                progressFill.textContent = Math.round(progress) + '%';
            } else {
                progressEl.style.display = 'none';
            }
            
            // Set type and icon
            notification.className = `notification ${type} show`;
            if (type === 'success') {
                icon.textContent = '✓';
            } else if (type === 'error') {
                icon.textContent = '✗';
            } else if (type === 'info') {
                icon.textContent = 'ℹ';
            } else if (type === 'progress') {
                icon.textContent = '⏳';
            }
            
            // Auto-hide after 3 seconds (only for success/error, not progress)
            if (type !== 'progress') {
                notificationTimeout = setTimeout(() => {
                    notification.classList.remove('show');
                }, 3000);
            }
        }
        
        function hideNotification() {
            const notification = document.getElementById('notification');
            notification.classList.remove('show');
            if (notificationTimeout) {
                clearTimeout(notificationTimeout);
            }
        }
        
        // Hamburger menu functionality
        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const hamburger = document.getElementById('hamburger');
            
            sidebar.classList.toggle('open');
            hamburger.classList.toggle('active');
        }
        
        // Close sidebar when clicking outside on mobile
        function setupMobileInteractions() {
            const sidebar = document.getElementById('sidebar');
            const hamburger = document.getElementById('hamburger');
            
            // Close sidebar when clicking outside
            document.addEventListener('click', function(e) {
                if (window.innerWidth <= 768) {
                    if (!sidebar.contains(e.target) && !hamburger.contains(e.target)) {
                        sidebar.classList.remove('open');
                        hamburger.classList.remove('active');
                    }
                }
            });
            
            // Hamburger click handler
            hamburger.addEventListener('click', function(e) {
                e.stopPropagation();
                toggleSidebar();
            });
            
            // Auto-close sidebar after navigation on mobile
            const folderItems = sidebar.querySelectorAll('.folder-item');
            folderItems.forEach(item => {
                item.addEventListener('click', function() {
                    if (window.innerWidth <= 768) {
                        setTimeout(() => {
                            sidebar.classList.remove('open');
                            hamburger.classList.remove('active');
                        }, 150);
                    }
                });
            });
        }

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            loadFolderTree();
            loadFolder('');
            startStatusPolling();
            setupMobileInteractions();
            
            // Setup upload zone
            const uploadZone = document.getElementById('uploadZone');
            const fileInput = document.getElementById('fileInput');
            
            uploadZone.addEventListener('click', () => fileInput.click());
            
            uploadZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                uploadZone.classList.add('dragover');
            });
            
            uploadZone.addEventListener('dragleave', () => {
                uploadZone.classList.remove('dragover');
            });
            
            uploadZone.addEventListener('drop', (e) => {
                e.preventDefault();
                uploadZone.classList.remove('dragover');
                handleFiles(e.dataTransfer.files);
            });
            
            fileInput.addEventListener('change', (e) => {
                handleFiles(e.target.files);
            });
        });
        
        function loadFolderTree() {
            fetch('/api/folders')
                .then(r => r.json())
                .then(data => {
                    const tree = document.getElementById('folderTree');
                    tree.innerHTML = renderFolderTree(data.tree);
                });
        }
        
        function renderFolderTree(items, level = 0) {
            let html = '';
            for (const item of items) {
                const isRoot = item.name === '📁 Root';
                const displayName = isRoot ? 'Home' : item.name;
                
                html += `
                    <div class="folder-item ${currentFolder === item.path ? 'active' : ''}" 
                         data-folder-path="${item.path}"
                         draggable="true" 
                         ondragstart="startFolderDrag(event)"
                         ondragover="handleFolderDragOver(event)"
                         ondrop="handleFolderDrop(event)">
                        <div class="folder-item-content" onclick="loadFolder('${item.path}')" style="margin-left: ${level * 16}px">
                            <div class="folder-left">
                                <span class="folder-icon">${isRoot ? '🏠' : (level > 0 ? '📁' : '🗂️')}</span>
                                <span class="folder-name">${displayName}</span>
                            </div>
                            <div class="folder-actions">
                                <span class="folder-badge">${item.image_count}</span>
                                ${item.path !== '' ? `<button class="folder-action-btn rename" onclick="startRename(event, '${item.path}')" title="Rename folder">✏️</button>` : ''}
                                ${item.path !== '' ? `<button class="folder-action-btn delete" onclick="event.stopPropagation(); deleteFolder('${item.path}')" title="Delete folder">×</button>` : ''}
                            </div>
                        </div>
                    </div>`;
                
                if (item.children && item.children.length > 0) {
                    html += renderFolderTree(item.children, level + 1);
                }
            }
            return html;
        }
        
        function loadFolder(path) {
            currentFolder = path;
            
            // Update breadcrumb
            const parts = path.split('/').filter(p => p);
            let breadcrumb = '<a href="#" class="breadcrumb-item" onclick="loadFolder(\\'\\')">Home</a>';
            let currentPath = '';
            for (const part of parts) {
                currentPath += (currentPath ? '/' : '') + part;
                breadcrumb += ` > <a href="#" class="breadcrumb-item" onclick="loadFolder('${currentPath}')">${part}</a>`;
            }
            document.getElementById('breadcrumb').innerHTML = breadcrumb;
            
            
            // Load images
            fetch(`/api/playlist/${encodeURIComponent(path)}`)
                .then(r => r.json())
                .then(data => {
                    renderImages(data.images, data.playlist);
                });
            
            // Refresh folder tree
            loadFolderTree();
        }
        
        function renderImages(images, playlist) {
            const grid = document.getElementById('imageGrid');
            const order = playlist.order || [];
            
            // Sort images by order
            const sortedImages = [...images].sort((a, b) => {
                const aIndex = order.indexOf(a.name);
                const bIndex = order.indexOf(b.name);
                if (aIndex === -1 && bIndex === -1) return 0;
                if (aIndex === -1) return 1;
                if (bIndex === -1) return -1;
                return aIndex - bIndex;
            });
            
            grid.innerHTML = sortedImages.map((img, index) => `
                <div class="image-item" draggable="true" data-image="${img.name}" data-index="${index}">
                    <div class="order-badge">#${index + 1}</div>
                    <img src="/api/thumbnail/${encodeURIComponent(currentFolder + '/' + img.name)}?w=300&q=80" alt="${img.name}">
                    <div class="image-info">
                        <div class="image-name" title="${img.name}">${img.name}</div>
                        <div class="image-actions">
                            <button class="btn btn-primary" onclick="pushImage('${img.name}')">Push</button>
                            <button class="btn btn-secondary" onclick="showMoveModal('${img.name}')">Move</button>
                            <button class="btn btn-danger" onclick="deleteImage('${img.name}')">Delete</button>
                        </div>
                    </div>
                </div>
            `).join('');
            
            // Setup drag and drop
            setupDragAndDrop();
        }
        
        function setupDragAndDrop() {
            const items = document.querySelectorAll('.image-item');
            let touchStartY = 0;
            let touchStartX = 0;
            let isTouching = false;
            let dropX = 0; // Store drop X coordinate
            
            items.forEach(item => {
                // Desktop drag and drop
                item.addEventListener('dragstart', (e) => {
                    draggedElement = item;
                    item.classList.add('dragging');
                    e.dataTransfer.effectAllowed = 'move';
                });
                
                item.addEventListener('dragend', () => {
                    item.classList.remove('dragging');
                    // Clear all drag highlights
                    document.querySelectorAll('.image-item').forEach(item => 
                        item.classList.remove('drag-over', 'drag-over-left', 'drag-over-right')
                    );
                    draggedElement = null;
                });
                
                item.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    if (draggedElement && draggedElement !== item) {
                        // Remove all drag classes
                        item.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
                        
                        // Determine position for visual feedback
                        const rect = item.getBoundingClientRect();
                        const middle = rect.left + (rect.width / 2);
                        
                        if (e.clientX < middle) {
                            item.classList.add('drag-over-left');
                        } else {
                            item.classList.add('drag-over-right');
                        }
                    }
                });
                
                item.addEventListener('dragleave', () => {
                    item.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
                });
                
                item.addEventListener('drop', (e) => {
                    e.preventDefault();
                    item.classList.remove('drag-over', 'drag-over-left', 'drag-over-right');
                    dropX = e.clientX; // Store drop coordinates
                    handleDrop(item);
                });
                
                // Touch events for mobile
                item.addEventListener('touchstart', (e) => {
                    if (e.touches.length === 1) {
                        touchStartX = e.touches[0].clientX;
                        touchStartY = e.touches[0].clientY;
                        isTouching = true;
                        
                        // Long press to start drag on mobile
                        setTimeout(() => {
                            if (isTouching) {
                                draggedElement = item;
                                item.classList.add('dragging');
                                navigator.vibrate && navigator.vibrate(50); // Haptic feedback
                            }
                        }, 500);
                    }
                }, { passive: true });
                
                item.addEventListener('touchmove', (e) => {
                    if (draggedElement === item) {
                        e.preventDefault();
                        const touch = e.touches[0];
                        const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
                        const targetItem = elementBelow?.closest('.image-item');
                        
                        // Remove previous drag-over classes
                        items.forEach(i => i.classList.remove('drag-over'));
                        
                        if (targetItem && targetItem !== item) {
                            targetItem.classList.add('drag-over');
                        }
                    }
                }, { passive: false });
                
                item.addEventListener('touchend', (e) => {
                    isTouching = false;
                    
                    if (draggedElement === item) {
                        const touch = e.changedTouches[0];
                        const elementBelow = document.elementFromPoint(touch.clientX, touch.clientY);
                        const targetItem = elementBelow?.closest('.image-item');
                        
                        if (targetItem && targetItem !== item) {
                            handleDrop(targetItem);
                        }
                        
                        item.classList.remove('dragging');
                        items.forEach(i => i.classList.remove('drag-over'));
                        draggedElement = null;
                    }
                }, { passive: true });
            });
        }
        
        function handleDrop(targetItem) {
            if (draggedElement && draggedElement !== targetItem) {
                const targetRect = targetItem.getBoundingClientRect();
                const targetMiddle = targetRect.left + (targetRect.width / 2);
                
                // Determine if we should insert before or after based on drop position
                const insertBefore = dropX < targetMiddle;
                
                if (insertBefore) {
                    // Insert before target item
                    targetItem.parentNode.insertBefore(draggedElement, targetItem);
                } else {
                    // Insert after target item
                    targetItem.parentNode.insertBefore(draggedElement, targetItem.nextSibling);
                }
                
                // Update order
                updatePlaylistOrder();
            }
        }
        
        function updatePlaylistOrder() {
            const items = document.querySelectorAll('.image-item');
            const newOrder = Array.from(items).map(item => item.dataset.image);
            
            fetch(`/api/playlist/${encodeURIComponent(currentFolder)}/order`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({order: newOrder})
            }).then(() => {
                loadFolder(currentFolder);
            });
        }
        
        function handleFiles(files) {
            const formData = new FormData();
            let fileCount = 0;
            
            for (const file of files) {
                if (file.type.startsWith('image/')) {
                    formData.append('files', file);
                    fileCount++;
                }
            }
            
            if (fileCount === 0) return;
            
            // Show progress
            const progressBar = document.getElementById('uploadProgress');
            const progressFill = document.getElementById('uploadProgressFill');
            progressBar.style.display = 'block';
            
            const xhr = new XMLHttpRequest();
            
            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    progressFill.style.width = percent + '%';
                    progressFill.textContent = percent + '%';
                }
            });
            
            xhr.addEventListener('load', () => {
                progressBar.style.display = 'none';
                if (xhr.status === 200) {
                    const response = JSON.parse(xhr.responseText);
                    showNotification('Upload Complete', `${response.uploaded.length} images uploaded`, 'success');
                } else {
                    showNotification('Upload Failed', 'Error uploading images', 'error');
                }
                loadFolder(currentFolder);
            });
            
            xhr.open('POST', `/api/upload/${encodeURIComponent(currentFolder)}`);
            xhr.send(formData);
        }
        
        function startStatusPolling() {
            // Poll every 2 seconds
            statusTimer = setInterval(updateStatus, 2000);
            updateStatus();
        }
        
        function updateStatus() {
            fetch('/api/slideshow/status')
                .then(r => r.json())
                .then(status => {
                    const indicator = document.getElementById('statusIndicator');
                    const statusText = document.getElementById('statusText');
                    const playBtn = document.getElementById('playBtn');
                    const stopBtn = document.getElementById('stopBtn');
                    const progress = document.getElementById('playlistProgress');
                    const nextChange = document.getElementById('nextChange');
                    const nowPlaying = document.getElementById('nowPlaying');
                    const loopStatus = document.getElementById('loopStatus');
                    const shuffleStatus = document.getElementById('shuffleStatus');
                    
                    if (status.running) {
                        indicator.classList.add('active');
                        statusText.textContent = 'Playing';
                        playBtn.style.display = 'none';
                        stopBtn.style.display = 'inline-block';
                        nowPlaying.classList.add('active');
                        
                        // Update progress  
                        if (status.total_images > 0) {
                            if (status.loop_enabled) {
                                progress.textContent = `${status.current_index}/${status.total_images} (Loop ${status.loop_count + 1})`;
                            } else {
                                progress.textContent = `${status.current_index}/${status.total_images}`;
                            }
                        } else {
                            progress.textContent = '-';
                        }
                        
                        // Update countdown
                        if (status.next_change) {
                            const remaining = Math.max(0, Math.floor(status.next_change - Date.now() / 1000));
                            if (remaining > 0) {
                                const minutes = Math.floor(remaining / 60);
                                const seconds = remaining % 60;
                                nextChange.textContent = `${minutes}:${seconds.toString().padStart(2, '0')}`;
                            } else {
                                nextChange.textContent = '0:00';
                            }
                        } else {
                            nextChange.textContent = '-';
                        }
                        
                        // Update folder name
                        const playingFolderEl = document.getElementById('playingFolder');
                        if (playingFolderEl) {
                            playingFolderEl.textContent = status.current_folder || 'Root';
                        }
                        
                        // Update global status regardless of current folder
                        if (loopStatus) {
                            loopStatus.textContent = status.loop_enabled ? 'On' : 'Off';
                        }
                        if (shuffleStatus) {
                            shuffleStatus.textContent = status.shuffle_enabled ? 'On' : 'Off';
                        }
                        
                        // Update thumbnails
                        if (status.current_image) {
                            const currentPath = status.current_folder ? `${status.current_folder}/${status.current_image}` : status.current_image;
                            document.getElementById('currentThumbnail').src = `/api/thumbnail/${encodeURIComponent(currentPath)}?w=200&q=85`;
                            document.getElementById('currentImageName').textContent = status.current_image;
                            document.getElementById('currentThumbnail').onclick = () => {
                                const imagePath = status.current_folder ? `${status.current_folder}/${status.current_image}` : status.current_image;
                                pushImageDirect(imagePath);
                            };
                        }
                        
                        if (status.next_image) {
                            const nextPath = status.current_folder ? `${status.current_folder}/${status.next_image}` : status.next_image;
                            document.getElementById('nextThumbnail').src = `/api/thumbnail/${encodeURIComponent(nextPath)}?w=200&q=85`;
                            document.getElementById('nextImageName').textContent = status.next_image;
                            document.getElementById('nextThumbnail').onclick = () => {
                                const imagePath = status.current_folder ? `${status.current_folder}/${status.next_image}` : status.next_image;
                                pushImageDirect(imagePath);
                            };
                        } else {
                            document.getElementById('nextThumbnail').src = '';
                            document.getElementById('nextImageName').textContent = 'End of playlist';
                        }
                    } else {
                        indicator.classList.remove('active');
                        statusText.textContent = 'Idle';
                        playBtn.style.display = 'inline-block';
                        stopBtn.style.display = 'none';
                        progress.textContent = '-';
                        nextChange.textContent = '-';
                        nowPlaying.classList.remove('active');
                        
                        // Reset global status
                        if (loopStatus) {
                            loopStatus.textContent = 'Off';
                        }
                        if (shuffleStatus) {
                            shuffleStatus.textContent = 'Off';
                        }
                    }
                });
        }
        
        function toggleSlideshow() {
            showNotification('Starting Slideshow', `Playing from ${currentFolder || 'Root'}...`, 'info');
            fetch(`/api/slideshow/start/${encodeURIComponent(currentFolder)}`, {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showNotification('Slideshow Started', `Now playing from ${currentFolder || 'Root'}`, 'success');
                    updateStatus();
                } else {
                    showNotification('Start Failed', 'Unable to start slideshow', 'error');
                }
            });
        }
        
        function stopSlideshow() {
            fetch('/api/slideshow/stop', {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showNotification('Slideshow Stopped', 'Playback has been stopped', 'success');
                    updateStatus();
                }
            });
        }
        
        function trackPushProgress(jobId, imageName) {
            const checkStatus = () => {
                fetch(`/api/push/status/${jobId}`)
                    .then(r => r.json())
                    .then(data => {
                        if (data.error) {
                            showNotification('Push Error', 'Job not found', 'error');
                            return;
                        }
                        
                        const { status, progress, message } = data;
                        
                        if (status === 'completed') {
                            showNotification('Push Complete!', `${imageName} sent successfully`, 'success');
                        } else if (status === 'failed') {
                            showNotification('Push Failed', data.error || message, 'error');
                        } else {
                            // Still in progress
                            showNotification('Pushing Image', message, 'progress', progress);
                            setTimeout(checkStatus, 500); // Poll every 500ms
                        }
                    })
                    .catch(err => {
                        showNotification('Push Error', 'Lost connection to server', 'error');
                    });
            };
            
            checkStatus();
        }
        
        function pushImage(imageName) {
            fetch(`/api/push/${encodeURIComponent(currentFolder + '/' + imageName)}`, {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                if (data.success && data.job_id) {
                    trackPushProgress(data.job_id, imageName);
                } else {
                    showNotification('Push Failed', data.error || 'Unable to start push', 'error');
                }
            })
            .catch(err => {
                showNotification('Push Error', 'Failed to communicate with server', 'error');
            });
        }
        
        function pushImageDirect(imagePath) {
            const imageName = imagePath.split('/').pop();
            fetch(`/api/push/${encodeURIComponent(imagePath)}`, {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                if (data.success && data.job_id) {
                    trackPushProgress(data.job_id, imageName);
                } else {
                    showNotification('Push Failed', data.error || 'Unable to start push', 'error');
                }
            })
            .catch(err => {
                showNotification('Push Error', 'Failed to communicate with server', 'error');
            });
        }
        
        function pushNextNow() {
            showNotification('Skipping to Next', 'Pushing next image...', 'info');
            fetch('/api/slideshow/next', {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showNotification('Next Image Pushed!', data.pushed || 'Skipped to next image', 'success');
                    setTimeout(updateStatus, 500);
                } else {
                    showNotification('Skip Failed', data.error || 'Unable to skip', 'error');
                }
            })
            .catch(err => {
                showNotification('Skip Error', 'Failed to skip to next', 'error');
            });
        }
        
        function deleteImage(imageName) {
            if (confirm(`Delete ${imageName}?`)) {
                fetch(`/api/image/${encodeURIComponent(currentFolder + '/' + imageName)}`, {
                    method: 'DELETE'
                }).then(() => {
                    loadFolder(currentFolder);
                });
            }
        }
        
        function refreshThumbnails(folderPath) {
            showNotification('Refreshing thumbnails...', 'progress');
            
            const url = folderPath 
                ? `/api/thumbnails/refresh/${encodeURIComponent(folderPath)}`
                : '/api/thumbnails/refresh';
                
            fetch(url, {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showNotification(`✅ Regenerated ${data.regenerated} thumbnails`, 'success');
                    // Reload current folder to show new thumbnails
                    loadFolder(currentFolder);
                } else {
                    showNotification('Failed to refresh thumbnails', 'error');
                }
            })
            .catch(error => {
                showNotification('Error refreshing thumbnails', 'error');
                console.error('Refresh error:', error);
            });
        }
        
        function startRename(event, folderPath) {
            event.stopPropagation();
            event.preventDefault();
            console.log('Renaming folder:', folderPath);
            const folderItem = document.querySelector(`[data-folder-path="${folderPath}"] .folder-name`);
            const currentName = folderItem.textContent;
            
            const input = document.createElement('input');
            input.type = 'text';
            input.value = currentName;
            input.className = 'folder-rename-input';
            input.style.cssText = 'background: white; border: 1px solid #3b82f6; border-radius: 4px; padding: 2px 6px; font-size: 14px; font-weight: 500; width: 120px;';
            
            folderItem.replaceWith(input);
            input.focus();
            input.select();
            
            // Prevent folder selection when clicking in input
            input.addEventListener('click', (e) => {
                e.stopPropagation();
            });
            
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    const newName = input.value.trim();
                    if (newName && newName !== currentName) {
                        fetch(`/api/folder/${encodeURIComponent(folderPath)}/rename`, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({new_name: newName})
                        }).then(response => response.json()).then(data => {
                            if (data.success) {
                                loadFolderTree();
                            } else {
                                alert('Failed to rename folder: ' + (data.error || 'Unknown error'));
                            }
                        });
                    } else {
                        folderItem.textContent = currentName;
                        input.replaceWith(folderItem);
                    }
                }
                if (e.key === 'Escape') {
                    folderItem.textContent = currentName;
                    input.replaceWith(folderItem);
                }
            });
        }
        
        function deleteFolder(folderPath) {
            if (confirm(`Delete folder "${folderPath}" and all its contents?`)) {
                fetch(`/api/folder/${encodeURIComponent(folderPath)}`, {
                    method: 'DELETE'
                }).then(response => {
                    if (response.ok) {
                        // Navigate to parent folder
                        const parentPath = folderPath.split('/').slice(0, -1).join('/');
                        loadFolder(parentPath);
                    } else {
                        alert('Failed to delete folder');
                    }
                });
            }
        }
        
        let draggedFolder = null;
        
        function startFolderDrag(event) {
            draggedFolder = event.target.closest('.folder-item');
            draggedFolder.classList.add('dragging');
            event.dataTransfer.effectAllowed = 'move';
        }
        
        function handleFolderDragOver(event) {
            event.preventDefault();
            if (draggedFolder && draggedFolder !== event.target.closest('.folder-item')) {
                event.target.closest('.folder-item').classList.add('drag-over');
            }
        }
        
        function handleFolderDrop(event) {
            event.preventDefault();
            const targetFolder = event.target.closest('.folder-item');
            targetFolder.classList.remove('drag-over');
            
            if (draggedFolder && draggedFolder !== targetFolder) {
                const sourcePath = draggedFolder.getAttribute('data-folder-path');
                const targetPath = targetFolder.getAttribute('data-folder-path');
                
                if (sourcePath && sourcePath !== targetPath) {
                    moveFolder(sourcePath, targetPath);
                }
            }
            
            draggedFolder.classList.remove('dragging');
            draggedFolder = null;
        }
        
        function moveFolder(sourcePath, targetPath) {
            const sourceName = sourcePath.split('/').pop();
            const newPath = targetPath ? `${targetPath}/${sourceName}` : sourceName;
            
            fetch(`/api/folder/move`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    source: sourcePath,
                    target: newPath
                })
            }).then(response => {
                if (response.ok) {
                    loadFolderTree();
                    loadFolder(currentFolder);
                } else {
                    alert('Failed to move folder');
                }
            });
        }
        
        function showNewFolderModal() {
            document.getElementById('newFolderModal').classList.add('show');
        }
        
        function createNewFolder() {
            const name = document.getElementById('newFolderName').value.trim();
            if (!name) return;
            
            const path = currentFolder ? `${currentFolder}/${name}` : name;
            
            fetch('/api/folder', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path})
            }).then(() => {
                closeModal('newFolderModal');
                document.getElementById('newFolderName').value = '';
                loadFolderTree();
                loadFolder(path);
            });
        }
        
        function showPlaylistSettings() {
            fetch(`/api/playlist/${encodeURIComponent(currentFolder)}`)
                .then(r => r.json())
                .then(data => {
                    const settings = data.playlist.settings;
                    document.getElementById('intervalInput').value = settings.interval;
                    document.getElementById('loopToggle').classList.toggle('active', settings.loop);
                    document.getElementById('shuffleToggle').classList.toggle('active', settings.shuffle);
                    document.getElementById('descriptionInput').value = data.playlist.description || '';
                    document.getElementById('settingsModal').classList.add('show');
                });
        }
        
        function savePlaylistSettings() {
            const settings = {
                interval: parseInt(document.getElementById('intervalInput').value),
                loop: document.getElementById('loopToggle').classList.contains('active'),
                shuffle: document.getElementById('shuffleToggle').classList.contains('active')
            };
            
            const description = document.getElementById('descriptionInput').value;
            
            fetch(`/api/playlist/${encodeURIComponent(currentFolder)}/settings`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({settings, description})
            }).then(() => {
                closeModal('settingsModal');
                loadFolder(currentFolder);
            });
        }
        
        function showMoveModal(imageName) {
            selectedImage = imageName;
            
            // Load folders for destination
            fetch('/api/folders')
                .then(r => r.json())
                .then(data => {
                    const select = document.getElementById('destinationFolder');
                    select.innerHTML = '<option value="">Root</option>';
                    
                    function addFolders(items, prefix = '') {
                        for (const item of items) {
                            if (item.path !== currentFolder) {
                                select.innerHTML += `<option value="${item.path}">${prefix}${item.name}</option>`;
                            }
                            if (item.children) {
                                addFolders(item.children, prefix + '  ');
                            }
                        }
                    }
                    
                    addFolders(data.tree);
                    document.getElementById('moveModal').classList.add('show');
                });
        }
        
        function confirmMove() {
            const destination = document.getElementById('destinationFolder').value;
            
            fetch('/api/move', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    image: selectedImage,
                    from: currentFolder,
                    to: destination
                })
            }).then(() => {
                closeModal('moveModal');
                loadFolder(currentFolder);
            });
        }
        
        function toggleSwitch(element) {
            element.classList.toggle('active');
        }
        
        function closeModal(modalId) {
            document.getElementById(modalId).classList.remove('show');
        }
        
        function refreshStatus() {
            loadFolder(currentFolder);
            updateStatus();
        }
    </script>
</body>
</html>''')

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

@app.route('/api/scheduler/jobs')
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
    app.run(host='0.0.0.0', port=5000, debug=False)