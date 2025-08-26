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
