"""
This module is a napari plugin.

It implements the ``napari_get_reader`` hook specification, (to create
a reader plugin).

Type annotations here are OPTIONAL!
If you don't care to annotate the return types of your functions
your plugin doesn't need to import, or even depend on napari at all!

Replace code below accordingly.
"""
import numpy as np
import s3fs
import os
import re
import json
import zarr
import requests
import dask.array as da
from vispy.color import Colormap

from urllib.parse import urlparse
from pluggy import HookimplMarker

import logging
# DEBUG logging for s3fs so we can track remote calls
logging.basicConfig(level=logging.INFO)
logging.getLogger('s3fs').setLevel(logging.DEBUG)

# for optional type hints only, otherwise you can delete/ignore this stuff
from typing import List, Optional, Union, Any, Tuple, Dict, Callable

LayerData = Union[Tuple[Any], Tuple[Any, Dict], Tuple[Any, Dict, str]]
PathLike = Union[str, List[str]]
ReaderFunction = Callable[[PathLike], List[LayerData]]
# END type hint stuff.

napari_hook_implementation = HookimplMarker("napari")



@napari_hook_implementation
def napari_get_reader(path: PathLike) -> Optional[ReaderFunction]:
    """
    Returns a reader for supported paths that include IDR ID

    - URL of the form: https://s3.embassy.ebi.ac.uk/idr/zarr/v0.1/ID.zarr/
    """
    if isinstance(path, list):
        path = path[0]

    result = urlparse(path)
    if result.scheme in ("", "file"):
        # Strips 'file://' if necessary
        instance = LocalZarr(result.path)
    else:
        instance = RemoteZarr(path)

    if instance.is_zarr():
        return instance.get_reader_function()


class BaseZarr:

    def __init__(self, path):
        self.zarr_path = path.endswith("/") and path or f"{path}/"
        self.zarray = self.get_json(".zarray")
        self.zgroup = self.get_json(".zgroup")
        if self.is_zarr():
            self.image_data = self.get_json("omero.json")
            self.root_attrs = self.get_json(".zattrs")

    def is_zarr(self):
        return self.zarray or self.zgroup

    def get_json(self, subpath):
        raise NotImplementedError("unknown")

    def get_reader_function(self):
        if not self.is_zarr():
            raise Exception("not a zarr")
        return self.reader_function

    def reader_function(self, path: PathLike) -> List[LayerData]:
        """Take a path or list of paths and return a list of LayerData tuples."""
        if isinstance(path, list):
            path = path[0]
        return [self.load_omero_zarr()]

    def load_omero_metadata(self):
        """Load OMERO metadata as json and convert for napari"""
        metadata = {}
        image_data = self.image_data
        try:
            print(image_data)
            colormaps = []
            for ch in image_data['channels']:
                # 'FF0000' -> [1, 0, 0]
                rgb = [(int(ch['color'][i:i+2], 16)/255) for i in range(0, 6, 2)]
                if image_data['rdefs']['model'] == 'greyscale':
                    rgb = [1, 1, 1]
                colormaps.append(Colormap([[0, 0, 0], rgb]))
            metadata['colormap'] = colormaps
            metadata['contrast_limits'] = [[ch['window']['start'], ch['window']['end']] for ch in image_data['channels']]
            metadata['name'] = [ch['label'] for ch in image_data['channels']]
            metadata['visible'] = [ch['active'] for ch in image_data['channels']]
        except Exception:
            pass

        return metadata


    def load_omero_zarr(self):

        resolutions = ["0"]  # TODO: could be first alphanumeric dataset on err
        try:
            print('root_attrs', self.root_attrs)
            if 'multiscales' in self.root_attrs:
                datasets = self.root_attrs['multiscales'][0]['datasets']
                resolutions = [d['path'] for d in datasets]
            print('resolutions', resolutions)
        except Exception as e:
            raise e

        pyramid = []
        for resolution in resolutions:
            # data.shape is (t, c, z, y, x) by convention
            data = da.from_zarr(f"{self.zarr_path}{resolution}")
            chunk_sizes = [str(c[0]) + (" (+ %s)" % c[-1] if c[-1] != c[0] else '') for c in data.chunks]
            print('resolution', resolution, 'shape (t, c, z, y, x)', data.shape, 'chunks', chunk_sizes, 'dtype', data.dtype)
            pyramid.append(data)

        metadata = self.load_omero_metadata()
        return(pyramid, {'channel_axis': 1, **metadata})



class LocalZarr(BaseZarr):

    def get_json(self, subpath):
        filename = os.path.join(self.zarr_path, subpath)

        if not os.path.exists(filename):
            return {}

        with open(filename) as f:
            return json.loads(f.read())


class RemoteZarr(BaseZarr):

    def get_json(self, subpath):
        rsp = requests.get(f"{self.zarr_path}{subpath}")
        try:
            if rsp.status_code == 403:  # file doesn't exist
                return {}
            return rsp.json()
        except:
            print("FIXME", rsp.text, dir(rsp))
            return {}
