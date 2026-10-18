import sys
import os

# Ensure project root is in sys.path so 'app' can be imported by Vercel serverless function
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from app.main import create_app

app = create_app()
