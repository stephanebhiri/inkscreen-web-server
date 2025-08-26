#!/usr/bin/env python3
"""
Unit tests for refactored inkscreen-web application
"""

import unittest
import os
import sys
import json
import tempfile
import shutil
from unittest.mock import Mock, patch, MagicMock

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state import AppState
from managers import PlaylistManager, FolderManager, SlideshowManager, PushJob


class TestAppState(unittest.TestCase):
    """Test AppState initialization and attributes"""
    
    def setUp(self):
        self.state = AppState()
    
    def test_initial_state(self):
        """Test initial state values"""
        self.assertEqual(self.state.push_jobs, {})
        self.assertEqual(self.state.current_folder, "")
        self.assertEqual(self.state.current_image, "")
        self.assertFalse(self.state.manual_override)
        self.assertEqual(self.state.thumbnail_call_count, 0)
        
    def test_esp32_stats_initialization(self):
        """Test ESP32 stats initialization"""
        self.assertEqual(self.state.esp32_stats["battery"], -1)
        self.assertEqual(self.state.esp32_stats["rssi"], 0)
        self.assertEqual(self.state.esp32_stats["heap"], 0)
        self.assertEqual(self.state.esp32_stats["uptime"], 0)
        self.assertIsNone(self.state.esp32_stats["last_seen"])
        
    def test_slideshow_state_initialization(self):
        """Test slideshow state initialization"""
        self.assertIsNone(self.state.slideshow_state['job_id'])
        self.assertEqual(self.state.slideshow_state['folder_path'], '')
        self.assertIsNone(self.state.slideshow_state['current_image_name'])
        self.assertEqual(self.state.slideshow_state['loop_count'], 0)
        self.assertEqual(self.state.slideshow_state['images'], [])
        self.assertEqual(self.state.slideshow_state['settings'], {})


class TestPushJob(unittest.TestCase):
    """Test PushJob class"""
    
    def test_push_job_initialization(self):
        """Test PushJob initialization"""
        job = PushJob('test-id', 'test.jpg', '/path/to/test.jpg')
        
        self.assertEqual(job.job_id, 'test-id')
        self.assertEqual(job.image_name, 'test.jpg')
        self.assertEqual(job.image_path, '/path/to/test.jpg')
        self.assertEqual(job.status, 'starting')
        self.assertEqual(job.progress, 0)
        self.assertEqual(job.message, 'Initializing...')
        self.assertIsNone(job.error)
        
    def test_push_job_update(self):
        """Test PushJob update method"""
        job = PushJob('test-id', 'test.jpg', '/path/to/test.jpg')
        
        job.update('processing', 50, 'Processing image...')
        self.assertEqual(job.status, 'processing')
        self.assertEqual(job.progress, 50)
        self.assertEqual(job.message, 'Processing image...')
        
    def test_push_job_to_dict(self):
        """Test PushJob to_dict method"""
        job = PushJob('test-id', 'test.jpg', '/path/to/test.jpg')
        job.update('completed', 100, 'Done')
        
        result = job.to_dict()
        self.assertEqual(result['job_id'], 'test-id')
        self.assertEqual(result['image_name'], 'test.jpg')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['progress'], 100)
        self.assertEqual(result['message'], 'Done')
        self.assertIsNone(result['error'])
        self.assertIn('elapsed_time', result)


class TestPlaylistManager(unittest.TestCase):
    """Test PlaylistManager class"""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.manager = PlaylistManager(self.temp_dir)
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        
    def test_load_empty_playlist(self):
        """Test loading playlist from empty folder"""
        playlist = self.manager.load_playlist(self.temp_dir)
        
        self.assertIn('images', playlist)
        self.assertIn('settings', playlist)
        self.assertIn('stats', playlist)
        self.assertEqual(playlist['images'], [])
        
    def test_save_and_load_playlist(self):
        """Test saving and loading playlist"""
        test_playlist = {
            'images': ['img1.jpg', 'img2.jpg'],
            'settings': {'interval': 300, 'loop': True},
            'stats': {'play_count': 5},
            'description': 'Test playlist'
        }
        
        self.manager.save_playlist(self.temp_dir, test_playlist)
        loaded = self.manager.load_playlist(self.temp_dir)
        
        self.assertEqual(loaded['images'], test_playlist['images'])
        self.assertEqual(loaded['settings'], test_playlist['settings'])
        self.assertEqual(loaded['description'], test_playlist['description'])


class TestFolderManager(unittest.TestCase):
    """Test FolderManager class"""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.thumb_dir = tempfile.mkdtemp()
        self.manager = FolderManager(self.temp_dir, self.thumb_dir)
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        shutil.rmtree(self.thumb_dir)
        
    def test_ensure_base_folder(self):
        """Test base folder creation"""
        new_base = os.path.join(self.temp_dir, 'new_folder')
        manager = FolderManager(new_base, self.thumb_dir)
        manager.ensure_base_folder()
        
        self.assertTrue(os.path.exists(new_base))
        
    def test_create_folder(self):
        """Test folder creation"""
        result = self.manager.create_folder('test_folder')
        
        self.assertTrue(result)
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir, 'test_folder')))
        
    def test_get_folder_tree_empty(self):
        """Test getting folder tree from empty directory"""
        tree = self.manager.get_folder_tree()
        
        self.assertIsInstance(tree, list)
        self.assertEqual(len(tree), 0)


class TestSlideshowManager(unittest.TestCase):
    """Test SlideshowManager class"""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.scheduler = Mock()
        self.app_state = AppState()
        self.manager = SlideshowManager(self.scheduler, self.temp_dir, self.app_state)
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        
    def test_initial_status(self):
        """Test initial slideshow status"""
        status = self.manager.get_status()
        
        self.assertFalse(status['running'])
        self.assertEqual(status['current_folder'], '')
        self.assertEqual(status['current_image'], '')
        self.assertEqual(status['total_images'], 0)
        
    def test_stop_slideshow(self):
        """Test stopping slideshow"""
        # Set up a fake running slideshow
        self.app_state.slideshow_state['job_id'] = 'test-job'
        self.scheduler.remove_job = Mock()
        
        self.manager.stop()
        
        self.scheduler.remove_job.assert_called_once_with('test-job')
        self.assertIsNone(self.app_state.slideshow_state['job_id'])
        self.assertEqual(self.app_state.slideshow_state['images'], [])
        
    @patch('managers.os.listdir')
    def test_start_slideshow_no_images(self, mock_listdir):
        """Test starting slideshow with no images"""
        mock_listdir.return_value = []
        
        result = self.manager.start(self.temp_dir)
        
        self.assertFalse(result)
        
    @patch('managers.os.listdir')
    @patch('managers.subprocess.run')
    def test_start_slideshow_with_images(self, mock_run, mock_listdir):
        """Test starting slideshow with images"""
        mock_listdir.return_value = ['test1.jpg', 'test2.png', 'readme.txt']
        self.scheduler.add_job = Mock(return_value=Mock(id='new-job'))
        
        result = self.manager.start(self.temp_dir)
        
        self.assertTrue(result)
        self.assertEqual(len(self.app_state.slideshow_state['images']), 2)
        self.assertEqual(self.app_state.slideshow_state['job_id'], 'new-job')


class TestIntegration(unittest.TestCase):
    """Integration tests for components working together"""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.thumb_dir = tempfile.mkdtemp()
        self.app_state = AppState()
        self.scheduler = Mock()
        
    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        shutil.rmtree(self.thumb_dir)
        
    def test_state_sharing_between_managers(self):
        """Test that state is properly shared between managers"""
        slideshow = SlideshowManager(self.scheduler, self.temp_dir, self.app_state)
        
        # Simulate slideshow updating state
        self.app_state.slideshow_state['current_image_name'] = 'test.jpg'
        self.app_state.slideshow_state['folder_path'] = self.temp_dir
        
        # State should be accessible from app_state
        self.assertEqual(self.app_state.slideshow_state['current_image_name'], 'test.jpg')
        
    def test_push_job_lifecycle(self):
        """Test push job lifecycle in app_state"""
        job = PushJob('job-1', 'image.jpg', '/path/to/image.jpg')
        
        # Add to state
        self.app_state.push_jobs['job-1'] = job
        
        # Update job
        job.update('processing', 50, 'Processing...')
        
        # Verify job in state
        self.assertIn('job-1', self.app_state.push_jobs)
        self.assertEqual(self.app_state.push_jobs['job-1'].status, 'processing')
        
        # Remove job
        del self.app_state.push_jobs['job-1']
        self.assertNotIn('job-1', self.app_state.push_jobs)


if __name__ == '__main__':
    unittest.main(verbosity=2)