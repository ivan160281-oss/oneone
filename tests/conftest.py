import os
import tempfile

# The engineering part opens its database when first imported, so its data
# folder has to be set before any test imports the app.
os.environ.setdefault("WIFIGPS_DATA_DIR", tempfile.mkdtemp(prefix="altgeo-eng-"))
os.environ["ALTGEO_ENG_JOBS"] = "0"
