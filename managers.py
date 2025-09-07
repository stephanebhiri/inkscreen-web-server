import os
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime
from logger_config import get_logger

# Module logger
logger = get_logger('managers')

class PushJob:
    def __init__(self, job_id, image_name, image_path):
        self.job_id = job_id
        self.image_name = image_name
        self.image_path = image_path
        self.status = 'starting'
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

class PlaylistManager:
    def __init__(self, base_folder):
        self.base_folder = base_folder

    def get_playlist_file(self, folder_path):
        return os.path.join(folder_path, '.playlist.json')
    
    def load_playlist(self, folder_path):
        playlist_file = self.get_playlist_file(folder_path)
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
                'active': False,
                'recursive': False
            },
            'tags': [],
            'description': '',
            'stats': {
                'play_count': 0,
                'last_played': None,
                'total_duration': 0
            }
        }
    
    def save_playlist(self, folder_path, playlist_data):
        playlist_data['modified'] = datetime.now().isoformat()
        playlist_file = self.get_playlist_file(folder_path)
        with open(playlist_file, 'w') as f:
            json.dump(playlist_data, f, indent=2)
    
    def update_order(self, folder_path, new_order=None):
        playlist = self.load_playlist(folder_path)
        
        current_images = []
        for f in os.listdir(folder_path):
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                current_images.append(f)
        
        if new_order:
            playlist['order'] = [img for img in new_order if img in current_images]
            for img in current_images:
                if img not in playlist['order']:
                    playlist['order'].append(img)
        else:
            existing_order = playlist.get('order', [])
            new_order = []
            
            for img in existing_order:
                if img in current_images:
                    new_order.append(img)
            
            for img in current_images:
                if img not in new_order:
                    new_order.append(img)
            
            playlist['order'] = new_order
        
        self.save_playlist(folder_path, playlist)
        return playlist

class FolderManager:
    def __init__(self, base_folder, thumbnails_folder):
        self.base_folder = base_folder
        self.thumbnails_folder = thumbnails_folder
        self.playlist_manager = PlaylistManager(base_folder)

    def ensure_base_folder(self):
        os.makedirs(self.base_folder, exist_ok=True)
        os.makedirs(self.thumbnails_folder, exist_ok=True)
    
    def create_folder(self, path):
        candidate = path.strip('/')
        # Basic validation: disallow reserved/special characters in any segment
        invalid_chars = set('/\\:*?"<>|')
        for segment in [p for p in candidate.split('/') if p]:
            if any(ch in invalid_chars for ch in segment):
                return False
        # Normalize and ensure within base_folder (prevent traversal)
        full_path = os.path.normpath(os.path.join(self.base_folder, candidate))
        base_abs = os.path.abspath(self.base_folder)
        if not os.path.abspath(full_path).startswith(base_abs):
            return False
        os.makedirs(full_path, exist_ok=True)
        self.playlist_manager.update_order(full_path)
        return True
    
    def get_folder_tree(self):
        self.ensure_base_folder()
        
        root_images = [f for f in os.listdir(self.base_folder) 
                      if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
        root_playlist = self.playlist_manager.load_playlist(self.base_folder)
        
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
                        playlist = self.playlist_manager.load_playlist(item_path)
                        items.append({
                            'name': item,
                            'path': item_rel_path,
                            'type': 'folder',
                            'children': children,
                            'image_count': len([f for f in os.listdir(item_path) 
                                              if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]),
                            'active': playlist.get('settings', {}).get('active', False)
                        })
            except PermissionError:
                pass
            
            return items
        
        tree[0]['children'] = walk_dir(self.base_folder)
        return tree
    
    def move_image(self, image_path, from_folder, to_folder):
        from_path = os.path.join(self.base_folder, from_folder.strip('/'), image_path)
        to_path = os.path.join(self.base_folder, to_folder.strip('/'), image_path)
        
        if os.path.exists(from_path):
            os.makedirs(os.path.dirname(to_path), exist_ok=True)
            shutil.move(from_path, to_path)
            
            self.playlist_manager.update_order(os.path.dirname(from_path))
            self.playlist_manager.update_order(os.path.dirname(to_path))
            
            # Keep thumbnail naming consistent with upload/delete conventions
            base_name_no_ext = os.path.splitext(image_path)[0]
            from_prefix = from_folder.strip('/').replace('/', '_')
            to_prefix = to_folder.strip('/').replace('/', '_')
            thumb_from_name = (
                f"{from_prefix}_{base_name_no_ext}_thumb.jpg" if from_prefix else f"{base_name_no_ext}_thumb.jpg"
            )
            thumb_to_name = (
                f"{to_prefix}_{base_name_no_ext}_thumb.jpg" if to_prefix else f"{base_name_no_ext}_thumb.jpg"
            )
            os.makedirs(self.thumbnails_folder, exist_ok=True)
            thumb_from = os.path.join(self.thumbnails_folder, thumb_from_name)
            thumb_to = os.path.join(self.thumbnails_folder, thumb_to_name)
            if os.path.exists(thumb_from):
                # If destination thumbnail exists for any reason, replace it
                try:
                    shutil.move(thumb_from, thumb_to)
                except Exception:
                    try:
                        os.replace(thumb_from, thumb_to)
                    except Exception:
                        pass
            
            return True
        return False

class SlideshowManager:
    def __init__(self, scheduler, base_folder, app_state):
        self.scheduler = scheduler
        self.base_folder = base_folder
        self.playlist_manager = PlaylistManager(base_folder)
        self.app_state = app_state

    def get_status(self):
        job_id = self.app_state.slideshow_state.get('job_id')
        job = self.scheduler.get_job(job_id) if job_id else None
        
        if job and job_id:
            images = self.app_state.slideshow_state['images']
            current_image_name = self.app_state.slideshow_state['current_image_name']
            settings = self.app_state.slideshow_state['settings']
            
            current_image = current_image_name or ''
            
            if current_image_name and current_image_name in images and images:
                current_index = images.index(current_image_name)
                next_index = current_index + 1
                
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
                next_image = images[0] if images else ''
                displayed_index = 0
            
            return {
                'running': True,
                'current_folder': self.app_state.slideshow_state['folder_path'].replace(self.base_folder, '').strip('/'),
                'current_image': current_image,
                'next_image': next_image,
                'current_index': displayed_index + 1,
                'total_images': len(images),
                'loop_count': self.app_state.slideshow_state['loop_count'],
                'loop_enabled': settings.get('loop', True),
                'shuffle_enabled': settings.get('shuffle', False),
                'interval': settings.get('interval', 300),
                'start_time': datetime.now().isoformat(),
                'next_change': job.next_run_time.timestamp() if job.next_run_time else None
            }
        
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
    
    def push_next_image(self, manual_trigger=False):
        try:
            images = self.app_state.slideshow_state['images']
            current_image_name = self.app_state.slideshow_state['current_image_name']
            settings = self.app_state.slideshow_state['settings']
            folder_path = self.app_state.slideshow_state['folder_path']
            
            if not images:
                self.stop()
                return
            
            if current_image_name and current_image_name in images:
                current_index = images.index(current_image_name)
                next_index = current_index + 1
            else:
                next_index = 0
            
            if next_index >= len(images):
                if settings.get('loop', True):
                    next_index = 0
                    self.app_state.slideshow_state['loop_count'] += 1
                    
                    if settings.get('shuffle', False):
                        import random
                        random.shuffle(self.app_state.slideshow_state['images'])
                        images = self.app_state.slideshow_state['images']
                        next_index = 0
                else:
                    self.stop()
                    return
            
            image_file = images[next_index]
            
            # Update state for HTTP polling architecture
            # Extract relative folder path from full folder path
            rel_folder = os.path.relpath(folder_path, self.base_folder)
            
            # Set the current image for HTTP polling
            self.app_state.current_folder = rel_folder
            self.app_state.current_image = image_file
            self.app_state.slideshow_state['current_image_name'] = image_file
            self.app_state.manual_override = False
            
            logger.info(f"Advanced to next image: {rel_folder}/{image_file}")
            
            # If manually triggered, reschedule the next automatic change
            if manual_trigger and self.app_state.slideshow_state.get('job_id'):
                try:
                    # Remove existing job and create a new one
                    self.scheduler.remove_job(self.app_state.slideshow_state['job_id'])
                    interval = settings.get('interval', 300)
                    job = self.scheduler.add_job(
                        self.push_next_image,
                        'interval',
                        seconds=interval,
                        id='slideshow_job',
                        replace_existing=True,
                        max_instances=1
                    )
                    self.app_state.slideshow_state['job_id'] = job.id
                    logger.info(f"Rescheduled next change in {interval} seconds")
                except Exception as e:
                    logger.error(f"Error rescheduling job: {e}")
            
        except Exception as e:
            logger.error(f"Error in push_next_image: {e}")
    
    def start(self, folder_path):
        self.stop()
        
        playlist = self.playlist_manager.load_playlist(folder_path)
        settings = playlist.get('settings', {})
        interval = settings.get('interval', 300)
        
        images = playlist.get("order", [])
        
        # Always rescan if recursive mode is enabled
        if settings.get("recursive", False):
            images = []  # Clear existing order for recursive scan
            # Recursive scan
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    if file.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        rel_path = os.path.relpath(os.path.join(root, file), folder_path)
                        images.append(rel_path)
        elif not images:
            # Normal scan only if no order exists
            images = [f for f in os.listdir(folder_path)
                     if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
        
        if not images:
            return False
        
        if settings.get('shuffle', False):
            import random
            images = images.copy()
            random.shuffle(images)
        
        self.app_state.slideshow_state = {
            'job_id': None,
            'folder_path': folder_path,
            'current_image_name': None,
            'loop_count': 0,
            'images': images,
            'settings': settings
        }
        
        # Set the first image
        try:
            if images:
                self.push_next_image()
            else:
                logger.warning("No images to display in slideshow")
        except Exception as e:
            logger.warning(f"Error pushing first image: {e}")
        
        try:
            job = self.scheduler.add_job(
                self.push_next_image,
                'interval',
                seconds=interval,
                id='slideshow_job',
                replace_existing=True,
                max_instances=1
            )
            self.app_state.slideshow_state['job_id'] = job.id
        except Exception as e:
            logger.error(f"Error creating job: {e}", exc_info=True)
            return False
        
        playlist['stats']['play_count'] = playlist['stats'].get('play_count', 0) + 1
        playlist['stats']['last_played'] = datetime.now().isoformat()
        self.playlist_manager.save_playlist(folder_path, playlist)
        
        return True
    
    def stop(self):
        if self.app_state.slideshow_state.get('job_id'):
            try:
                self.scheduler.remove_job(self.app_state.slideshow_state['job_id'])
            except:
                pass
        
        self.app_state.slideshow_state = {
            'job_id': None,
            'folder_path': '',
            'current_image_name': None,
            'loop_count': 0,
            'images': [],
            'settings': {}
        }
