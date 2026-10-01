"""Engineering part of ALTGEO (admin only), mounted at /eng by app.main.

The Flask app is imported lazily: importing it opens the engineering
database (WIFIGPS_DATA_DIR) and may start the background jobs.
"""
