import math
import os
import multiprocessing
import subprocess as sp
from typing import Any, Iterator, List, Optional, Set, Tuple

from shapely.geometry import box, Polygon, MultiPolygon

from gfw_pixetl import get_module_logger
from gfw_pixetl.errors import GDALError
from gfw_pixetl.layers import Layer
from gfw_pixetl.tiles.tile import Tile

logger = get_module_logger(__name__)


class Pipe(object):
    """
    Base Pipe including all the basic stages to seed, filter, delete and upload tiles.
    Create a subclass and override create_tiles() method to create your own pipe.
    """

    workers: int = math.ceil(multiprocessing.cpu_count() / 2)

    def __init__(self, layer: Layer, subset: Optional[List[str]] = None) -> None:
        self.grid = layer.grid
        self.layer = layer
        self.subset = subset

    def create_tiles(self, overwrite=True) -> List[Tile]:
        """
        Override this method when implementing pipes
        """
        raise NotImplementedError()

    def get_grid_tiles(self) -> Set[Tile]:
        """
        Seed all available tiles within given grid.
        Use 1x1 degree tiles covering all land area as starting point.
        Then see in which target grid cell it would fall.
        Remove duplicated grid cells.
        """

        logger.debug("Get grid Tiles")
        tiles = set()

        for i in range(-89, 91):
            for j in range(-180, 180):
                origin = self.grid.xy_grid_origin(j, i)
                tiles.add(Tile(origin=origin, grid=self.grid, layer=self.layer))

        logger.info(f"Found {len(tiles)} tile inside grid")
        # logger.debug(tiles)

        return tiles

    def filter_subset_tiles(self, tiles: Iterator[Tile]) -> Iterator[Tile]:
        """
        Apply filter incase user only want to process only a subset.
        Useful for testing.
        """
        for tile in tiles:
            if not self.subset or (self.subset and tile.tile_id in self.subset):
                yield tile

    @staticmethod
    def filter_target_tiles(
        tiles: Iterator[Tile], overwrite: bool = True
    ) -> Iterator[Tile]:
        """
        Don't process tiles if they already exists in target location,
        unless overwrite is set to True
        """
        for tile in tiles:
            if overwrite or not tile.dst_exists():
                yield tile

    @staticmethod
    def delete_if_empty(tiles: Iterator[Tile]) -> Iterator[Tile]:
        """
        Exclude empty intermediate tiles and delete local copy
        """
        for tile in tiles:
            if tile.local_src_is_empty():
                tile.rm_local_src()
            else:
                yield tile

    @staticmethod
    def upload_file(tiles: Iterator[Tile]) -> Iterator[Tile]:
        """
        Upload tile to target location
        """
        for tile in tiles:
            tile.upload()
            yield tile

    @staticmethod
    def delete_file(tiles: Iterator[Tile]) -> Iterator[Tile]:
        """
        Delete local file
        """
        for tile in tiles:
            tile.rm_local_src()
            yield tile

    def create_vrt(self, uris: List[str]) -> str:
        """
        ! Important this is not a parallelpipe Stage and must be run with only one worker
        Create VRT file from input URI.
        """

        vrt = "all.vrt"
        tile_list = "tiles.txt"

        self._write_tile_list(tile_list, uris)

        cmd = ["gdalbuildvrt", "-input_file_list", tile_list, vrt]

        logger.info("Create VRT file")
        p: sp.Popen = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)

        o: Any
        e: Any
        o, e = p.communicate()

        os.remove(tile_list)

        if p.returncode != 0:
            logger.error("Could not create VRT file")
            logger.exception(e)
            raise GDALError(e)
        else:
            return vrt

    def create_extent(self, tiles: List[Tile]) -> Polygon:
        extent: Optional[MultiPolygon] = None
        for tile in tiles:
            geom: Polygon = self._bounds_to_polygon(tile.bounds)
            if not extent:
                extent = geom
            else:
                extent = extent.union(geom)
        return extent

    @staticmethod
    def _write_tile_list(tile_list: str, uris: List[str]) -> None:
        with open(tile_list, "w") as input_tiles:
            for uri in uris:
                tile_uri = f"/vsis3/{uri}\n"
                input_tiles.write(tile_uri)

    @staticmethod
    def _bounds_to_polygon(bounds: box) -> Polygon:
        return Polygon(
            [
                (bounds[0], bounds[1]),
                (bounds[2], bounds[1]),
                (bounds[2], bounds[3]),
                (bounds[0], bounds[3]),
                (bounds[0], bounds[1]),
            ]
        )