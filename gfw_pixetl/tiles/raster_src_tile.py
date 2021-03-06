import os
from copy import deepcopy
from math import floor, sqrt
from multiprocessing import Pool
from multiprocessing.pool import Pool as PoolType
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np
import rasterio
from numpy.ma import MaskedArray
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.shutil import copy as raster_copy
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window, bounds, from_bounds, union
from retrying import retry

from gfw_pixetl import get_module_logger, utils
from gfw_pixetl.decorators import lazy_property, processify
from gfw_pixetl.errors import retry_if_rasterio_io_error
from gfw_pixetl.grids import Grid
from gfw_pixetl.layers import RasterSrcLayer
from gfw_pixetl.models.types import Bounds
from gfw_pixetl.settings.gdal import GDAL_ENV
from gfw_pixetl.settings.globals import GLOBALS
from gfw_pixetl.sources import RasterSource
from gfw_pixetl.tiles import Tile
from gfw_pixetl.utils.aws import download_s3
from gfw_pixetl.utils.gdal import create_vrt
from gfw_pixetl.utils.google import download_gcs
from gfw_pixetl.utils.path import create_dir, from_vsi

LOGGER = get_module_logger(__name__)

Windows = Tuple[Window, Window]


class RasterSrcTile(Tile):
    def __init__(self, tile_id: str, grid: Grid, layer: RasterSrcLayer) -> None:
        super().__init__(tile_id, grid, layer)
        self.layer: RasterSrcLayer = layer
        # self.src: RasterSource = RasterSource(uri=self._vrt())

    @lazy_property
    def src(self) -> RasterSource:
        LOGGER.debug(f"Find input files for {self.tile_id}")
        input_files = list()
        for f in self.layer.input_files:
            if self.dst[self.default_format].geom.intersects(f[0]) and not self.dst[
                self.default_format
            ].geom.touches(f[0]):
                LOGGER.debug(f"Add file {f[1]} to input files for {self.tile_id}")

                if self.layer.process_locally:
                    input_file = self._download_source_file(f[1])
                else:
                    input_file = f[1]

                input_files.append(input_file)

        if not len(input_files):
            raise Exception(
                f"Did not find any intersecting files for tile {self.tile_id}"
            )

        return RasterSource(create_vrt(input_files, vrt=self.tile_id + ".vrt"))

    def _download_source_file(self, remote_file: str) -> str:
        """Download remote files."""

        download_constructor = {"gs": download_gcs, "s3": download_s3}

        path = from_vsi(remote_file)
        parts = urlparse(path)

        local_file = os.path.join(self.work_dir, "input", parts.netloc, parts.path[1:])
        create_dir(os.path.dirname(local_file))

        LOGGER.debug(
            f"Download remote file {remote_file} to {local_file} using {parts.scheme}"
        )
        download_constructor[parts.scheme](
            bucket=parts.netloc, key=parts.path[1:], dst=local_file
        )

        return local_file

    @lazy_property
    def intersecting_window(self) -> Window:
        dst_left, dst_bottom, dst_right, dst_top = self.dst[self.default_format].bounds
        src_left, src_bottom, src_right, src_top = self.src.reproject_bounds(
            self.grid.crs
        )

        left = max(dst_left, src_left)
        bottom = max(dst_bottom, src_bottom)
        right = min(dst_right, src_right)
        top = min(dst_top, src_top)

        window: Window = rasterio.windows.from_bounds(
            left, bottom, right, top, transform=self.dst[self.default_format].transform
        )
        return utils.snapped_window(window)

    def within(self) -> bool:
        """Check if target tile extent intersects with source extent."""
        return (
            # must intersect, but we don't want geometries that only share an exterior point
            self.dst[self.default_format].geom.intersects(self.layer.geom)
            and not self.dst[self.default_format].geom.touches(self.layer.geom)
        )

    def transform(self) -> bool:
        """Write input data to output tile."""
        LOGGER.info(f"Transform tile {self.tile_id}")

        try:
            has_data = self._process_windows()

        except Exception as e:
            LOGGER.exception(e)
            self.status = "failed"
            has_data = True

        else:
            # invoking gdal-geotiff and compute stats here
            # instead of in a separate stage to assure we don't run out of memory
            # the transform stage uses all available memory for concurrent processes.
            # Having another stage which needs a lot of memory might cause the process to crash
            self.postprocessing()

        return has_data

    def _src_to_vrt(self) -> Tuple[DatasetReader, WarpedVRT]:
        chunk_size = (self._block_byte_size() * self._max_blocks(),)
        with rasterio.Env(
            VSI_CACHE_SIZE=chunk_size,  # Cache size for current file.
            CPL_VSIL_CURL_CHUNK_SIZE=chunk_size,  # Chunk size for partial downloads
            **GDAL_ENV,
        ):
            src: DatasetReader = rasterio.open(self.src.uri, "r", sharing=False)

            transform, width, height = self._vrt_transform(
                *self.src.reproject_bounds(self.grid.crs)
            )
            vrt = WarpedVRT(
                src,
                crs=self.dst[self.default_format].crs,
                transform=transform,
                width=width,
                height=height,
                warp_mem_limit=utils.available_memory_per_process_mb(),
                resampling=self.layer.resampling,
            )

        return src, vrt

    def _process_windows(self) -> bool:

        # In case we have more workers than cores we can further subdivide the read process.
        # In that case we will need to write the windows into separate files
        # and merger them into one file at the end of the write process
        co_workers: int = utils.get_co_workers()
        if co_workers >= 2:
            has_data: bool = self._process_windows_parallel(co_workers)

        # Otherwise we just read the entire image in one process
        # And write directly to target file.
        else:
            has_data = self._process_windows_sequential()

        return has_data

    def _process_windows_parallel(self, co_workers):
        """Process windows in parallel and write output into separate files.

        Create VRT of output files and copy results into final GTIFF
        """
        has_data = False

        LOGGER.info(f"Process tile {self.tile_id} with {co_workers} co_workers")

        pool: PoolType = Pool(processes=co_workers)
        out_files: List[Optional[str]] = pool.map(
            self._parallel_transform, self.windows()
        )
        all_files: List[str] = [f for f in out_files if f is not None]
        if all_files:
            # merge all data into one VRT and copy to target file
            vrt_name: str = os.path.join(self.tmp_dir, f"{self.tile_id}.vrt")
            create_vrt(all_files, extent=self.bounds, vrt=vrt_name)
            raster_copy(
                vrt_name,
                self.local_dst[self.default_format].uri,
                strict=False,
                **self.dst[self.default_format].profile,
            )
            # Clean up tmp files
            for f in all_files:
                LOGGER.debug(f"Delete temporary file {f}")
                os.remove(f)
            has_data = True

        return has_data

    def _process_windows_sequential(self):
        """Read on window after the other and update target file."""
        LOGGER.info(f"Process tile {self.tile_id} with a single worker")

        src: DatasetReader
        vrt: WarpedVRT

        src, vrt = self._src_to_vrt()
        out_files = list()
        for window in self.windows():
            out_files.append(self._processified_transform(vrt, window))

        has_data = any(value is not None for value in out_files)

        vrt.close()
        src.close()

        return has_data

    def _parallel_transform(self, window) -> Optional[str]:
        """When transforming in parallel, we need to read SRC and create VRT in
        every process."""
        src: DatasetReader
        vrt: WarpedVRT

        src, vrt = self._src_to_vrt()

        out_data: Optional[str] = self._transform(vrt, window, True)

        vrt.close()
        src.close()

        return out_data

    @processify
    def _processified_transform(
        self, vrt: WarpedVRT, window: Window, write_to_seperate_files=False
    ) -> Optional[str]:
        """Wrapper to run _transform in a separate process.

        This will make sure that memory get completely cleared once
        block is processed. With out this, we might experience memory
        leakage, in particular for float data types.
        """
        return self._transform(vrt, window, write_to_seperate_files)

    def _transform(
        self, vrt: WarpedVRT, window: Window, write_to_seperate_files=False
    ) -> Optional[str]:
        """Reading windows from input VRT, reproject, resample, transform and
        write to destination."""
        masked_array: MaskedArray = self._read_window(vrt, window)
        if self._block_has_data(masked_array):
            LOGGER.debug(f"{window} of tile {self.tile_id} has data - continue")
            masked_array = self._calc(masked_array, window)
            array: np.ndarray = self._set_dtype(masked_array, window)
            del masked_array
            out_file: Optional[str] = self._write_window(
                array, window, write_to_seperate_files
            )
            del array

        else:
            LOGGER.debug(f"{window} of tile {self.tile_id} has no data - skip")
            del masked_array
            out_file = None
        return out_file

    def windows(self) -> List[Window]:
        """Creates local output file and returns list of size optimized windows
        to process."""
        LOGGER.debug(f"Create local output file for tile {self.tile_id}")
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(
                self.get_local_dst_uri(self.default_format),
                "w",
                **self.dst[self.default_format].profile,
            ) as dst:
                windows = [window for window in self._windows(dst)]
        self.set_local_dst(self.default_format)

        return windows

    def _windows(self, dst: DatasetWriter) -> Iterator[Window]:
        """Divides raster source into larger windows which will still fit into
        memory."""

        block_count: int = int(sqrt(self._max_blocks()))
        x_blocks: int = int(dst.width / dst.block_shapes[0][0])
        y_blocks: int = int(dst.height / dst.block_shapes[0][1])

        for i in range(0, x_blocks, block_count):
            for j in range(0, y_blocks, block_count):
                max_i = min(i + block_count, x_blocks)
                max_j = min(j + block_count, y_blocks)
                window = self._union_blocks(dst, i, j, max_i, max_j)
                try:
                    yield utils.snapped_window(
                        window.intersection(self.intersecting_window)
                    )
                except rasterio.errors.WindowError as e:
                    if not (str(e) == "windows do not intersect"):
                        raise

    @staticmethod
    def _block_has_data(array: MaskedArray) -> bool:
        """Check if current block has any data."""
        msk = np.invert(array.mask.astype(bool))
        size = msk[msk].size
        LOGGER.debug(f"Block has {size} data pixels")
        return array.shape[0] > 0 and array.shape[1] > 0 and size != 0

    def _calc(self, array: MaskedArray, dst_window: Window) -> MaskedArray:
        """Apply user defined calculation on array."""
        if self.layer.calc:
            LOGGER.debug(f"Update {dst_window} of tile {self.tile_id}")
            funcstr = (
                f"def f(A: MaskedArray) -> MaskedArray:\n    return {self.layer.calc}"
            )
            exec(funcstr, globals())
            array = f(array)  # type: ignore # noqa: F821
        else:
            LOGGER.debug(
                f"No user defined formula provided. Skip calculating values for {dst_window} of tile {self.tile_id}"
            )
        return array

    def _max_blocks(self) -> int:
        """Calculate the maximum amount of blocks we can fit into memory,
        making sure that blocks can always fill a squared extent.

        We can only use a fraction of the available memory per process
        per block b/c we might have multiple copies of the array at the
        same time. Using a divisor of 8 leads to max memory usage of
        about 75%.
        """

        # Decrease block size, in case we have co-workers.
        # This way we can process more blocks in parallel.
        divisor = GLOBALS.divisor
        co_workers = floor(GLOBALS.cores / GLOBALS.workers)
        if co_workers >= 2:
            divisor = divisor * co_workers

        # further reduce block size in case we need to perform additional computations
        if self.layer.calc is not None:
            divisor = divisor ** 2

        LOGGER.debug(f"Divisor set to {divisor} for tile {self.tile_id}")

        bytes_per_block: int = self._block_byte_size()
        memory_per_process: float = utils.available_memory_per_process_bytes() / divisor

        # make sure we get an number we whose sqrt is a whole number
        max_blocks: int = floor(sqrt(memory_per_process / bytes_per_block)) ** 2

        LOGGER.debug(f"Maximum number of blocks to read at once: {max_blocks}")
        return max_blocks

    def _block_byte_size(self):
        # max_bytes_per_block: float = self._max_block_size(dst) * self._max_itemsize()
        block_size: int = (
            self.dst[self.default_format].blockxsize
            * self.dst[self.default_format].blockysize
        )
        LOGGER.debug(f"Block Size: {block_size}")

        item_size: int = np.zeros(1, dtype=self.dst[self.default_format].dtype).itemsize
        LOGGER.debug(f"Item Size: {item_size}")

        bytes_per_block: int = block_size * item_size
        return bytes_per_block

    @retry(
        retry_on_exception=retry_if_rasterio_io_error,
        stop_max_attempt_number=7,
        wait_exponential_multiplier=1000,
        wait_exponential_max=300000,
    )  # Wait 2^x * 1000 ms between retries by to 300 sec, then 300 sec afterwards.
    def _read_window(self, vrt: WarpedVRT, dst_window: Window) -> MaskedArray:
        """Read window of input raster."""
        dst_bounds: Bounds = bounds(dst_window, self.dst[self.default_format].transform)
        window = vrt.window(*dst_bounds)

        src_bounds = transform_bounds(
            self.dst[self.default_format].crs, self.src.crs, *dst_bounds
        )

        LOGGER.debug(
            f"Read {dst_window} for Tile {self.tile_id} - this corresponds to bounds {src_bounds} in source"
        )
        try:
            return vrt.read(
                window=window,
                out_shape=(int(round(dst_window.height)), int(round(dst_window.width))),
                masked=True,
            )
        except rasterio.RasterioIOError:
            LOGGER.warning(
                f"RasterioIO error while reading {dst_window} for Tile {self.tile_id}. "
                "Will make attempt to retry."
            )
            raise

    def _reproject_dst_window(self, dst_window: Window) -> Window:
        """Reproject window into same projection as source raster."""

        dst_bounds: Bounds = bounds(
            window=dst_window,
            transform=self.dst[self.default_format].transform,
            height=self.grid.blockysize,
            width=self.grid.blockxsize,
        )
        src_bounds: Bounds = transform_bounds(
            self.dst[self.default_format].crs, self.src.crs, *dst_bounds
        )

        src_window: Window = from_bounds(*src_bounds, transform=self.src.transform)
        LOGGER.debug(
            f"Source window for {dst_window} of tile {self.tile_id} is {src_window}"
        )
        return src_window

    def _set_dtype(self, array: MaskedArray, dst_window) -> np.ndarray:
        """Update data type to desired output datatype Update nodata value to
        desired nodata value (current no data values will be updated and any
        values which already has new no data value will stay as is)"""
        if self.dst[self.default_format].has_no_data():
            LOGGER.debug(
                f"Set datatype and no data value for {dst_window} of tile {self.tile_id}"
            )
            array = np.ma.filled(array, self.dst[self.default_format].nodata).astype(
                self.dst[self.default_format].dtype
            )

        else:
            LOGGER.debug(f"Set datatype for {dst_window} of tile {self.tile_id}")
            array = array.data.astype(self.dst[self.default_format].dtype)
        return array

    def _vrt_transform(
        self, west: float, south: float, east: float, north: float
    ) -> Tuple[rasterio.Affine, float, float]:
        """Compute Affine transformation, width and height for WarpedVRT using
        output CRS and pixel size."""

        LOGGER.debug(f"Output Bounds {west, south, east, north}")
        north, west = self.grid.snap_coordinates(north, west)
        south, east = self.grid.snap_coordinates(south, east)

        transform: rasterio.Affine = rasterio.transform.from_origin(
            west, north, self.grid.xres, self.grid.yres
        )
        width = round((east - west) / self.grid.xres)
        height = round((north - south) / self.grid.yres)

        LOGGER.debug(f"Output Affine and dimensions {transform}, {width}, {height}")
        return transform, width, height

    @staticmethod
    def _union_blocks(
        dst: DatasetWriter, min_i: int, min_j: int, max_i: int, max_j: int
    ) -> Window:
        """Loops over selected blocks of data source and merges their windows
        into one."""
        windows: List[Window] = list()

        for i in range(min_i, max_i):
            for j in range(min_j, max_j):
                windows.append(dst.block_window(1, i, j))
        return union(*windows)

    def _write_window(
        self, array: np.ndarray, dst_window: Window, write_to_seperate_files: bool
    ) -> str:
        if write_to_seperate_files:
            out_file: str = self._write_window_to_separate_file(array, dst_window)
        else:
            out_file = self._write_window_to_shared_file(array, dst_window)
        return out_file

    def _write_window_to_shared_file(
        self, array: np.ndarray, dst_window: Window
    ) -> str:
        """Write blocks into output raster."""
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(
                self.local_dst[self.default_format].uri,
                "r+",
                **self.dst[self.default_format].profile,
            ) as dst:
                LOGGER.debug(f"Write {dst_window} of tile {self.tile_id}")
                dst.write(array, window=dst_window)
                del array
        return self.local_dst[self.default_format].uri

    def _write_window_to_separate_file(
        self, array: np.ndarray, dst_window: Window
    ) -> str:

        file_name = f"{self.tile_id}_{dst_window.col_off}_{dst_window.row_off}.tif"
        file_path = os.path.join(self.tmp_dir, file_name)

        profile = deepcopy(self.dst[self.default_format].profile)
        transform = rasterio.windows.transform(dst_window, profile["transform"])
        profile.update(
            width=dst_window.width, height=dst_window.height, transform=transform
        )

        with rasterio.Env(**GDAL_ENV):

            with rasterio.open(
                file_path,
                "w",
                **profile,
            ) as dst:
                LOGGER.debug(
                    f"Write {dst_window} of tile {self.tile_id} to separate file {file_path}"
                )
                dst.write(array)
                del array
        return file_path
