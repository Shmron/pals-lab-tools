"""
Shared helper for staging an uploaded CSV / zipped-shapefile to disk in the
jobs directory, used by both the manual Extract page and Shmron AI's
submit_extraction_job tool so they save files identically.
"""
import os
import uuid
import zipfile

JOBS_DIR = '/home/USER/PRECISE/palsearth/jobs'


def stage_uploaded_file(uploaded_file):
    """
    Saves a Streamlit UploadedFile (csv or zip) into JOBS_DIR the same way
    the manual Extract flow does, and returns:
        {"input_filename": str, "geometry_type": "points"|"polygons", "n_locations": int}
    Raises ValueError on anything that doesn't look like valid input.
    """
    os.makedirs(JOBS_DIR, exist_ok=True)
    fname = uploaded_file.name
    job_tmp = str(uuid.uuid4())

    if fname.endswith('.csv'):
        import pandas as pd
        uploaded_file.seek(0)
        df = pd.read_csv(uploaded_file)
        uploaded_file.seek(0)
        lat_c = ['latitude', 'lat', 'LAT', 'Latitude', 'LATITUDE', 'y', 'Y']
        lon_c = ['longitude', 'lon', 'LON', 'Longitude', 'LONGITUDE', 'x', 'X', 'lng', 'LNG']
        lat_col = next((c for c in lat_c if c in df.columns), None)
        lon_col = next((c for c in lon_c if c in df.columns), None)
        if not lat_col or not lon_col:
            raise ValueError(
                "Could not find latitude/longitude columns. Rename them to "
                "latitude/longitude, lat/lon, or y/x."
            )
        saved = os.path.join(JOBS_DIR, f"{job_tmp}_input.csv")
        with open(saved, "wb") as f:
            f.write(uploaded_file.read())
        return {"input_filename": saved, "geometry_type": "points", "n_locations": len(df)}

    elif fname.endswith('.zip'):
        saved = os.path.join(JOBS_DIR, f"{job_tmp}_input.zip")
        uploaded_file.seek(0)
        with open(saved, "wb") as f:
            f.write(uploaded_file.read())

        exdir = os.path.join(JOBS_DIR, f"{job_tmp}_shp")
        os.makedirs(exdir, exist_ok=True)
        with zipfile.ZipFile(saved, "r") as z:
            for member in z.namelist():
                dest = os.path.realpath(os.path.join(exdir, member))
                if not dest.startswith(os.path.realpath(exdir) + os.sep):
                    raise ValueError(f"Rejected unsafe zip entry: {member}")
            z.extractall(exdir)

        from glob import glob
        shps = glob(os.path.join(exdir, "**", "*.shp"), recursive=True)
        if not shps:
            raise ValueError("No .shp file found inside the zip.")

        import geopandas as gpd
        gdf = gpd.read_file(shps[0])
        geom_type = gdf.geometry.geom_type.iloc[0]
        geometry_type = "points" if geom_type in ("Point", "MultiPoint") else "polygons"
        return {"input_filename": shps[0], "geometry_type": geometry_type, "n_locations": len(gdf)}

    else:
        raise ValueError("Only .csv or .zip (shapefile) files are supported.")
