"""
Importing anything from `src` runs this file first, which imports
src.config — the single common place .env is loaded and configuration is
read from. See src/config.py for details. Nothing else needs to be added
here; new entrypoints get .env loading for free just by importing `src`.
"""

from src import config 

