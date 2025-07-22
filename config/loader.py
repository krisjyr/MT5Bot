import json
import os

def load_settings(filepath: str) -> dict:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Settings file not found at {filepath}")
    
    with open(filepath, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON format in settings file.")
