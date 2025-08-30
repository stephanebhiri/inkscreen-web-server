class AppState:
    def __init__(self):
        self.push_jobs = {}
        self.esp32_stats = {
            "battery": -1,
            "rssi": 0,
            "heap": 0,
            "uptime": 0,
            "last_seen": None
        }
        self.current_folder = ""
        self.current_image = ""
        self.manual_override = False
        self.slideshow_state = {
            'job_id': None,
            'folder_path': '',
            'current_image_name': None,
            'loop_count': 0,
            'images': [],
            'settings': {}
        }
        self.thumbnail_call_count = 0
        self.cleanup_stats = {
            'last_run': None,
            'orphaned_cleaned': 0,
            'dynamic_cleaned': 0
        }
